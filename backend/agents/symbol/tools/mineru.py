"""② 文本通道：MinerU 云端 API 客户端（docs/specs/Module1-Spec.md §8.2）。

接口流程（v4）：
  1. POST /file-urls/batch   申请上传地址，拿到 batch_id 与 file_urls
  2. PUT  file_url           直传文件字节（不能带 Content-Type）
  3. GET  /extract-results/batch/{batch_id}   轮询直到 done
  4. 下载 full_zip_url，解出 full.md / content_list.json / layout.json / images/

结果按文件内容哈希缓存，重复跑同一份 PDF 不会再上传。

**瘦身上传（`parse(pages=[...])`）**：MinerU 对单文件有 200 页硬上限，超过**整个
拒收**（`number of pages exceeds limit (200 pages)`），一份 1943 页的手册连一行结果
都出不来。所以支持只上传「需要的那些页」：本地用 pymupdf 导成一个子集 PDF 再传。

子集会让页码语义整体偏移，而下游（`PackageHint.page`、`pipeline._pin_table`、
`Discovery.pin_pages`、`Source.pages`）**全部按原 PDF 页号思考**。所以页码映射
**统一在这一层还原** —— `content_list` 里每个块的 `page_idx` 出这扇门时一定是
原 PDF 的 0 起页号，下游一行都不用改。映射用的页集落在 `work_dir/pages.json`，
`_load` 自己读，**不接受调用方重算的列表**（否则选择算法一变，就会拿新列表去
重映射旧缓存，页号静默错位 —— 比缓存未命中危险得多）。
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pymupdf
import requests

from backend.agents.symbol import config

TERMINAL_STATES = {"done", "failed"}

# 服务端对单文件的硬上限。超了整个拒收，所以「退回全档重跑」这条路只在这个页数
# 以内才走得通（见 `pipeline._retry_full_document`）。
MINERU_PAGE_LIMIT = 200

# 页码映射的口径版本。**页集哈希必须覆盖它** —— 改了口径（比如以后不是简单的一对一
# 替换），旧缓存里的 `page_idx` 就是按老口径写的，得让缓存键跟着变。
PAGE_MAP_VERSION = "v1"


class MineruError(RuntimeError):
    pass


class MineruOfflineError(MineruError):
    """离线模式（`parse(..., allow_upload=False)`）下缓存没命中。

    单独一个类型，是为了让离线飞轮能把「这份没缓存、跳过」和「这份跑挂了」分开报
    —— 前者是**预期内的**，不该被算成失败，更不该悄悄变成一次真实上传。
    """


@dataclass
class MineruResult:
    """一次解析的产物。

    **哪些字段是「原 PDF 坐标系」**：`content_list` 的 `page_idx` —— 瘦身路径下
    已经还原成原 PDF 的 0 起页号，和全档解析的结果没有区别。

    **哪些不是**：`markdown` / `layout` / `images`。它们是子集 PDF 的直接产物，
    坐标系是**子集**的（第 n 页 = 送上去的第 n 页，不是原 PDF 的第 n 页）。
    目前三者都没有下游消费者（`images` 只被打印个数，渲染走 `render.render_page`
    拿原 PDF + 原页号），所以放着没坏；但将来谁要用 `layout` 的 bbox 做「证据可
    回溯到图号」，**必须先按 `page_map` 换算**，否则静默错位。
    """

    pdf: Path
    work_dir: Path
    markdown: str = ""
    content_list: list[dict[str, Any]] = field(default_factory=list)
    layout: dict[str, Any] = field(default_factory=dict)
    images: list[Path] = field(default_factory=list)
    from_cache: bool = False
    # 瘦身解析时用的是哪几页（原 PDF 的 1 起页号，**有序**）；全档解析时为 None。
    page_map: list[int] | None = None
    # 映射过程中必须让人看见的事（如越界块被跳过）。绝不静默吞掉。
    notes: list[str] = field(default_factory=list)

    def text_on_page(self, page_index: int) -> str:
        """取某一页的纯文本（page_index 从 0 开始，与 content_list 的 page_idx 对齐）。"""
        chunks = [
            item.get("text", "")
            for item in self.content_list
            if item.get("page_idx") == page_index and item.get("text")
        ]
        return "\n".join(chunks)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _pages_key(pages: Sequence[int]) -> str:
    """瘦身页集的缓存键后缀。**覆盖有序列表**，不是集合 —— 顺序不同，映射就不同。"""
    raw = PAGE_MAP_VERSION + "|" + ",".join(str(int(p)) for p in pages)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _subset_pdf(src_path: Path, pages: Sequence[int], dest: Path) -> None:
    """把 `pages`（原 PDF 的 1 起页号）导成一个子集 PDF。

    逐页插入，不是一次 `insert_pdf(from_page=min, to_page=max)`：页集是乱序的、
    不连续的（引脚段 + 余量环 + 线索页 + 前 3 页 + 封装段），而一次调用只能给
    一个连续区间。

    **页数必须逐一对上**，否则回来做页码映射时整体错位，而且是静默错位 ——
    在这里当场比一次，不等下载回来才发现。
    """
    with pymupdf.open(src_path) as src:
        out = pymupdf.open()
        try:
            for page in pages:
                out.insert_pdf(src, from_page=page - 1, to_page=page - 1)
            if out.page_count != len(pages):
                raise MineruError(
                    f"子集导出页数不符：要 {len(pages)} 页、实得 {out.page_count} 页"
                )
            # garbage/deflate 不能省：实测 RP2040 不加会把 4.69 MB 撑到 7.26 MB，
            # 加了变 1.12 MB。离 MinerU 的 200 MB 上限虽远，但白省的白省。
            out.save(dest, garbage=4, deflate=True)
        finally:
            out.close()


def _remap_page_idx(
    blocks: list[dict[str, Any]], pages: Sequence[int], notes: list[str]
) -> None:
    """把 `content_list` 的 `page_idx` 由「子集序号(0 起)」改回「原页号(0 起)」。

    越界的块**不猜**：原样留着并记一条 note。它匹配不上任何真实页，因此是惰性的，
    但「有东西没对上」这件事必须看得见 —— 静默丢块比留个明显的空转块更糟。
    """
    bad: list[int] = []
    for block in blocks:
        index = block.get("page_idx")
        if not isinstance(index, int):
            continue
        if 0 <= index < len(pages):
            block["page_idx"] = pages[index] - 1
        else:
            bad.append(index)
    if bad:
        notes.append(
            f"瘦身回映射：{len(bad)} 个块的 page_idx 越界（{sorted(set(bad))[:5]}），"
            f"已跳过映射，这些块不属于任何原页"
        )


class MineruClient:
    """MinerU 云端解析客户端。令牌只在内存里，不落盘、不打印。"""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = config.MINERU_BASE_URL,
        cache_dir: Path | None = None,
        timeout: int = 60,
    ) -> None:
        self._token = token or config.mineru_token()
        self._base = base_url.rstrip("/")
        self._cache_dir = cache_dir or (config.PROJECT_ROOT / ".cache" / "mineru")
        self._timeout = timeout

    # ------------------------------------------------------------ HTTP
    def _headers(self, json_body: bool = True) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _check(self, payload: dict[str, Any], what: str) -> dict[str, Any]:
        if payload.get("code") not in (0, None):
            raise MineruError(f"{what} 失败：code={payload.get('code')} msg={payload.get('msg')}")
        return payload.get("data") or {}

    # ------------------------------------------------------------ 1+2 提交
    def submit(
        self,
        pdf_path: Path,
        *,
        is_ocr: bool = True,
        enable_table: bool = True,
        enable_formula: bool = False,
        language: str = "ch",
        data_id: str | None = None,
    ) -> str:
        """申请上传地址 + 直传文件，返回 batch_id。

        `data_id` 默认取文件内容哈希，但**瘦身路径必须显式传**：那时候 `pdf_path`
        是临时目录里的子集，pymupdf 每次导出的字节都不一样（实测同一份源文件、
        同样的页码，两次 `save` 出来的 sha256 不同），拿它当 id 等于每次都在变。
        传进来的应该是「原文件哈希 + 页集哈希」。
        """
        pdf_path = Path(pdf_path)
        body = {
            "enable_formula": enable_formula,
            "enable_table": enable_table,
            "language": language,
            "files": [
                {
                    "name": pdf_path.name,
                    "is_ocr": is_ocr,
                    "data_id": data_id or _file_digest(pdf_path),
                }
            ],
        }
        response = requests.post(
            f"{self._base}/file-urls/batch",
            headers=self._headers(),
            json=body,
            timeout=self._timeout,
        )
        response.raise_for_status()
        data = self._check(response.json(), "申请上传地址")

        batch_id = data.get("batch_id")
        urls = data.get("file_urls") or []
        if not batch_id or not urls:
            raise MineruError(f"申请上传地址返回异常：{data}")

        with pdf_path.open("rb") as handle:
            upload = requests.put(urls[0], data=handle, timeout=300)
        if upload.status_code not in (200, 201):
            raise MineruError(
                f"上传失败：HTTP {upload.status_code} {upload.text[:200]}"
            )
        return batch_id

    # ------------------------------------------------------------ 3 轮询
    def wait(
        self,
        batch_id: str,
        *,
        poll_interval: float = 5.0,
        timeout: float = 600.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            response = requests.get(
                f"{self._base}/extract-results/batch/{batch_id}",
                headers=self._headers(json_body=False),
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = self._check(response.json(), "查询解析结果")
            results = data.get("extract_result") or []
            if results:
                item = results[0]
                state = item.get("state")
                if state == "failed":
                    raise MineruError(f"解析失败：{item.get('err_msg', '未知原因')}")
                if state in TERMINAL_STATES:
                    return item
            if time.monotonic() >= deadline:
                raise MineruError(f"解析超时（>{timeout:.0f}s），batch_id={batch_id}")
            time.sleep(poll_interval)

    # ------------------------------------------------------------ 4 下载解包
    @staticmethod
    def _fetch(url: str, timeout: int = 300) -> bytes:
        """下载字节。MinerU 的 CDN 对 Python 默认 TLS 指纹会直接断连（curl 正常），
        所以 SSLError 时退回系统 curl。"""
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.content
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError) as exc:
            import shutil
            import subprocess
            import tempfile

            curl = shutil.which("curl")
            if not curl:
                raise MineruError(f"下载失败且系统没有 curl：{exc}") from exc

            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "download.zip"
                proc = subprocess.run(
                    [curl, "-fsSL", "--max-time", str(timeout), "-o", str(out), url],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0 or not out.exists():
                    raise MineruError(
                        f"curl 下载失败（exit={proc.returncode}）：{proc.stderr[:200]}"
                    ) from exc
                return out.read_bytes()

    def download(self, zip_url: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(self._fetch(zip_url))) as archive:
            archive.extractall(dest_dir)
        return dest_dir

    # ------------------------------------------------------------ 一站式
    def parse(
        self,
        pdf_path: Path,
        *,
        pages: Sequence[int] | None = None,
        use_cache: bool = True,
        allow_upload: bool = True,
        **options: Any,
    ) -> MineruResult:
        """解析一份 PDF；`pages` 给了就只解析这几页（原 PDF 的 1 起页号）。

        **`pages` 必须是显式关键字参数**，不能靠 `**options` 转给 `submit()` ——
        `submit` 不认识它，会直接 TypeError。

        缓存查找顺序是「**全档优先，瘦身兜底**」：语料里可能已经有一份花过钱的
        全档缓存，它的内容是瘦身结果的**超集**，直接用只会更准、不花钱。这条不算
        「所有 PDF 都瘦身」的例外 —— 它不改行为，只是不浪费已经买回来的结果。

        `allow_upload=False` 把「没命中缓存就上传」这条路堵死，换成
        `MineruOfflineError` —— 离线飞轮靠它保证一分钱不花。**这是结构性保证，
        不是靠调用方自觉**：`flywheel.cached()` 那种"盘上有目录就算有缓存"的判断
        是乐观的（瘦身键取决于页集，页集要跑 locate 才知道），拦不住钱。
        """
        pdf_path = Path(pdf_path)
        digest = _file_digest(pdf_path)
        wanted = self._clean_pages(pdf_path, pages)

        full_dir = self._cache_dir / digest
        key = f"{digest}-s{_pages_key(wanted)}" if wanted else digest
        slim_dir = self._cache_dir / key

        if use_cache:
            if (full_dir / ".complete").exists():
                return self._load(pdf_path, full_dir, from_cache=True)
            if (slim_dir / ".complete").exists():
                return self._load(pdf_path, slim_dir, from_cache=True)

        if not allow_upload:
            where = f"全档或瘦身页集 {len(wanted)} 页" if wanted else "全档"
            raise MineruOfflineError(
                f"离线模式：{pdf_path.name} 的{where}不在缓存里，跳过（不联网、不花钱）"
            )

        if wanted:
            # 临时文件只包住 submit：它内部是「申请地址 → PUT → 返回 batch_id」，
            # 返回时文件就不再需要了。放 with 里，异常路径也会清干净。
            # **绝不落进 .cache/mineru/** —— 会污染 flywheel 的缓存判据。
            # ignore_cleanup_errors：Windows 上若还有句柄没放，清理会抛异常，那就把
            # 真正的解析错误盖掉了 —— 清理失败不值得让整单报错。
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                subset = Path(tmp) / f"{pdf_path.stem}_p{len(wanted)}.pdf"
                _subset_pdf(pdf_path, wanted, subset)
                # name 用子集自己的文件名：MinerU 控制台里一眼能看出「这是哪份手册的
                # 多少页子集」。_load 按后缀 glob 找结果，文件名不影响解包。
                batch_id = self.submit(subset, data_id=key, **options)
            work_dir = slim_dir
        else:
            batch_id = self.submit(pdf_path, data_id=key, **options)
            work_dir = full_dir

        item = self.wait(batch_id)
        zip_url = item.get("full_zip_url")
        if not zip_url:
            raise MineruError(f"解析完成但没有 full_zip_url：{item}")

        self.download(zip_url, work_dir)
        if wanted:
            # 映射用的页集落盘，_load 从盘上读 —— 这样「选择算法改了」不会拿新
            # 列表去重映射旧缓存（那是静默错位，比缓存未命中危险得多）。
            (work_dir / "pages.json").write_text(
                json.dumps(list(wanted)), encoding="utf-8"
            )
        (work_dir / ".complete").write_text(batch_id, encoding="utf-8")
        return self._load(pdf_path, work_dir, from_cache=False)

    @staticmethod
    def _clean_pages(pdf_path: Path, pages: Sequence[int] | None) -> list[int]:
        """去重、限界，**保持给定顺序**（顺序就是判据，见 `pdf_locator.slim_pages`）。"""
        if not pages:
            return []
        with pymupdf.open(pdf_path) as doc:
            total = doc.page_count
        out: list[int] = []
        for page in pages:
            page = int(page)
            if 1 <= page <= total and page not in out:
                out.append(page)
        return out

    @staticmethod
    def _load(pdf_path: Path, work_dir: Path, *, from_cache: bool) -> MineruResult:
        def find(*patterns: str) -> Path | None:
            """解包出来的文件名带 batch uuid 前缀，所以按后缀找。"""
            for pattern in patterns:
                direct = work_dir / pattern
                if direct.exists():
                    return direct
                matches = sorted(work_dir.glob(f"*{pattern}"))
                if matches:
                    return matches[0]
            return None

        def read_json(*patterns: str) -> Any:
            path = find(*patterns)
            if path is None:
                return {}
            return json.loads(path.read_text(encoding="utf-8"))

        markdown = ""
        md_path = find("full.md")
        if md_path is not None:
            markdown = md_path.read_text(encoding="utf-8")

        # 瘦身目录里有一份 pages.json。**从盘上读，不要调用方传** —— 那是这一份
        # 缓存写的时候用的页集；拿调用方现在算出来的列表去映射，选择算法一变就错位。
        notes: list[str] = []
        page_map: list[int] | None = None
        pages_path = work_dir / "pages.json"
        if pages_path.exists():
            try:
                page_map = [int(p) for p in json.loads(pages_path.read_text(encoding="utf-8"))]
            except (OSError, ValueError, TypeError):
                page_map = None
                notes.append("pages.json 读不出来，content_list 的 page_idx 是瘦身后的页号")

        content_list = read_json("content_list.json") or []
        if page_map:
            _remap_page_idx(content_list, page_map, notes)

        images_dir = work_dir / "images"
        return MineruResult(
            pdf=pdf_path,
            work_dir=work_dir,
            markdown=markdown,
            content_list=content_list,
            layout=read_json("layout.json") or {},
            images=sorted(images_dir.glob("*")) if images_dir.exists() else [],
            from_cache=from_cache,
            page_map=page_map,
            notes=notes,
        )


def main(argv: list[str]) -> int:
    import sys

    if len(argv) < 2:
        print("用法：python -m app.mineru <datasheet.pdf> [--pages 3,4,5] [页码...]")
        print("  注意 MinerU 对单文件有 200 页硬上限，超了整个拒收；")
        print("  超过 200 页的用 --pages 指定要哪几页（主流程由 pdf_locator.slim_pages 决定）。")
        return 2

    pages: list[int] | None = None
    rest = list(argv[2:])
    if "--pages" in rest:
        at = rest.index("--pages")
        pages = [int(x) for x in rest[at + 1].split(",")]
        del rest[at : at + 2]

    result = MineruClient().parse(Path(argv[1]), pages=pages)
    print(f"缓存目录 : {result.work_dir}（{'命中缓存' if result.from_cache else '本次新解析'}）")
    print(f"markdown : {len(result.markdown)} 字符")
    print(f"内容块   : {len(result.content_list)}")
    print(f"图片     : {len(result.images)}")
    if result.page_map:
        print(f"瘦身页集 : {result.page_map}（已还原成原 PDF 页号）")
    for note in result.notes:
        print(f"  · {note}")
    argv = [argv[0], argv[1], *rest]

    for raw in argv[2:]:
        page_index = int(raw) - 1
        print(f"\n===== 第 {raw} 页 =====")
        for block in result.content_list:
            if block.get("page_idx") != page_index:
                continue
            kind = block.get("type")
            text = block.get("text") or block.get("table_body") or ""
            caption = block.get("table_caption") or block.get("image_caption") or []
            print(f"[{kind}] {' / '.join(caption)}")
            if text:
                print(str(text)[:400])
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
