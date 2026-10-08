"""② 视觉通道第一步：把目标页渲染成整页 PNG（docs/specs/Module1-Spec.md §8.2 ①）。

MinerU 云端只回结构化文本和裁剪图，不给整页渲染，所以整页图必须本地出。
渲染结果按「文件哈希 + 页码 + DPI」缓存，避免重复渲染。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from backend.agents.symbol import config

DEFAULT_DPI = 300          # 引脚图上的细线在小 DPI 下会糊
RENDER_CACHE = config.PROJECT_ROOT / ".cache" / "render"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


@dataclass
class RenderedPage:
    page: int          # 1 起
    path: Path
    width: int
    height: int
    dpi: int


def render_page(
    pdf_path: str | Path,
    page: int,
    *,
    dpi: int = DEFAULT_DPI,
    out_dir: Path | None = None,
    use_cache: bool = True,
) -> RenderedPage:
    """把第 page 页（1 起）渲染成 PNG。"""
    pdf_path = Path(pdf_path)
    if page < 1:
        raise ValueError("页码从 1 开始")

    out_dir = out_dir or (RENDER_CACHE / _digest(pdf_path))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"p{page}@{dpi}.png"

    with pymupdf.open(pdf_path) as doc:
        if page > doc.page_count:
            raise ValueError(f"{pdf_path.name} 只有 {doc.page_count} 页，取不到第 {page} 页")
        if use_cache and target.exists():
            rect = doc[page - 1].rect
            zoom = dpi / 72
            return RenderedPage(
                page=page,
                path=target,
                width=round(rect.width * zoom),
                height=round(rect.height * zoom),
                dpi=dpi,
            )

        pixmap = doc[page - 1].get_pixmap(dpi=dpi)
        pixmap.save(target)
        return RenderedPage(
            page=page, path=target, width=pixmap.width, height=pixmap.height, dpi=dpi
        )


def render_pages(
    pdf_path: str | Path,
    pages: list[int],
    *,
    dpi: int = DEFAULT_DPI,
    out_dir: Path | None = None,
    use_cache: bool = True,
) -> list[RenderedPage]:
    return [
        render_page(pdf_path, page, dpi=dpi, out_dir=out_dir, use_cache=use_cache)
        for page in pages
    ]


def render_region(
    pdf_path: str | Path,
    page: int,
    bbox: list[float],
    *,
    dpi: int = DEFAULT_DPI,
    out_dir: Path | None = None,
) -> RenderedPage:
    """渲染页面上的一块区域（bbox 为 PDF 点坐标 [x0,y0,x1,y1]），用于放大看引脚图。"""
    pdf_path = Path(pdf_path)
    out_dir = out_dir or (RENDER_CACHE / _digest(pdf_path))
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "-".join(str(round(v)) for v in bbox)
    target = out_dir / f"p{page}_crop{tag}@{dpi}.png"

    with pymupdf.open(pdf_path) as doc:
        clip = pymupdf.Rect(*bbox)
        pixmap = doc[page - 1].get_pixmap(dpi=dpi, clip=clip)
        pixmap.save(target)
        return RenderedPage(
            page=page, path=target, width=pixmap.width, height=pixmap.height, dpi=dpi
        )


_FIG_CAPTION_RE = re.compile(
    r"(?:图|圖|Figure|FIGURE|Fig\.)\s*\d+\s*[-–—.]\s*\d+", re.IGNORECASE
)
_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\s+\S")


def _page_lines(page: pymupdf.Page) -> list[tuple[float, float, str]]:
    """返回页面上每一行的 (y0, y1, 文本)。"""
    lines: list[tuple[float, float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append((line["bbox"][1], line["bbox"][3], text))
    return lines


def figure_band(pdf_path: str | Path, page: int) -> pymupdf.Rect | None:
    """圈出「章节标题 → 最后一条图注」之间的区域，即引脚图所在的那条带。

    用本页自己的矢量/文本坐标算，不用 MinerU 的 bbox —— 后者是另一套
    归一化坐标系，直接当 PDF 裁剪框用会错位。
    """
    pdf_path = Path(pdf_path)
    with pymupdf.open(pdf_path) as doc:
        if page < 1 or page > doc.page_count:
            raise ValueError(f"页码 {page} 超出范围")
        pdf_page = doc[page - 1]
        lines = _page_lines(pdf_page)
        if not lines:
            return None

        caption_ys = [y1 for y0, y1, text in lines if _FIG_CAPTION_RE.search(text)]
        if not caption_ys:
            return None
        caption_bottom = max(caption_ys)

        heading_ys = [
            y0 for y0, _, text in lines if _HEADING_RE.match(text) and y0 < min(caption_ys)
        ]
        top = max(heading_ys) if heading_ys else 0.0

        band = pymupdf.Rect(0, top - 4, pdf_page.rect.width, caption_bottom + 4)

        # 收紧到实际的图形范围，但保留文字标签（引脚名在图外）
        drawing_rect = pymupdf.Rect()
        for item in pdf_page.get_drawings():
            rect = item["rect"]
            if rect.y0 >= band.y0 and rect.y1 <= band.y1 and rect.width < band.width:
                drawing_rect |= rect
        if drawing_rect.is_empty:
            return band
        region = pymupdf.Rect(
            drawing_rect.x0 - 60,
            max(band.y0, drawing_rect.y0 - 20),
            drawing_rect.x1 + 60,
            min(band.y1, drawing_rect.y1 + 20),
        )
        return region & pdf_page.rect


def render_figure_band(
    pdf_path: str | Path, page: int, *, dpi: int = DEFAULT_DPI
) -> RenderedPage | None:
    """渲染引脚图所在的那条带，用于视觉通道看细节。"""
    region = figure_band(pdf_path, page)
    if region is None:
        return None
    return render_region(
        pdf_path, page, [region.x0, region.y0, region.x1, region.y1], dpi=dpi
    )


def main(argv: list[str]) -> int:
    import sys

    if len(argv) < 3:
        print("用法：python -m app.render <datasheet.pdf> <页码...>")
        return 2
    for raw in argv[2:]:
        rendered = render_page(argv[1], int(raw))
        print(f"第 {rendered.page} 页 -> {rendered.path} ({rendered.width}x{rendered.height})")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
