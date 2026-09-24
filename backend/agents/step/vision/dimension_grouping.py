"""原始线段归一化与尺寸证据组构建。"""

from __future__ import annotations

import math
import re
from statistics import median

from backend.agents.step.vision.line_detector import link_tokens_to_lines
from backend.agents.step.vision.ocr import parse_explicit_count_expression
from backend.agents.step.vision.schemas import (
    DimensionGroup,
    GeometryLine,
    OCRToken,
    TokenLineLink,
    ViewRegion,
)
from backend.agents.step.vision.view_splitter import region_for_bbox


_DIMENSION_TEXT = re.compile(
    r"[ØΦ±]|REF|UNC|\([^)]*\d[^)]*\)|(?:^|\D)[.]?\d+(?:\.\d+)?",
    re.I,
)
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"


def _orientation(line: GeometryLine) -> str:
    dx = line.end[0] - line.start[0]
    dy = line.end[1] - line.start[1]
    if abs(dy) <= max(4, abs(dx) * 0.05):
        return "horizontal"
    if abs(dx) <= max(4, abs(dy) * 0.05):
        return "vertical"
    return "oblique"


def _interval(line: GeometryLine, orientation: str) -> tuple[int, int, float]:
    if orientation == "horizontal":
        return min(line.start[0], line.end[0]), max(line.start[0], line.end[0]), (
            line.start[1] + line.end[1]
        ) / 2.0
    return min(line.start[1], line.end[1]), max(line.start[1], line.end[1]), (
        line.start[0] + line.end[0]
    ) / 2.0


def normalize_line_segments(lines: list[GeometryLine]) -> list[GeometryLine]:
    """合并近共线且区间重叠的 Hough 线段，保留箭头和最高置信度。"""
    merged: list[GeometryLine] = []
    for source in sorted(lines, key=lambda item: -math.dist(item.start, item.end)):
        orientation = _orientation(source)
        if orientation == "oblique":
            merged.append(source)
            continue
        start, end, axis = _interval(source, orientation)
        target_index = None
        for index, target in enumerate(merged):
            if _orientation(target) != orientation:
                continue
            other_start, other_end, other_axis = _interval(target, orientation)
            if abs(axis - other_axis) <= 5 and start <= other_end + 12 and end >= other_start - 12:
                target_index = index
                break
        if target_index is None:
            merged.append(source)
            continue
        target = merged[target_index]
        other_start, other_end, other_axis = _interval(target, orientation)
        low, high = min(start, other_start), max(end, other_end)
        fixed = int(round((axis + other_axis) / 2.0))
        new_start = (low, fixed) if orientation == "horizontal" else (fixed, low)
        new_end = (high, fixed) if orientation == "horizontal" else (fixed, high)
        arrows = list(dict.fromkeys([*target.arrow_ids, *source.arrow_ids]))
        line_type = "dimension_line" if arrows else (
            "extension_line" if "extension_line" in (target.type, source.type) else target.type
        )
        merged[target_index] = GeometryLine(
            line_id=target.line_id,
            type=line_type,
            start=new_start,
            end=new_end,
            arrow_ids=arrows,
            confidence=max(target.confidence, source.confidence),
            source_region_id=target.source_region_id,
        )
    return [
        line.model_copy(update={"line_id": f"line_{index:04d}"})
        for index, line in enumerate(merged, start=1)
    ]


def assign_evidence_to_views(
    tokens: list[OCRToken],
    lines: list[GeometryLine],
    regions: list[ViewRegion],
) -> tuple[list[OCRToken], list[GeometryLine]]:
    """在视图检测后，为全量 token 和归一化线段补充区域归属。"""
    assigned_tokens = [
        token.model_copy(update={"source_region_id": region_for_bbox(token.bbox, regions)})
        for token in tokens
    ]
    assigned_lines = []
    for line in lines:
        bbox = (
            min(line.start[0], line.end[0]),
            min(line.start[1], line.end[1]),
            max(line.start[0], line.end[0]),
            max(line.start[1], line.end[1]),
        )
        assigned_lines.append(line.model_copy(
            update={"source_region_id": region_for_bbox(bbox, regions)}
        ))
    return assigned_tokens, assigned_lines


