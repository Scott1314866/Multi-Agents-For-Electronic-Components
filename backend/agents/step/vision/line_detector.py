"""OpenCV 尺寸线、尺寸界线、中心线和箭头候选检测。"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from backend.agents.step.vision.schemas import (
    ArrowEvidence,
    GeometryLine,
    OCRToken,
    TokenLineLink,
    ViewRegion,
)
from backend.agents.step.vision.view_splitter import region_for_bbox


def _point_distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _line_bbox(start: tuple[int, int], end: tuple[int, int]) -> tuple[int, int, int, int]:
    return min(start[0], end[0]), min(start[1], end[1]), max(start[0], end[0]), max(start[1], end[1])


def _distance_bbox_to_line(token: OCRToken, line: GeometryLine) -> float:
    token_center = ((token.bbox[0] + token.bbox[2]) / 2.0, (token.bbox[1] + token.bbox[3]) / 2.0)
    line_center = ((line.start[0] + line.end[0]) / 2.0, (line.start[1] + line.end[1]) / 2.0)
    return _point_distance(token_center, line_center)


def link_tokens_to_lines(
    tokens: list[OCRToken], lines: list[GeometryLine]
) -> list[TokenLineLink]:
    """根据 token 高度和中心距离建立可复现的文字—线段邻接关系。"""
    #  计算 自适应 距离 阈值
    token_heights = [max(1, token.bbox[3] - token.bbox[1]) for token in tokens]
    proximity = max(
        35.0,
        (float(np.median(token_heights)) if token_heights else 20.0) * 3.0,
    )
    links: list[TokenLineLink] = []
    for token in tokens:
        ranked = sorted(lines, key=lambda line: _distance_bbox_to_line(token, line))
        nearby = [
            line for line in ranked[:6]
            if _distance_bbox_to_line(token, line) <= proximity
        ]
        links.append(TokenLineLink(
            token_id=token.token_id,
            line_ids=[line.line_id for line in nearby],
            confidence=0.90 if nearby else 0.0,
        ))
    return links


def _detect_arrows(binary: np.ndarray) -> list[ArrowEvidence]:
    """用小型实心三角轮廓生成箭头候选。"""
    foreground = 255 - binary
    contours, _ = cv2.findContours(foreground, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    arrows: list[ArrowEvidence] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 12 or area > 900:
            continue
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.08 * perimeter, True)
        if not 3 <= len(polygon) <= 5:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        fill_ratio = area / float(max(1, w * h))
        if fill_ratio < 0.25:
            continue
        arrows.append(ArrowEvidence(
            arrow_id=f"arrow_{len(arrows) + 1:04d}",
            bbox=(x, y, x + w, y + h),
            center=(x + w / 2.0, y + h / 2.0),
            confidence=min(0.95, 0.55 + fill_ratio * 0.4),
        ))
    return arrows


def detect_geometry(
    binary_path: Path,
    tokens: list[OCRToken],
    regions: list[ViewRegion],
) -> tuple[list[GeometryLine], list[ArrowEvidence], list[TokenLineLink]]:
    """检测工程线段并建立 token-line 邻接关系。

    Args:
        binary_path: 预处理二值图。
        tokens: OCR token。
        regions: 候选视图区域。

    Returns:
        线段、箭头和 token-line 关联。
    """
    binary = cv2.imdecode(np.fromfile(binary_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if binary is None:
        raise ValueError(f"无法读取几何检测图片：{binary_path}")
    # Canny 边缘
    edges = cv2.Canny(binary, 50, 150, apertureSize=3)
    # HoughLinesP 检测线段
    raw = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=45,
        minLineLength=35,
        maxLineGap=8,
    )
    arrows = _detect_arrows(binary)
    lines: list[GeometryLine] = []
    for row in raw.reshape(-1, 4) if raw is not None else []:
        start = (int(row[0]), int(row[1]))
        end = (int(row[2]), int(row[3]))
        length = _point_distance(start, end)
        if length < 35:
            continue
        near_arrow_ids = [
            arrow.arrow_id
            for arrow in arrows
            if min(_point_distance(arrow.center, start), _point_distance(arrow.center, end)) <= 24.0
        ]
        horizontal = abs(end[1] - start[1]) <= max(3, abs(end[0] - start[0]) * 0.04)
        vertical = abs(end[0] - start[0]) <= max(3, abs(end[1] - start[1]) * 0.04)
        line_type = "line_segment"
        if len(near_arrow_ids) >= 1:
            line_type = "dimension_line"
        elif horizontal or vertical:
            line_type = "extension_line"
        bbox = _line_bbox(start, end)
        lines.append(GeometryLine(
            line_id=f"line_{len(lines) + 1:04d}",
            type=line_type,
            start=start,
            end=end,
            arrow_ids=near_arrow_ids,
            confidence=0.90 if line_type == "dimension_line" else 0.72,
            source_region_id=region_for_bbox(bbox, regions),
        ))
    return lines, arrows, link_tokens_to_lines(tokens, lines)
