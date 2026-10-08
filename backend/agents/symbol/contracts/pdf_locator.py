"""① PDF 结构定位（docs/specs/Module1-Spec.md §8.1）。

只做轻量文本层分析，不解图、不调外部服务：把整份 datasheet 收敛到
「引脚章节 + 封装章节」两小段页码，供 ② 渲染和提取使用。

四级策略，命中即停：
  1. 书签 / Outline（最可靠）
  2. 目录页文本解析
  3. 关键词锚点 ± 2 页
  4. 比例法兜底

章节边界语义：从命中页到「下一个书签/目录条目的前一页」，不是固定 ±N。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from .pkgname import hint_pages

# ---------------------------------------------------------------- 锚点词表
ANCHORS: dict[str, list[str]] = {
    "pins": [
        "pin configuration",
        "pin functions",
        "pin function",
        "pin description",
        "pin assignment",
        "pinout",
        "引脚配置",
        "引脚功能",
        "引脚定义",
        "管脚配置",
        "管脚功能",
    ],
    "package": [
        "mechanical packaging",     # 覆盖 TI 的 "Mechanical, Packaging, and Orderable Information"
        "mechanical data",
        "package outline",
        "package option addendum",
        "package materials",
        "package dimensions",
        "packaging information",
        "generic package view",
        "land pattern",
        "stencil",
        "封装尺寸",
        "封装信息",
        "封装图",
        "封装和可订购信息",
        "焊盘",
    ],
}

TARGET_TITLES = {"pins": "引脚章节", "package": "封装章节"}

# 封装章节常见的小标题：章节过大时用它把无信息量的页裁掉
PACKAGE_KEEP_MARKERS = [
    "package option addendum",
    "package materials information",
    "package outline",
    "example board layout",
    "example stencil design",
    "generic package view",
    "mechanical data",
    "land pattern",
    "封装",
]
PACKAGE_DROP_MARKERS = [
    "important notice",
    "重要声明",
    "重要通知",
    "版权",
]

MAX_SECTION_PAGES = 24   # 单段最多取这么多页，防止书签缺失时整本吃进来
NARROW_THRESHOLD = 6     # 封装章节超过这么多页才启用裁剪
FALLBACK_WINDOW = 2      # 策略 3 的 ±N
FALLBACK_RATIO = 0.15    # 策略 4：从头/尾各取 15%
PIN_SEARCH_LIMIT = 0.6       # 引脚章节只可能在前 60% 页里
PACKAGE_SEARCH_START = 0.5   # 封装章节只可能在后 50% 页里


@dataclass
class Entry:
    """一条书签或目录条目。"""

    level: int
    title: str
    page: int


@dataclass
class LocateResult:
    pdf: str
    total_pages: int
    strategy: str = ""
    entries: list[Entry] = field(default_factory=list)
    targets: dict[str, list[int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def all_pages(self) -> list[int]:
        pages: set[int] = set()
        for value in self.targets.values():
            pages.update(value)
        return sorted(pages)

    def to_dict(self) -> dict:
        return {
            "pdf": self.pdf,
            "total_pages": self.total_pages,
            "strategy": self.strategy,
            "targets": {k: v for k, v in self.targets.items()},
            "pages": self.all_pages,
            "notes": self.notes,
        }


# ---------------------------------------------------------------- 工具
_PUNCT_RE = re.compile(r"[\s　\-–—_/\\:：·.、()（）\[\]【】,，]+")


def _norm(text: str) -> str:
    return _PUNCT_RE.sub(" ", text.strip().lower()).strip()


def _matches(title: str, anchors: list[str]) -> bool:
    """标题是否命中锚点：按词边界匹配，避免 'pin' 误伤正文。

    词尾留一个可选的 `s`。手册目录里写的多是**复数** —— ATMEGA328P 的书签明明是
    `1. Pin Configurations` / `1.1 Pin Descriptions`，而词表里只有单数，
    原来的 `(?![a-z0-9])` 恰好卡在词尾那个 `s` 上，一条都命中不了，引脚表就躺在
    第 8 页没人去取。**不去逐个补复数**：词表是人维护的，漏一个就静默失效一次，
    不如让匹配函数对英语复数天然宽容，一次修好。
    """
    norm = _norm(title)
    if not norm:
        return False
    for anchor in anchors:
        a = _norm(anchor)
        if not a:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(a)}s?(?![a-z0-9])", norm):
            return True
    return False


# ---------------------------------------------------------------- 策略 1
def _bookmark_entries(doc: pymupdf.Document) -> list[Entry]:
    entries = []
    for level, title, page in doc.get_toc(simple=True):
        if not title or page < 1:
            continue
        entries.append(Entry(level=int(level), title=title.strip(), page=int(page)))
    entries.sort(key=lambda e: (e.page, e.level))
    return entries


# ---------------------------------------------------------------- 策略 2
_TOC_LINE_RE = re.compile(r"^(?P<title>.+?)[\s.·]{3,}(?P<page>\d{1,4})\s*$")


def _toc_entries(doc: pymupdf.Document, max_scan: int = 12) -> list[Entry]:
    """在前若干页里找目录页并解析出条目。

    注意：长标题会折行，形如
        `14 Mechanical, Packaging, and Orderable `
        `Information.................................................... 41`
    所以遇到短标题时要把它和上一行拼回去，否则锚点词会丢。
    """
    entries: list[Entry] = []
    for pno in range(min(max_scan, doc.page_count)):
        text = doc[pno].get_text("text")
        if "contents" not in text[:400].lower() and text.count("....") < 3:
            continue

        prev = ""
        for line in text.splitlines():
            stripped = line.strip()
            match = _TOC_LINE_RE.match(stripped)
            if not match:
                if stripped:
                    prev = stripped
                continue

            title = match.group("title").strip()
            # 标题被折断的判据：剩下的词很少，且不以章节号开头
            if prev and len(title.split()) <= 2 and not title[:1].isdigit():
                title = f"{prev} {title}".strip()
            prev = ""

            page = int(match.group("page"))
            if 1 <= page <= doc.page_count:
                entries.append(Entry(level=1, title=title, page=page))
        if entries:
            break
    entries.sort(key=lambda e: e.page)
    return entries


# ---------------------------------------------------------------- 策略 3
_HEADING_MAX_LEN = 72


def _keyword_entries(doc: pymupdf.Document, anchors: list[str]) -> list[Entry]:
    """全文扫「像标题的行」：短、且命中锚点。"""
    entries: list[Entry] = []
    for pno in range(doc.page_count):
        for line in doc[pno].get_text("text").splitlines():
            line = line.strip()
            if not line or len(line) > _HEADING_MAX_LEN:
                continue
            if _matches(line, anchors):
                entries.append(Entry(level=1, title=line, page=pno + 1))
    return entries


# ---------------------------------------------------------------- 章节展开
def _section_pages(entries: list[Entry], hit: Entry, total_pages: int) -> list[int]:
    """从命中条目展开到「下一个条目的前一页」，**收尾多留一页**。

    图注条目常常排在表格条目之前 —— STM32F030 的引脚章节里 `Figure 3. LQFP64 …
    pinout` 排在 `Table 5. Pin definitions` 前面，按「下一个条目的前一页」收尾
    就正好把表切掉：页集给到 25，而表在第 26 页。STM32F405/F407VET6 同理
    （给到 44、表在 45）。多送一页只多花一点解析费，漏一页是**整份白跑**。
    """
    later = [e.page for e in entries if e.page > hit.page]
    end = (min(later) - 1) if later else total_pages
    end = min(end, hit.page + MAX_SECTION_PAGES - 1, total_pages)
    end = min(end + 1, total_pages)              # 收尾多留一页（见 docstring）
    if end < hit.page:
        end = hit.page
    return list(range(hit.page, end + 1))


def _collect(
    key: str, entries: list[Entry], total_pages: int
) -> tuple[list[int], list[Entry]]:
    hits = [e for e in entries if _matches(e.title, ANCHORS[key])]
    if not hits:
        return [], []
    pages: set[int] = set()
    for hit in hits:
        pages.update(_section_pages(entries, hit, total_pages))
    return sorted(pages), hits


def _narrow_package(
    doc: pymupdf.Document, pages: list[int]
) -> tuple[list[int], str | None]:
    """封装章节常常很长（多个封装的图纸 + 法律声明），裁掉无信息量的页。"""
    if len(pages) <= NARROW_THRESHOLD:
        return pages, None

    head = pages[0]                       # 章节首页保留（含订购信息/概述）
    kept = [head]
    for pno in pages[1:]:
        text = doc[pno - 1].get_text("text").lower()
        if any(m in text for m in PACKAGE_DROP_MARKERS):
            continue
        if any(m in text for m in PACKAGE_KEEP_MARKERS):
            kept.append(pno)

    if len(kept) >= 2 and len(kept) < len(pages):
        note = f"封装章节过长，裁剪掉 {len(pages) - len(kept)} 页无信息量页面"
        return kept, note
    return pages, None


_SENTENCE_TAIL = "。.;；,，:："
_BULLET_PREFIX = ("•", "-", "·", "*", "(", "（", "【")

# `Figure 5. …` / `Table 3-1 …` / `Section 8 …` 这类**带编号的图注/表注行**的前缀
_NUMBERED_PREFIX_RE = re.compile(
    r"^(?:fig(?:ure)?|tab(?:le)?|section|chapter|appendix|part)\s*"
    r"[0-9]+(?:[.\-–][0-9]+)*\s*[.:、]?\s*",
    re.I,
)
_CAPTION_MAX_BODY = 40     # 去掉编号前缀后，剩下的部分最长这么长才算标题


def _heading_like(line: str) -> bool:
    """判断一行文本是否「像标题」，用于滤掉正文里的锚点词。"""
    s = line.strip()
    if not s or len(s) > _HEADING_MAX_LEN:
        return False
    if s[-1] in _SENTENCE_TAIL or s.startswith(_BULLET_PREFIX):
        return False
    if re.match(r"^\d+(\.\d+)*\s+\S", s):          # 14 Mechanical, Packaging, ...
        return True
    letters = re.sub(r"[^A-Za-z]", "", s)
    if letters and letters.upper() == letters:     # 全大写英文
        return True

    # 带编号的图注/表注行：AD7793 的真锚点是第 9 页的 `Figure 5. Pin Configuration`，
    # 它三条旧判据全落空 —— 不以数字开头、不是全大写，而那条
    # `fullmatch(r"[一-鿿A-Za-z0-9\s]{2,20}")` 也因为**中间那个 `.` 不在字符类里**、
    # 且长度超 20 而失败。于是锚点扫描空手而归，退化成比例法取前 4 页。
    # 新判据只看一件事：**去掉编号前缀后，剩下的部分短且不像句子**。
    body = _NUMBERED_PREFIX_RE.sub("", s).strip()
    if body != s and 0 < len(body) <= _CAPTION_MAX_BODY:
        if body[-1] not in _SENTENCE_TAIL and len(body.split()) <= 6:
            return True

    if re.fullmatch(r"[一-鿿A-Za-z0-9\s]{2,20}", s) and "(" not in s:
        return True
    return False


def _fallback(doc: pymupdf.Document, key: str) -> tuple[list[int], str]:
    """策略 3：锚点 ± 2 页；策略 4：比例法。

    引脚章节总在文档前段，封装章节总在文档末段，因此按方向筛候选页，
    避免首页「特性」里的 封装信息/引脚功能 之类小标题把定位带偏。
    """
    anchors = ANCHORS[key]
    n = doc.page_count
    if key == "pins":
        lo_bound, hi_bound = 1, max(1, int(n * PIN_SEARCH_LIMIT))
    else:
        lo_bound, hi_bound = max(1, int(n * PACKAGE_SEARCH_START)), n

    candidates: list[int] = []
    for pno in range(doc.page_count):
        page_no = pno + 1
        if not (lo_bound <= page_no <= hi_bound):
            continue
        for line in doc[pno].get_text("text").splitlines():
            if _heading_like(line) and _matches(line, anchors):
                candidates.append(page_no)
                break

    if not candidates:                              # 方向筛选后没结果，全文档再扫一遍
        for pno in range(doc.page_count):
            for line in doc[pno].get_text("text").splitlines():
                if _heading_like(line) and _matches(line, anchors):
                    candidates.append(pno + 1)
                    break

    if candidates:
        hit = candidates[0]                         # 候选按页序收集，取最早的一处
        lo = max(1, hit - FALLBACK_WINDOW)
        hi = min(n, hit + FALLBACK_WINDOW)
        return list(range(lo, hi + 1)), "keyword-window"

    window = max(1, int(n * FALLBACK_RATIO))
    if key == "pins":
        return list(range(1, window + 1)), "ratio"
    return list(range(max(1, n - window + 1), n + 1)), "ratio"


# ---------------------------------------------------------------- 引脚图页
# 引脚图（Top View 那种封装外形图）所在页的特征词
FIGURE_MARKERS = [
    "pinout",
    "pin assignment",
    "top view",
    "引脚图",
    "引脚配置",
    "管脚配置",
    "封装图",
]
# 引脚表所在页的特征词：这种页是密集的表格，不是图。
# **注意别把 "pin description" 写进来** —— 那是章节标题（`Pinouts and pin
# description`），会印在该章节每一页的页眉上，连引脚图页也一起扣分，判据就废了。
# 这里要的是表格本身的标题，比如 STM32 的 `Table 5. Pin definitions`。
TABLE_PAGE_MARKERS = [
    "pin definition",
    "pin definitions",
    "pin functions",
    "引脚定义",
    "引脚功能",
    "管脚功能",
]


def figure_pages(
    pdf_path: str | Path, pages: list[int], package: str = ""
) -> list[int]:
    """在候选页里挑出「引脚图可能在哪几页」，按可信度从高到低排。

    引脚图和引脚表**未必在同一页**。STM32F105 的引脚表在第 26 页，引脚图却在
    第 24 页 —— 而且第 23/24/25 页分别画着 BGA100 / LQFP100 / LQFP64 三张图，
    得靠页面文字里的封装名认页，选错封装等于白画。

    打分：页里有目标封装名 +2，有引脚图特征词 +1，有引脚表特征词 -3。
    只返回正分的页；空结果表示「这条线索不灵」，调用方回退到原来的页。
    """
    path = Path(pdf_path)
    if not path.exists() or not pages:
        return []

    wanted = (package or "").strip().lower()
    scored: list[tuple[int, int]] = []
    with pymupdf.open(path) as doc:
        for page_no in pages:
            if not (1 <= page_no <= doc.page_count):
                continue
            text = doc[page_no - 1].get_text("text").lower()
            score = 0
            if wanted and wanted in text:
                score += 2
            if any(marker in text for marker in FIGURE_MARKERS):
                score += 1
            if any(marker in text for marker in TABLE_PAGE_MARKERS):
                score -= 3
            if score > 0:
                scored.append((-score, page_no))     # 负号 → 高分排前面，同分按页序
    scored.sort()
    return [page_no for _, page_no in scored]


# ---------------------------------------------------------------- 瘦身页集
# MinerU 的单文件上限是 200 页，触发点在上传申请阶段、错误信息生硬，用户很难自诊；
# 而且「exceeds 200」到底是 >200 还是 >=200 没验证过。**留 10 页余量**，按 190 截。
SLIM_LIMIT = 190
SLIM_MARGIN = 2          # 引脚段前后各留几页，吸收「边界算早一页」这类偏差
SLIM_HEAD_PAGES = (1, 2, 3)   # 特性列表/概述，常常是第一处封装线索所在


@dataclass
class SlimPlan:
    """「这份 PDF 送哪几页给 MinerU」的决定，附一段能说清理由的说明。"""

    pages: list[int] = field(default_factory=list)      # 原 PDF 的 1 起页号，**有序**
    notes: list[str] = field(default_factory=list)


def slim_pages(
    located: LocateResult,
    pdf_path: str | Path,
    *,
    limit: int = SLIM_LIMIT,
    strict: bool = False,
) -> SlimPlan:
    """决定只把哪几页送给 MinerU。

    为什么要瘦身：MinerU 有单文件 200 页硬上限，超限**整个拒收**，一份 1943 页的
    手册现在连一行结果都出不来。而真正要看的（引脚表、封装图、订购信息）通常只有
    几十页。

    **顺序就是判据，不能打乱。** `pipeline._best_table` 是「按候选页顺序找第一张真
    能解析出引脚的表，找到就停」，排在前面 = 优先被当成引脚表。引脚段必须在最前，
    线索页绝不能排它前面 —— STM32F030 的第 30/32 页也有 2 脚/16 脚的杂表，顺序错
    了会选到杂表，而且**静默选错**。

    组成（括号里是它为什么必须在场）：
      1. `targets["pins"]` —— 引脚段，表就在这里
      2. 引脚段各自 ±`SLIM_MARGIN` 页的余量环 —— 吸收定位偏差
      3. 带封装线索的页 —— 脚数判据的数源，丢了会凭空多出 `pin_count_mismatch` 误报
         （实测 STM32H723 丢 176、STM32F722 丢 216、STM32H743 丢 240）
      4. 前 3 页 —— 概述/特性，常常写着一整排封装式样
      5. `targets["package"]` —— 封装段

    **`strict=True`：只送引脚段 + 封装段**，就是目录里那两节，其余一概不留。
    这是用户要的「我只需要这两节」的开关 —— 页数最少、最省，代价是把三样保险
    一起关了（余量环、前 3 页、封装线索页），于是：
      - 章节边界算早一页时（STM32F030 给到 25、表在 26）会**解析不出引脚表**；
      - 脚数判据缺了数源会**两头都出问题**：既误报，也**漏报**。实测 ADS1256
        真该报的 `pin_count_mismatch` 在严格模式下整个消失（broken 65 分变
        suspect 90 分）—— 少送页**不是更干净的瘦身，是拿判据的灵敏度换页数**。
    换句话说 strict 不是「更准的瘦身」。它仍然有安全网兜着（`pipeline` 里找不到
    表会自动退回全档重跑，只要原 PDF 没超 200 页），所以最坏结果是多花一次全档
    上传费，不是出错结果 —— 但**判据变钝这件事没有任何安全网兜**。

    超 `limit` 就截断，并把「砍掉了什么」写进 `notes` —— 这部分必须可见，绝不静默。
    """
    total = located.total_pages
    pages: list[int] = []
    seen: set[int] = set()

    def push(seq) -> None:
        for page in seq:
            if isinstance(page, int) and 1 <= page <= total and page not in seen:
                seen.add(page)
                pages.append(page)

    pins = [p for p in (located.targets.get("pins") or []) if isinstance(p, int)]
    push(pins)
    if not strict:
        # 余量环接在**整段引脚段之后**，不是逐页插在中间 —— 保持「引脚段原页优先」不变
        ring = sorted({p + d for p in pins for d in range(-SLIM_MARGIN, SLIM_MARGIN + 1)})
        push(ring)
        # 扫全文找线索页是这里最慢的一段（1943 页那份约 5 秒），strict 下直接跳过
        push(hint_pages(pdf_path))
        push(SLIM_HEAD_PAGES)
    push(located.targets.get("package") or [])

    notes: list[str] = []
    if strict:
        notes.append(
            f"严格模式：只送引脚段 + 封装段 {len(pages)} 页（余量环、前 3 页、"
            f"封装线索页都没送）—— 线索页是脚数判据的**唯一**数源，缺了会**误报也会漏报**："
            f"实测 ADS1256 真该报的 pin_count_mismatch 会整个消失（broken 65 分变 suspect 90 分）"
        )
    if len(pages) > limit:
        dropped = pages[limit:]
        pages = pages[:limit]
        kept = "引脚段和封装段的前半" if strict else "引脚段、线索页、前 3 页"
        notes.append(
            f"瘦身页集超过 {limit} 页上限，砍掉尾部 {len(dropped)} 页"
            f"（第 {dropped[0]}–{dropped[-1]} 页，多为封装段）；{kept} 保留在前 {limit} 页里"
        )
    notes.append(
        f"瘦身：全 {total} 页 → 送 {len(pages)} 页（引脚段 {len(pins)} 页 + "
        + ("封装段）" if strict else "余量环 + 封装线索页 + 前 3 页 + 封装段）")
    )
    return SlimPlan(pages=pages, notes=notes)


# ---------------------------------------------------------------- 主入口
def locate(pdf_path: str | Path) -> LocateResult:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    with pymupdf.open(pdf_path) as doc:
        total = doc.page_count
        result = LocateResult(pdf=str(pdf_path), total_pages=total)

        entries = _bookmark_entries(doc)
        strategy = "bookmark" if entries else ""
        if not entries:
            entries = _toc_entries(doc)
            strategy = "toc" if entries else ""

        if entries:
            result.entries = entries
            for key in ANCHORS:
                pages, hits = _collect(key, entries, total)
                if pages:
                    if key == "package":
                        pages, note = _narrow_package(doc, pages)
                        if note:
                            result.notes.append(f"{TARGET_TITLES[key]}：{note}")
                    result.targets[key] = pages
                    titles = "、".join(dict.fromkeys(h.title for h in hits))
                    result.notes.append(f"{TARGET_TITLES[key]}：命中「{titles}」")
                else:
                    pages, how = _fallback(doc, key)
                    result.targets[key] = pages
                    result.notes.append(
                        f"{TARGET_TITLES[key]}：书签/目录无命中条目，退化为 {how} "
                        f"取第 {pages[0]}–{pages[-1]} 页"
                    )
            result.strategy = strategy
        else:
            result.strategy = "keyword-window"
            for key in ANCHORS:
                pages, how = _fallback(doc, key)
                result.targets[key] = pages
                result.notes.append(
                    f"{TARGET_TITLES[key]}：无书签无目录，{how} 取第 "
                    f"{pages[0]}–{pages[-1]} 页"
                )

    return result


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("用法：python -m app.pdf_locator <datasheet.pdf>")
        return 2
    result = locate(argv[1])
    print(f"文件      : {result.pdf}")
    print(f"总页数    : {result.total_pages}")
    print(f"定位策略  : {result.strategy}")
    for key, pages in result.targets.items():
        print(f"{TARGET_TITLES[key]:<10}: {len(pages)} 页 -> {pages}")
    for note in result.notes:
        print(f"  · {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