def build_dimension_groups(
    tokens: list[OCRToken],
    lines: list[GeometryLine],
    links: list[TokenLineLink] | None = None,
) -> tuple[list[DimensionGroup], list[TokenLineLink]]:
    """把每个尺寸文字及其邻近尺寸线、界线和箭头归并为可检索证据组。"""
    links = links or link_tokens_to_lines(tokens, lines)
    line_index = {line.line_id: line for line in lines}
    groups: list[DimensionGroup] = []
    for token in tokens:
        if token.confidence < 0.55:
            continue
        if parse_explicit_count_expression(token.text) is not None:
            groups.append(DimensionGroup(
                dimension_id=f"dim_{len(groups) + 1:04d}",
                view_id=token.source_region_id,
                evidence_type="identity_text",
                bbox=token.bbox,
                token_ids=[token.token_id],
                orientation="horizontal",
                confidence=token.confidence,
            ))
            continue
        if not _DIMENSION_TEXT.search(token.text):
            continue
        overlapping_horizontal = [
            other
            for other in tokens
            if token.rotation_deg != 0
            and other.rotation_deg == 0
            and other.token_id != token.token_id
            and other.source_region_id == token.source_region_id
            and max(0, min(token.bbox[2], other.bbox[2]) - max(token.bbox[0], other.bbox[0]))
            * max(0, min(token.bbox[3], other.bbox[3]) - max(token.bbox[1], other.bbox[1]))
            > 0
            and re.search(r"\d", other.text)
        ]
        if token.rotation_deg != 0 and len(overlapping_horizontal) >= 2:
            # 旋转 OCR 有时会跨两行横排尺寸，把“5,60 / 5,00”拼成“55”。
            # 同时覆盖两条横排数值时视为重复通道伪影，不进入尺寸组。
            continue
        token_area = max(
            1,
            (token.bbox[2] - token.bbox[0]) * (token.bbox[3] - token.bbox[1]),
        )
        is_fragment = any(
            other.token_id != token.token_id
            and other.source_region_id == token.source_region_id
            and other.confidence >= 0.80
            and len(other.text.strip()) > len(token.text.strip())
            and (
                max(
                    0,
                    min(token.bbox[2], other.bbox[2])
                    - max(token.bbox[0], other.bbox[0]),
                )
                * max(
                    0,
                    min(token.bbox[3], other.bbox[3])
                    - max(token.bbox[1], other.bbox[1]),
                )
            )
            / token_area
            >= 0.60
            for other in tokens
        )
        if is_fragment:
            continue
        link = next((item for item in links if item.token_id == token.token_id), None)
        linked = [
            line_index[line_id]
            for line_id in (link.line_ids if link else [])
            if line_id in line_index
        ]
        normalized_text = token.text.replace(",", ".").replace("−", "-")
        is_explicit_range = bool(re.fullmatch(
            rf"\s*{_NUMBER}\s*(?:°|deg)?\s*[-–—]\s*{_NUMBER}\s*(?:°|deg)?\s*",
            normalized_text,
            flags=re.I,
        ))
        if not linked and is_explicit_range:
            cx = (token.bbox[0] + token.bbox[2]) / 2.0
            cy = (token.bbox[1] + token.bbox[3]) / 2.0
            ranked = sorted(
                (
                    (
                        math.hypot(
                            cx - (line.start[0] + line.end[0]) / 2.0,
                            cy - (line.start[1] + line.end[1]) / 2.0,
                        ),
                        line,
                    )
                    for line in lines
                    if line.source_region_id == token.source_region_id
                ),
                key=lambda item: item[0],
            )
            if ranked and ranked[0][0] <= 140.0:
                linked = [ranked[0][1]]
                link = TokenLineLink(
                    token_id=token.token_id,
                    line_ids=[ranked[0][1].line_id],
                    confidence=0.75,
                )
                links = [
                    item for item in links if item.token_id != token.token_id
                ]
                links.append(link)
        if not linked:
            continue
        dimension_lines = [line for line in linked if line.type == "dimension_line"]
        extension_lines = [line for line in linked if line.type != "dimension_line"]
        selected = [*dimension_lines[:2], *extension_lines[:3]]
        xs = [token.bbox[0], token.bbox[2], *[p for line in selected for p in (line.start[0], line.end[0])]]
        ys = [token.bbox[1], token.bbox[3], *[p for line in selected for p in (line.start[1], line.end[1])]]
        primary = dimension_lines[0] if dimension_lines else selected[0]
        groups.append(DimensionGroup(
            dimension_id=f"dim_{len(groups) + 1:04d}",
            view_id=token.source_region_id,
            bbox=(min(xs), min(ys), max(xs), max(ys)),
            token_ids=[token.token_id],
            dimension_line_ids=[line.line_id for line in dimension_lines[:2]],
            extension_line_ids=[line.line_id for line in extension_lines[:3]],
            arrow_ids=list(dict.fromkeys(
                arrow_id for line in selected for arrow_id in line.arrow_ids
            )),
            orientation=_orientation(primary),
            confidence=min(token.confidence, link.confidence if link else 0.0),
        ))
    # 上下堆叠的极限尺寸通常共享同一组尺寸线。把它们合成一个证据组，
    # 让语义层能够按 MAX/MIN 规则选择单一 token，而不是误认为两个尺寸。
    merged_groups: list[DimensionGroup] = []
    for group in groups:
        key = (group.view_id, frozenset(group.dimension_line_ids))
        target_index = next((
            index
            for index, candidate in enumerate(merged_groups)
            if key == (candidate.view_id, frozenset(candidate.dimension_line_ids))
            and bool(group.dimension_line_ids)
            and max(group.bbox[0], candidate.bbox[0])
                <= min(group.bbox[2], candidate.bbox[2]) + 20
            and abs(
                (group.bbox[1] + group.bbox[3]) / 2.0
                - (candidate.bbox[1] + candidate.bbox[3]) / 2.0
            ) <= 90
        ), None)
        if target_index is None:
            merged_groups.append(group)
            continue
        candidate = merged_groups[target_index]
        merged_groups[target_index] = candidate.model_copy(update={
            "bbox": (
                min(candidate.bbox[0], group.bbox[0]),
                min(candidate.bbox[1], group.bbox[1]),
                max(candidate.bbox[2], group.bbox[2]),
                max(candidate.bbox[3], group.bbox[3]),
            ),
            "token_ids": list(dict.fromkeys([
                *candidate.token_ids, *group.token_ids,
            ])),
            "extension_line_ids": list(dict.fromkeys([
                *candidate.extension_line_ids, *group.extension_line_ids,
            ])),
            "arrow_ids": list(dict.fromkeys([
                *candidate.arrow_ids, *group.arrow_ids,
            ])),
            "confidence": min(candidate.confidence, group.confidence),
        })
    return merged_groups, links


