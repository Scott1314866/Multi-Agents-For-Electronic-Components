"""基于连通区域投影切分工程图候选视图。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from backend.agents.step.vision.schemas import OCRToken, ViewRegion


def _horizontal_band_regions(binary: np.ndarray) -> list[tuple[int, int, int, int]]:
    """在页面外框吞并连通域时，按水平留白恢复内容区域。

    该回退只读取二值像素，不识别器件型号或尺寸值。页面左右各忽略少量
    边缘像素，避免整页图框让每一行都被误判为有内容。
    """
    height, width = binary.shape
    foreground = binary < 128
    margin_x = max(2, int(round(width * 0.02)))
    inner = foreground[:, margin_x : max(margin_x + 1, width - margin_x)]
    active = inner.sum(axis=1) > max(8, int(round(width * 0.01)))

    raw_bands: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(active):
        if value and start is None:
            start = index
        if start is not None and (not value or index == height - 1):
            end = index if not value else index + 1
            if end - start >= 5:
                raw_bands.append((start, end))
            start = None
    if not raw_bands:
        return []

    merge_gap = max(30, int(round(height * 0.04)))
    merged: list[list[int]] = []
    for start, end in raw_bands:
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1][1] = end
        else:
            merged.append([start, end])

    minimum_height = max(40, int(round(height * 0.04)))
    padding = max(8, int(round(min(height, width) * 0.01)))
    boxes: list[tuple[int, int, int, int]] = []
    for start, end in merged:
        if end - start < minimum_height:
            continue
        band = foreground[start:end]
        ys, xs = np.where(band)
        if xs.size == 0:
            continue
        x1 = max(0, int(xs.min()) - padding)
        x2 = min(width, int(xs.max()) + 1 + padding)
        y1 = max(0, start - padding)
        y2 = min(height, end + padding)
        boxes.append((x1, y1, x2, y2))
    return boxes


def split_view_regions(binary_path: Path) -> list[ViewRegion]:
    """从二值工程图中提取不依赖器件类型的候选视图区域。

    Args:
        binary_path: 预处理后的黑字白底二值图。

    Returns:
        按从上到下、从左到右排序的区域列表。
    """
    binary = cv2.imdecode(np.fromfile(binary_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if binary is None:
        raise ValueError(f"无法读取二值图片：{binary_path}")
    foreground = 255 - binary
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (35, 21))
    merged = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    height, width = binary.shape
    image_area = float(width * height)
    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area_ratio = (w * h) / image_area
        if area_ratio < 0.002 or area_ratio > 0.55:
            continue
        if w < 80 or h < 40:
            continue
        boxes.append((x, y, x + w, y + h))
    if not boxes:
        boxes = _horizontal_band_regions(binary)
    if not boxes:
        # 极端情况下仍保留整张工程图作为一个可审计候选区域，不能让后续
        # 节点因空队列直接失败。
        boxes = [(0, 0, width, height)]
    boxes.sort(key=lambda item: (item[1] // max(1, height // 12), item[0]))
    return [
        ViewRegion(
            region_id=f"region_{index:03d}",
            bbox=box,
            area_ratio=((box[2] - box[0]) * (box[3] - box[1])) / image_area,
        )
        for index, box in enumerate(boxes, start=1)
    ]


def render_region_overview(
    binary_path: Path,
    regions: list[ViewRegion],
    output_path: Path,
) -> Path:
    """生成仅用于第一阶段视图识别的区域编号总览图。

    Args:
        binary_path: 与区域坐标同尺度的预处理二值图。
        regions: 本地算法切分出的候选区域。
        output_path: 总览 PNG 保存路径。

    Returns:
        已写入的总览图绝对路径。

    Raises:
        ValueError: 二值图无法读取或 PNG 编码失败时抛出。
    """
    binary = cv2.imdecode(np.fromfile(binary_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if binary is None:
        raise ValueError(f"无法读取区域总览底图：{binary_path}")
    canvas = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    font_scale = max(0.55, min(canvas.shape[:2]) / 1800.0)
    thickness = max(2, int(round(font_scale * 3)))
    for region in regions:
        x1, y1, x2, y2 = region.bbox
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (0, 0, 255), thickness)
        label = region.region_id
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
        )
        label_y = max(text_height + baseline + 4, y1)
        cv2.rectangle(
            canvas,
            (x1, label_y - text_height - baseline - 4),
            (x1 + text_width + 8, label_y + 4),
            (255, 255, 255),
            -1,
        )
        cv2.putText(
            canvas,
            label,
            (x1 + 4, label_y - baseline),
            cv2.FONT_HERSHEY_SIMPLEX,
            font_scale,
            (0, 0, 255),
            thickness,
            cv2.LINE_AA,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", canvas)
    if not ok:
        raise ValueError("区域编号总览图编码失败")
    encoded.tofile(output_path)
    return output_path.resolve()


def render_region_crop(
    binary_path: Path,
    region: ViewRegion,
    output_path: Path,
) -> Path:
    """保存当前候选区域裁剪图，供逐视图语义节点读取。

    Args:
        binary_path: 唯一输入图产生的预处理二值图。
        region: 当前视图区域及其 bbox。
        output_path: 裁剪 PNG 输出路径。

    Returns:
        已写入的裁剪图绝对路径。

    失败状态:
        图片不可读、bbox 无效或编码失败时抛出 ``ValueError``。
    """
    binary = cv2.imdecode(np.fromfile(binary_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if binary is None:
        raise ValueError(f"无法读取视图裁剪底图：{binary_path}")
    height, width = binary.shape
    x1, y1, x2, y2 = region.bbox
    x1, x2 = max(0, x1), min(width, x2)
    y1, y2 = max(0, y1), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"无效视图区域：{region.region_id}")
    crop = binary[y1:y2, x1:x2]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        raise ValueError("视图裁剪图编码失败")
    encoded.tofile(output_path)
    return output_path.resolve()


def region_for_bbox(
    bbox: tuple[int, int, int, int], regions: list[ViewRegion]
) -> str:
    """按 token 中心点选择所属候选区域。"""
    center_x = (bbox[0] + bbox[2]) / 2.0
    center_y = (bbox[1] + bbox[3]) / 2.0
    containing = [
        region
        for region in regions
        if region.bbox[0] <= center_x <= region.bbox[2]
        and region.bbox[1] <= center_y <= region.bbox[3]
    ]
    if not containing:
        return "unassigned"
    containing.sort(key=lambda item: item.area_ratio)
    return containing[0].region_id


def ensure_region_coverage(
    regions: list[ViewRegion],
    tokens: list[OCRToken],
    image_size: tuple[int, int],
    *,
    minimum_coverage: float = 0.65,
) -> list[ViewRegion]:
    """在局部切分遗漏主体时增加一个全页复合视图候选。

    Args:
        regions: OpenCV 已切分的候选区域。
        tokens: 唯一输入图产生的 OCR token。
        image_size: 预处理图的宽、高。
        minimum_coverage: 可靠 OCR 中心点应被局部区域覆盖的最低比例。

    Returns:
        覆盖充分时原样返回；否则追加 ``region_document``。该区域只表示
        “整页仍需理解”，不携带器件族或任何尺寸语义。
    """
    reliable = [token for token in tokens if token.confidence >= 0.55]
    if not reliable or any(region.area_ratio >= 0.75 for region in regions):
        return regions
    covered = sum(
        region_for_bbox(token.bbox, regions) != "unassigned" for token in reliable
    )
    if covered / len(reliable) >= minimum_coverage:
        return regions
    width, height = image_size
    fallback = ViewRegion(
        region_id="region_document",
        bbox=(0, 0, width, height),
        area_ratio=1.0,
    )
    return [*regions, fallback]
