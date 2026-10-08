"""封装名：从 MinerU 解析出的文本里刮"这份手册提到过哪些封装、各多少脚"。

**为什么需要它**（2026-09-10 飞轮发现的）。第一次跑 MCU+ADC 前 20 份，14 份出了
结论，`pin_count_mismatch`（"LQFP100 只解出 87 个引脚"那条）**一次都没触发过** ——
不是文件都对，是这条检查压根没通电：14 份里 11 份封装名为空，剩下 3 份读到的是
`RUG` / `PW` / `DGS` 这种纯字母订购码，按 `pin_count_from_package` 的设计提不出脚数。

而封装名之所以拿不到，根子不在"视觉不够强"。真实图注写的是封装**式样**不是订购码：

    Figure 49. ADS1115 X2QFN Package      SSOP PACKAGE(TOP VIEW)      16-Lead TSSOP

这些是数据手册自己白纸黑字写的，MinerU 已经把它们当文本抓回来了，一条没用上。
本模块就是去把它们捡回来 —— 零成本、可核对（每条线索带页码和原文片段）。

**取数纪律**：照抄 `selfcheck` 那条 —— **宁可闭嘴，也不拿猜的数去报警**。脚数一律
要过 `_plausible_pin_count`，它挡的就是 `SOT23` 的 23 这种"封装名里的数字不是脚数"。

**依赖方向**：本模块是"封装名的一切知识"的最底层，不 import 项目内任何东西；
`selfcheck` 反过来从这里取词汇表。这样两者不成环（`selfcheck` 要用 `PackageHint`
做类型标注，若词汇表留在 `selfcheck` 就会互相 import）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# -------------------------------------------------------------------- 封装词汇表
# 已知封装式样。**长的必须排在短的之前** —— 否则 `SSOP` 会抢在 `TSSOP` 前头命中，
# `QFN` 会在 `X2QFN` 里被匹配到（虽然 \b 通常能挡住，但顺序对了就不必依赖它）。
_PACKAGE_PREFIXES = (
    "HTSSOP", "TSSOP", "VSSOP", "LQFP", "TQFP", "VQFP", "LFBGA", "TFBGA", "UFBGA",
    "VFBGA", "WLCSP", "DSBGA", "HVQFN", "X2QFN", "WQFN", "UQFN", "VSON", "QFN",
    "DFN", "SOIC", "SSOP", "MSOP", "PDIP", "DIP", "BGA", "LGA", "CSP", "SON", "SOT",
    "QFP",
)

# 拼成交替式，给线索正则用
_STYLE_ALT = "|".join(_PACKAGE_PREFIXES)

_PREFIX_RE = re.compile(r"\b(" + _STYLE_ALT + r")[\s\-_]*(\d{1,4})\b", re.I)
# `SOT23-5` / `MSOP-10` / `TSSOP-20`：尾巴上的数字才是脚数
_SUFFIX_RE = re.compile(r"-(\d{1,4})\s*$")
_DIGIT_RUN_RE = re.compile(r"\d+")

# ---------------------------------------------------------------- 线索提取用的正则
# 「16-Lead TSSOP」「14-pin SOIC」—— 数据手册最爱的写法，式样和脚数分开写
_HINT_LEAD_RE = re.compile(
    r"(\d{1,3})\s*[-\s]?(?:leads?|pins?)\b[^.]{0,24}?\b(" + _STYLE_ALT + r")\b", re.I
)
# 「LQFP64」「TSSOP16」—— 式样紧贴脚数
_HINT_STYLE_NUM_RE = re.compile(r"\b(" + _STYLE_ALT + r")[\s\-_]?(\d{1,3})\b", re.I)
# 只有式样：「SSOP PACKAGE(TOP VIEW)」。给候选名可以，给脚数不行
_HINT_STYLE_RE = re.compile(r"\b(" + _STYLE_ALT + r")\b", re.I)

# **这些类型的块一律不看**：实测一份 1179 块的文档里 header/footer/page_number
# 三类占了 500 多块，页眉页脚会把同一个封装名重复几十遍，把真线索淹掉。
# （按 type 排除，不是按"有没有 text 键"—— header 块同样有 text 键。）
_SKIP_TYPES = {"header", "footer", "page_number", "page_footnote", "equation"}

# 要扫的块 → 它哪几个键装着正文；值是这个块算"正文"还是"图注"（图注更贴近引脚图，
# `package_candidates()` 排序时给它加权）。
_SCAN_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("text", ("text", "aside_text")),           # 正文 / 页边批注
    ("caption", ("image_caption", "table_caption", "chart_caption")),
)


def _plausible_pin_count(value: int) -> bool:
    """这个数字像不像脚数。

    奇数里只认 3 和 5 —— 单封装芯片的奇数脚基本只有这两种。这条挡的是
    `SOT23`（SOT-23 是 3 脚封装，23 不是脚数）和 `SOT89`（3 脚）这类
    "封装名里的数字根本不是脚数"的坑。
    """
    if value in (3, 5):
        return True
    return 4 <= value <= 2000 and value % 2 == 0


def pin_count_from_package(package: str) -> int | None:
    """从封装名里推脚数；推不出来返回 None。

    优先级（顺序不能换）：

    1. `-数字` 后缀 —— `SOT23-5` → 5、`MSOP-10` → 10。**必须最先判**，否则下面
       的 `SOT` 前缀规则会把 `SOT23` 的 23 当成脚数。
    2. 已知封装前缀 + 数字 —— `LQFP100` → 100、`BGA100` → 100。
    3. 订购码形态（**只有恰好一段数字**才认）—— `DGS0010A` → 10。
       加"恰好一段"这个限制是为了挡住 `STM32F105VCT6` 这种型号串：它有三段数字，
       取最后一段会得到 6，把型号当封装名填进来时会误报。

    推不出来就返回 None —— 自检宁可闭嘴，也不能拿一个猜的数去报警。
    """
    text = (package or "").strip()
    if not text:
        return None

    match = _SUFFIX_RE.search(text)
    if match:
        value = int(match.group(1))
        if _plausible_pin_count(value):
            return value

    match = _PREFIX_RE.search(text)
    if match:
        value = int(match.group(2))
        if _plausible_pin_count(value):
            return value

    runs = _DIGIT_RUN_RE.findall(text)
    if len(runs) == 1 and text[:1].isalpha() and len(text) <= 12:
        value = int(runs[0])
        if _plausible_pin_count(value):
            return value

    return None


# -------------------------------------------------------------------- 线索
@dataclass(frozen=True)
class PackageHint:
    """一处"这份手册提到过这个封装"的证据。"""

    style: str                  # 规范化大写：TSSOP / LQFP / X2QFN
    pin_count: int | None       # 顺带读到的脚数；只有式样时为 None
    page: int                   # 1 起，与项目其余部分一致（content_list 的 page_idx 是 0 起）
    evidence: str               # 原文片段，供人核对 —— 必须留痕
    source: str = "text"        # "text" | "caption"

    @property
    def label(self) -> str:
        """给用户看的候选名：有脚数就用 `<式样><脚数>`，如 `LQFP64` / `TSSOP16`。

        特意选这个形状，是因为 `pin_count_from_package` 认得它（见 `_PREFIX_RE`）
        —— 于是候选名自己就能当判据用，不必另造一套解析。只有式样时就用式样本身
        （`SSOP`），它推不出脚数，但仍是个合法的候选名。
        """
        return f"{self.style}{self.pin_count}" if self.pin_count else self.style


def _blocks_text(blocks: Iterable[dict[str, Any]]) -> list[tuple[int, str, str]]:
    """把 content_list 里**值得扫的**块摊成 (页码, 文本, 来源) 三元组。

    来源是 "text" 还是 "caption" —— 图注里的封装名比正文里的更贴近引脚图，
    `package_candidates()` 排序时会给它加权。
    """
    out: list[tuple[int, str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if str(block.get("type") or "").lower() in _SKIP_TYPES:
            continue
        page = int(block.get("page_idx") or 0) + 1
        for source, keys in _SCAN_KEYS:
            for key in keys:
                value = block.get(key)
                if isinstance(value, str) and value.strip():
                    out.append((page, value.strip(), source))
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            out.append((page, item.strip(), source))
    return out


def _clip(text: str, start: int, end: int, *, pad: int = 24) -> str:
    """截取命中处周围一小段原文，作为证据。太长就截断，别把整段塞进 Finding。"""
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    snippet = " ".join(text[lo:hi].split())
    if lo > 0:
        snippet = "…" + snippet
    if hi < len(text):
        snippet = snippet + "…"
    return snippet[:120]


def _hints_in(page: int, text: str, source: str) -> list[PackageHint]:
    """从一段文本里刮线索。

    三种模式**按可信度依次扫，命中的位置要登记**（`claimed`）—— 否则
    `16-Lead TSSOP` 会同时产出 `TSSOP16` 和 `TSSOP` 两个候选，凭空多出一条噪音。
    """
    hints: list[PackageHint] = []
    claimed: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return not any(s < end and start < e for s, e in claimed)

    def add(style: str, count: int | None, start: int, end: int) -> None:
        claimed.append((start, end))
        hints.append(
            PackageHint(
                style=style.upper(),
                pin_count=count,
                page=page,
                evidence=_clip(text, start, end),
                source=source,
            )
        )

    # ① 「16-Lead TSSOP」—— 脚数和式样都明写
    for match in _HINT_LEAD_RE.finditer(text):
        value = int(match.group(1))
        if not _plausible_pin_count(value):
            continue
        add(match.group(2), value, match.start(), match.end())

    # ② 「LQFP64」—— 式样紧贴脚数
    for match in _HINT_STYLE_NUM_RE.finditer(text):
        if not free(match.start(), match.end()):
            continue
        value = int(match.group(2))
        if not _plausible_pin_count(value):
            continue
        add(match.group(1), value, match.start(), match.end())

    # ③ 只有式样 —— 给得出候选名，给不出脚数
    for match in _HINT_STYLE_RE.finditer(text):
        if not free(match.start(), match.end()):
            continue
        add(match.group(1), None, match.start(), match.end())

    return hints


def hints_from_text(blocks: Iterable[dict[str, Any]]) -> list[PackageHint]:
    """扫一遍 MinerU 的 `content_list`，返回所有封装线索（按文档顺序）。

    同一处线索会在多页重复出现（目录、正文、订购信息表都写一遍），这里**不去重**
    —— 出现次数本身就是排序依据，交给 `package_candidates()` 用。
    """
    hints: list[PackageHint] = []
    for page, text, source in _blocks_text(blocks):
        hints.extend(_hints_in(page, text, source))
    return hints


def package_candidates(hints: Iterable[PackageHint]) -> list[str]:
    """把线索归成给人看的候选名，最可能的排前面。

    排序依据（都是能解释得清的）：**先看是不是图注里写的**（图注贴着引脚图，
    比正文里顺嘴提一句可靠），再看**出现次数**，最后按首次出现的位置（= 文档
    顺序，靠前的更可能是这份手册的主角）。

    同名不同脚数的（`TSSOP16` 和 `TSSOP`）都保留 —— 它们是不同的候选。
    """
    order: dict[str, int] = {}
    weight: dict[str, list[int]] = {}
    for index, hint in enumerate(hints):
        label = hint.label
        if label not in order:
            order[label] = index
            weight[label] = [0, 0]          # [图注次数, 总次数]
        weight[label][1] += 1
        if hint.source == "caption":
            weight[label][0] += 1

    def rank(label: str) -> tuple[int, int, int]:
        caption_hits, total = weight[label]
        return (-caption_hits, -total, order[label])

    return sorted(order, key=rank)


def hinted_counts(hints: Iterable[PackageHint]) -> list[int]:
    """线索里出现过的所有脚数，升序去重。没有就返回空表。

    自检的用法是：**解析出的脚数只要落在这个集合里就不报错**。多封装手册
    （STM32 一份文档覆盖 LQFP32/48/64、WLCSP36）给出的是一组值，不是单值。
    """
    return sorted({h.pin_count for h in hints if h.pin_count})


def hint_pages(pdf_path: str | Path, *, source: str = "text") -> list[int]:
    """扫**本地 PDF 文本层**，返回带封装线索的页码（1 起，升序）。

    为什么这件事归 `pkgname`：它才是「封装线索」这个概念的 owner，调用方
    （`pdf_locator.slim_pages`）只需要知道「哪几页值得送给 MinerU」，不必知道线索
    长什么样、正则怎么写。

    为什么要在**本地**扫而不是拿 MinerU 的结果：瘦身的意义就是「先决定送哪几页、
    再去解析」，此处的调用方正是在**决定之前**问的。本地文本层免费、不受 MinerU
    页数限制，拿它当召回依据刚好。

    **它是个超集**：本地扫描不认识 MinerU 的块类型，`_SKIP_TYPES` 挡掉的页眉页脚
    在这里照样算线索（页眉印个 `LQFP100` 就会记一页）。方向是对的 —— 多送几页只
    多花一点解析费，漏掉线索页会让脚数判据的数据源缺一块，凭空多出误报。

    代价：要读完整个文本层，1943 页那份实测约 5 秒。所以**调用方只应调一次**。
    """
    import pymupdf                       # 只为这一处引入：本模块其余部分是纯文本处理

    path = Path(pdf_path)
    pages: list[int] = []
    with pymupdf.open(path) as doc:
        for index in range(doc.page_count):
            text = doc[index].get_text("text")
            if text and _hints_in(index + 1, text, source):
                pages.append(index + 1)
    return pages


def describe_hints(hints: Iterable[PackageHint], limit: int = 6) -> str:
    """把线索拼成一句能放进 `Finding.detail` 的话，带页码和原文。

    例：`第 4 页「…packaged in a 16-lead TSSOP package…」；第 12 页「SSOP PACKAGE(TOP VIEW)」`
    """
    seen: set[tuple[str, int | None]] = set()
    parts: list[str] = []
    for hint in hints:
        key = (hint.style, hint.pin_count)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"第 {hint.page} 页「{hint.evidence}」")
        if len(parts) >= limit:
            break
    return "；".join(parts)