def detect_dimension_table_regions(
    tokens: list[OCRToken], regions: list[ViewRegion]
) -> list[str]:
    """根据表头文字确定尺寸表区域，不依赖器件族或具体尺寸值。"""
    result: list[str] = []
    for region in regions:
        raw_texts = {
            token.text.strip().casefold()
            for token in tokens
            if token.source_region_id == region.region_id
        }
        texts = {
            re.sub(r"[^a-z0-9]+", "", token.text.strip().casefold())
            for token in tokens
            if token.source_region_id == region.region_id
        }
        has_metric = any(
            "millimet" in text or text.startswith(("miimet", "milimet"))
            for text in texts
        )
        has_imperial = any(text.startswith("inch") for text in texts)
        has_limits = (
            "min" in texts and ("nom" in texts or "typ" in texts) and "max" in texts
        )
        has_dimension_header = any("dimension" in text for text in texts)
        has_symbol_header = "symbol" in texts
        key_value_labels = [
            text for text in raw_texts if re.match(r"^dimension\s+\S+", text, re.I)
        ]
        has_key_value_table = (
            any("dimensions" in text for text in texts)
            and len(key_value_labels) >= 2
            and sum(bool(re.search(r"\d", text)) for text in texts) >= 2
        )
        has_pin_matrix = (
            any("pins" in text for text in texts)
            and any(text in {"dim", "amax", "amin"} for text in texts)
            and sum(bool(re.search(r"\d", text)) for text in texts) >= 4
        )
        if (
            ((has_metric or has_imperial) and has_limits and (
                has_dimension_header or has_symbol_header
            ))
            or has_key_value_table
            or has_pin_matrix
        ):
            result.append(region.region_id)
    return result


def _build_pin_count_matrix_groups(
    region_id: str,
    region_tokens: list[OCRToken],
    all_tokens: list[OCRToken],
    updated: dict[str, OCRToken],
    group_offset: int,
) -> list[DimensionGroup]:
    """解析“PINS × A MAX/A MIN”封装长度矩阵。

    列选择只依据同一张图中的 ``N PINS`` 身份 token；不会保存任何型号
    尺寸，也不会从参考模型反推列值。
    """
    count_matches = [
        (token, re.search(r"\b(\d{1,3})\s*PINS?\b", token.text, re.I))
        for token in all_tokens
    ]
    counts = [
        (token, int(match.group(1)))
        for token, match in count_matches
        if match is not None and token.confidence >= 0.80
    ]
    if not counts:
        return []
    _, selected_count = max(counts, key=lambda item: item[0].confidence)
    header = next(
        (
            token
            for token in region_tokens
            if token.text.strip() == str(selected_count) and token.confidence >= 0.80
        ),
        None,
    )
    if header is None:
        return []
    header_x = (header.bbox[0] + header.bbox[2]) / 2.0
    groups: list[DimensionGroup] = []
    for role, pattern in (("max", r"^A\s*MAX$"), ("min", r"^A\s*MIN$")):
        label = next(
            (
                token
                for token in region_tokens
                if re.match(pattern, token.text.strip(), re.I)
            ),
            None,
        )
        if label is None:
            continue
        label_y = (label.bbox[1] + label.bbox[3]) / 2.0
        row_height = max(10, label.bbox[3] - label.bbox[1])
        candidates = [
            token
            for token in region_tokens
            if token.token_id not in {label.token_id, header.token_id}
            and abs((token.bbox[1] + token.bbox[3]) / 2.0 - label_y)
            <= row_height * 1.2
            and re.fullmatch(r"\d+(?:[.,]\d+)?", token.text.strip())
        ]
        if not candidates:
            continue
        value = min(
            candidates,
            key=lambda token: (
                abs((token.bbox[0] + token.bbox[2]) / 2.0 - header_x),
                -token.confidence,
            ),
        )
        updated[value.token_id] = value.model_copy(update={
            "unit_context": "mm",
            "value_role": "table_maximum" if role == "max" else "table_minimum",
        })
        groups.append(DimensionGroup(
            dimension_id=f"table_{group_offset + len(groups) + 1:04d}",
            view_id=region_id,
            evidence_type="table_row",
            bbox=(
                min(label.bbox[0], value.bbox[0]),
                min(label.bbox[1], value.bbox[1]),
                max(label.bbox[2], value.bbox[2]),
                max(label.bbox[3], value.bbox[3]),
            ),
            token_ids=[label.token_id, value.token_id],
            row_label_token_ids=[label.token_id],
            context_token_ids=[header.token_id],
            table_columns={role: value.token_id},
            orientation="horizontal",
            confidence=min(label.confidence, value.confidence),
        ))
    return groups


def _build_key_value_table_groups(
    region_id: str,
    region_tokens: list[OCRToken],
    region_lines: list[GeometryLine],
    updated: dict[str, OCRToken],
    group_offset: int,
) -> list[DimensionGroup]:
    """归并“Dimension L | 3.2±0.2 mm”一类两列表格证据。"""
    labels = [
        token
        for token in region_tokens
        if re.match(r"^dimension\s+\S+", token.text.strip(), re.I)
    ]
    if not labels:
        return []
    token_heights = [max(1, token.bbox[3] - token.bbox[1]) for token in region_tokens]
    row_tolerance = max(8.0, median(token_heights) * 0.8) if token_heights else 12.0
    groups: list[DimensionGroup] = []
    for label in sorted(labels, key=lambda token: token.bbox[1]):
        label_y = (label.bbox[1] + label.bbox[3]) / 2.0
        candidates = [
            token
            for token in region_tokens
            if token.token_id != label.token_id
            and token.bbox[0] > label.bbox[2]
            and abs((token.bbox[1] + token.bbox[3]) / 2.0 - label_y)
            <= row_tolerance
            and re.search(r"\d", token.text)
        ]
        if not candidates:
            continue
        value_token = min(
            candidates,
            key=lambda token: (
                abs((token.bbox[1] + token.bbox[3]) / 2.0 - label_y),
                token.bbox[0],
            ),
        )
        unit = "mm" if re.search(r"\bmm\b", value_token.text, re.I) else "unknown"
        updated[value_token.token_id] = value_token.model_copy(update={
            "unit_context": unit,
            "value_role": "table_nominal",
        })
        groups.append(DimensionGroup(
            dimension_id=f"table_{group_offset + len(groups) + 1:04d}",
            view_id=region_id,
            evidence_type="table_row",
            bbox=(
                min(label.bbox[0], value_token.bbox[0]),
                min(label.bbox[1], value_token.bbox[1]),
                max(label.bbox[2], value_token.bbox[2]),
                max(label.bbox[3], value_token.bbox[3]),
            ),
            token_ids=[label.token_id, value_token.token_id],
            row_label_token_ids=[label.token_id],
            table_columns={"nom": value_token.token_id},
            # 两列表格本身就是明确结构化证据，不把文字笔画误充为尺寸线。
            extension_line_ids=[],
            orientation="horizontal",
            confidence=min(label.confidence, value_token.confidence),
        ))
    return groups


def build_table_dimension_groups(
    tokens: list[OCRToken],
    lines: list[GeometryLine],
    table_region_ids: set[str],
) -> tuple[list[DimensionGroup], list[OCRToken]]:
    """把尺寸表的行标签、符号及毫米 MIN/NOM/MAX 单元格归并为尺寸组。"""
    updated = {token.token_id: token for token in tokens}
    groups: list[DimensionGroup] = []
    for region_id in sorted(table_region_ids):
        region_tokens = [
            token for token in tokens if token.source_region_id == region_id
        ]
        region_lines = [
            line for line in lines if line.source_region_id == region_id
        ]
        header_tokens = [
            token
            for token in region_tokens
            if re.sub(r"[^a-z]", "", token.text.strip().casefold())
            in {"min", "nom", "typ", "max"}
        ]
        if len(header_tokens) < 3:
            matrix_groups = _build_pin_count_matrix_groups(
                region_id, region_tokens, tokens, updated, len(groups)
            )
            if matrix_groups:
                groups.extend(matrix_groups)
                continue
            groups.extend(_build_key_value_table_groups(
                region_id,
                region_tokens,
                region_lines,
                updated,
                len(groups),
            ))
            continue
        header_top = min(token.bbox[1] for token in header_tokens)
        unit_headers = [
            token for token in region_tokens
            if (
                "millimet" in token.text.strip().casefold()
                or token.text.strip().casefold().startswith(("miimet", "milimet"))
            )
            and token.bbox[1] < header_top
        ]
        inch_headers = [
            token for token in region_tokens
            if "inch" in token.text.strip().casefold()
            and token.bbox[1] < header_top
        ]
        header_candidates = header_tokens
        if unit_headers and inch_headers:
            metric_x = sum(
                (token.bbox[0] + token.bbox[2]) / 2.0 for token in unit_headers
            ) / len(unit_headers)
            inch_x = sum(
                (token.bbox[0] + token.bbox[2]) / 2.0 for token in inch_headers
            ) / len(inch_headers)
            boundary = (metric_x + inch_x) / 2.0
            header_candidates = [
                token for token in header_tokens
                if (((token.bbox[0] + token.bbox[2]) / 2.0) < boundary)
                == (metric_x < inch_x)
            ]
        metric_headers = sorted(
            header_candidates,
            key=lambda token: (token.bbox[0] + token.bbox[2]) / 2.0,
        )[:3]
        header_by_name = {
            (
                "nom"
                if re.sub(r"[^a-z]", "", token.text.strip().casefold()) == "typ"
                else re.sub(r"[^a-z]", "", token.text.strip().casefold())
            ): token
            for token in metric_headers
        }
        if not {"min", "nom", "max"}.issubset(header_by_name):
            continue
        header_y = max(token.bbox[3] for token in metric_headers)
        metric_centers = [
            (token.bbox[0] + token.bbox[2]) / 2.0 for token in metric_headers
        ]
        column_steps = [
            right - left
            for left, right in zip(metric_centers, metric_centers[1:])
            if right > left
        ]
        if not column_steps:
            continue
        column_step = median(column_steps)
        column_tolerance = max(12.0, column_step * 0.48)
        label_right = int(min(metric_centers) - column_step * 0.55)
        data_tokens = [
            token
            for token in region_tokens
            if (token.bbox[1] + token.bbox[3]) / 2.0 > header_y + 4
        ]
        token_heights = [
            max(1, token.bbox[3] - token.bbox[1]) for token in data_tokens
        ]
        row_tolerance = max(6.0, median(token_heights) * 0.65) if token_heights else 10.0
        row_clusters: list[list[OCRToken]] = []
        for token in sorted(data_tokens, key=lambda item: (item.bbox[1] + item.bbox[3]) / 2.0):
            center_y = (token.bbox[1] + token.bbox[3]) / 2.0
            target = next(
                (
                    row for row in row_clusters
                    if abs(
                        center_y
                        - sum((item.bbox[1] + item.bbox[3]) / 2.0 for item in row)
                        / len(row)
                    ) <= row_tolerance
                ),
                None,
            )
            if target is None:
                row_clusters.append([token])
            else:
                target.append(token)

        for row in row_clusters:
            label_candidates = [
                token
                for token in row
                if (token.bbox[0] + token.bbox[2]) / 2.0 < label_right
                and re.search(r"[A-Za-zΑ-ωα-ω]", token.text)
            ]
            descriptive_labels = [
                token
                for token in label_candidates
                if (
                    len(re.sub(r"[^A-Za-zΑ-ωα-ω]", "", token.text)) >= 3
                    or re.fullmatch(
                        r"[A-Za-zΑ-ωα-ω][A-Za-z0-9Α-ωα-ω]*",
                        token.text.strip(),
                    )
                )
            ]
            if not descriptive_labels:
                continue
            # 行名本身已经能唯一表达语义。短符号 OCR 容易受旋转和表格线干扰，
            # 保留在全量 State 中，但不送入当前 Qwen 小批次，也不参与门禁。
            label_tokens = sorted(descriptive_labels, key=lambda token: token.bbox[0])
            columns: dict[str, OCRToken] = {}
            for name, header in header_by_name.items():
                header_x = (header.bbox[0] + header.bbox[2]) / 2.0
                candidates = [
                    token
                    for token in row
                    if abs((token.bbox[0] + token.bbox[2]) / 2.0 - header_x)
                    <= column_tolerance
                    and re.search(r"\d", token.text)
                ]
                if candidates:
                    columns[name] = min(
                        candidates,
                        key=lambda token: (
                            abs((token.bbox[0] + token.bbox[2]) / 2.0 - header_x),
                            -token.confidence,
                        ),
                    )
            selected_column = "nom" if "nom" in columns else (
                "max" if "max" in columns else ""
            )
            if not selected_column:
                continue
            label_text = " ".join(
                token.text for token in sorted(label_tokens, key=lambda item: item.bbox[0])
            ).casefold()
            unit = "count" if "number of" in label_text else (
                "deg" if "angle" in label_text else "mm"
            )
            role_names = {
                "min": "table_minimum",
                "nom": "table_nominal",
                "max": "table_maximum",
            }
            for name, token in columns.items():
                updated[token.token_id] = token.model_copy(update={
                    "unit_context": unit,
                    "value_role": role_names[name],
                })
            row_y = sum((token.bbox[1] + token.bbox[3]) / 2.0 for token in row) / len(row)
            horizontal_grid_lines = [
                line
                for line in region_lines
                if _orientation(line) == "horizontal"
                and math.dist(line.start, line.end) >= column_step * 2.0
            ]
            nearby_lines = sorted(
                horizontal_grid_lines or region_lines,
                key=lambda line: abs(
                    row_y - (line.start[1] + line.end[1]) / 2.0
                ),
            )[:2]
            all_tokens = [*label_tokens, *columns.values()]
            groups.append(DimensionGroup(
                dimension_id=f"table_{len(groups) + 1:04d}",
                view_id=region_id,
                evidence_type="table_row",
                bbox=(
                    min(token.bbox[0] for token in all_tokens),
                    min(token.bbox[1] for token in all_tokens),
                    max(token.bbox[2] for token in all_tokens),
                    max(token.bbox[3] for token in all_tokens),
                ),
                token_ids=list(dict.fromkeys(
                    [token.token_id for token in [*label_tokens, *columns.values()]]
                )),
                row_label_token_ids=[token.token_id for token in label_tokens],
                table_columns={name: token.token_id for name, token in columns.items()},
                extension_line_ids=[line.line_id for line in nearby_lines],
                orientation="horizontal",
                confidence=min(
                    max(token.confidence for token in descriptive_labels),
                    columns[selected_column].confidence,
                ),
            ))
    return groups, [updated[token.token_id] for token in tokens]
