"""按候选视图检索小批量 OCR 与几何证据。

完整证据始终保存在 Agent State 中。本模块只为单次 Qwen 任务生成只读子集，
不修改 token、line 的原始 ID、bbox 或区域归属。
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from backend.agents.step.vision.schemas import (
    DimensionGroup,
    QwenSemanticResult,
    QwenViewClassificationResult,
    QwenViewSemanticResult,
    VisualEvidenceBundle,
)


_ENGINEERING_PATTERN = re.compile(
    r"[ØΦ±]|\bR\s*\d|REF|UNC|\d\s*[-×x]\s*[ØΦ.]|"
    r"\([^)]*\d[^)]*\)|(?:^|\D)[.]?\d+(?:\.\d+)?",
    re.I,
)

_DESCRIPTIVE_TEXT_PATTERN = re.compile(r"[A-Za-z]{3,}|[\u4e00-\u9fff]")


def _expanded_bbox(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    margin_ratio: float = 0.06,
) -> tuple[int, int, int, int]:
    """按图像短边扩展区域，纳入落在视图外侧的尺寸标注。"""
    width, height = image_size
    margin = max(24, int(min(width, height) * margin_ratio))
    x1, y1, x2, y2 = bbox
    return (
        max(0, x1 - margin),
        max(0, y1 - margin),
        min(width, x2 + margin),
        min(height, y2 + margin),
    )


def _bbox_intersects(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> bool:
    """判断两个轴对齐 bbox 是否相交。"""
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def build_region_summaries(evidence: VisualEvidenceBundle) -> list[dict[str, Any]]:
    """构造第一阶段视图识别摘要，不发送完整 OCR/线段集合。

    Args:
        evidence: Agent State 中的完整视觉证据。

    Returns:
        每个候选区域的 bbox、证据计数和少量非尺寸文本样本。
    """
    summaries: list[dict[str, Any]] = []
    for region in evidence.regions:
        expanded = _expanded_bbox(region.bbox, evidence.image_size)
        tokens = [
            token
            for token in evidence.ocr_tokens
            if token.source_region_id == region.region_id
            or _bbox_intersects(token.bbox, expanded)
        ]
        lines = [
            line
            for line in evidence.lines
            if line.source_region_id == region.region_id
            or (
                expanded[0] <= (line.start[0] + line.end[0]) / 2 <= expanded[2]
                and expanded[1] <= (line.start[1] + line.end[1]) / 2 <= expanded[3]
            )
        ]
        # 只排除没有描述性文字的纯数值尺寸。像 ``SOT-23`` 这类带数字的
        # 封装标题必须保留，否则器件族分类会丢失最强证据。
        text_samples = [
            token.text[:80]
            for token in sorted(tokens, key=lambda item: item.confidence, reverse=True)
            if _DESCRIPTIVE_TEXT_PATTERN.search(token.text)
        ][:8]
        summaries.append(
            {
                "region_id": region.region_id,
                "bbox": region.bbox,
                "area_ratio": region.area_ratio,
                "ocr_token_count": len(tokens),
                "line_type_counts": dict(Counter(line.type for line in lines)),
                "text_samples": text_samples,
            }
        )
    return summaries


def retrieve_view_evidence(
    evidence: VisualEvidenceBundle,
    region_id: str,
    *,
    max_tokens: int = 36,
) -> dict[str, Any]:
    """检索一个视图的尺寸 token 及其直接关联线段。

    Args:
        evidence: Agent State 中的完整视觉证据。
        region_id: 第一阶段确认的当前候选视图 ID。
        max_tokens: 单次 Qwen 请求允许的最大 OCR token 数。

    Returns:
        仅包含当前视图 token、line 和 token-line 邻接关系的证据子集。

    Raises:
        KeyError: ``region_id`` 不存在。
    """
    region = next((item for item in evidence.regions if item.region_id == region_id), None)
    if region is None:
        raise KeyError(f"未知视图区域：{region_id}")
    expanded = _expanded_bbox(region.bbox, evidence.image_size)
    candidates = [
        token
        for token in evidence.ocr_tokens
        if (
            token.source_region_id == region_id
            or _bbox_intersects(token.bbox, expanded)
        )
        and token.confidence >= 0.55
        and _ENGINEERING_PATTERN.search(token.text)
    ]
    candidates.sort(
        key=lambda item: (
            0 if item.source_region_id == region_id else 1,
            -item.confidence,
            item.bbox[1],
            item.bbox[0],
        )
    )
    tokens = candidates[:max_tokens]
    token_ids = {token.token_id for token in tokens}
    links = [
        link for link in evidence.token_line_links if link.token_id in token_ids
    ]
    linked_line_ids = {
        line_id for link in links for line_id in link.line_ids[:2]
    }
    lines = [line for line in evidence.lines if line.line_id in linked_line_ids]
    retained_line_ids = {line.line_id for line in lines}
    return {
        "region": region.model_dump(),
        "ocr_tokens": [token.model_dump() for token in tokens],
        "lines": [line.model_dump() for line in lines],
        "token_line_links": [
            {
                **link.model_dump(),
                "line_ids": [
                    line_id for line_id in link.line_ids if line_id in retained_line_ids
                ],
            }
            for link in links
        ],
    }


def retrieve_dimension_group_evidence(
    evidence: VisualEvidenceBundle,
    dimension_groups: list[DimensionGroup],
    region_id: str,
    *,
    max_tokens: int = 25,
    max_dimension_groups: int = 12,
) -> dict[str, Any]:
    """按当前视图检索完整尺寸组，不在组内截断 token 或线段。

    Args:
        evidence: State 中的完整视觉证据。
        dimension_groups: 确定性程序生成的全部尺寸组。
        region_id: 当前处理视图。
        max_tokens: 当前 Prompt 的 token 上限。
        max_dimension_groups: 当前 Prompt 的尺寸组上限。

    Returns:
        当前视图的尺寸组、其引用 token 和线段。
    """
    region = next((item for item in evidence.regions if item.region_id == region_id), None)
    if region is None:
        raise KeyError(f"未知视图区域：{region_id}")
    token_index = {token.token_id: token for token in evidence.ocr_tokens}

    def prompt_token_ids(group: DimensionGroup) -> list[str]:
        """表格只发送字段名、符号及一个目标值，避免一行重复占用预算。

        表格分组阶段会保留完整 ``row_label_token_ids`` 供审计，其中偶尔会
        混入英制列数值或 OCR 将数字 ``0`` 识别成的 ``O``。Prompt 检索阶段
        不修改 State 中的原始证据，只选择一个描述性字段名和一个短符号，
        再附加 NOM/TYP（缺失时为 MAX）值。
        """
        if group.evidence_type != "table_row":
            # MAX、MIN、NOM、BSC 等限定词属于尺寸语义证据。它们虽然不承载
            # 数值，但必须随尺寸组进入当前 Prompt/Family 校正；完整数据仍
            # 保存在 State，这里只追加与已选尺寸组直接关联的上下文 token。
            return list(dict.fromkeys([
                *group.token_ids,
                *group.context_token_ids,
            ]))
        descriptive_id = next(
            (
                token_id
                for token_id in group.row_label_token_ids
                if token_id in token_index
                and _DESCRIPTIVE_TEXT_PATTERN.search(token_index[token_id].text)
            ),
            None,
        )
        symbol_id = next(
            (
                token_id
                for token_id in group.row_label_token_ids
                if token_id != descriptive_id
                and token_id in token_index
                and re.fullmatch(
                    r"[A-Za-z\u0370-\u03ff][A-Za-z0-9]?",
                    token_index[token_id].text.strip(),
                )
            ),
            None,
        )
        compact_labels = [item for item in (descriptive_id, symbol_id) if item]
        if not compact_labels:
            compact_labels = list(group.row_label_token_ids)
        selected_id = group.table_columns.get("nom") or group.table_columns.get("max")
        return list(dict.fromkeys([
            *compact_labels,
            *group.context_token_ids,
            *([selected_id] if selected_id else []),
        ]))

    ranked = sorted(
        (group for group in dimension_groups if group.view_id == region_id),
        key=lambda item: (
            0 if item.evidence_type == "identity_text"
            else 1 if item.dimension_line_ids
            else 2,
            -len(item.arrow_ids),
            -item.confidence,
            item.bbox[1],
            item.bbox[0],
        ),
    )
    selected: list[DimensionGroup] = []
    selected_token_ids: list[str] = []
    for group in ranked:
        group_token_ids = prompt_token_ids(group)
        new_ids = [item for item in group_token_ids if item not in selected_token_ids]
        if len(selected) >= max_dimension_groups:
            break
        if selected and len(selected_token_ids) + len(new_ids) > max_tokens:
            continue
        selected.append(group)
        selected_token_ids.extend(new_ids)
    line_ids = {
        line_id
        for group in selected
        for line_id in [*group.dimension_line_ids, *group.extension_line_ids]
    }
    line_index = {line.line_id: line for line in evidence.lines}
    return {
        "region": {"region_id": region.region_id, "bbox": region.bbox},
        "dimension_groups": [
            {
                "dimension_id": group.dimension_id,
                "evidence_type": group.evidence_type,
                "bbox": group.bbox,
                "token_ids": prompt_token_ids(group),
                "row_label_token_ids": group.row_label_token_ids,
                "context_token_ids": group.context_token_ids,
                "table_columns": (
                    {"nom": group.table_columns["nom"]}
                    if "nom" in group.table_columns
                    else {"max": group.table_columns["max"]}
                    if "max" in group.table_columns
                    else {}
                    if group.evidence_type == "table_row"
                    else group.table_columns
                ),
                "dimension_line_ids": group.dimension_line_ids,
                "extension_line_ids": group.extension_line_ids,
                "arrow_ids": group.arrow_ids,
                "orientation": group.orientation,
                "confidence": round(group.confidence, 3),
            }
            for group in selected
        ],
        "ocr_tokens": [
            {
                "token_id": token_index[token_id].token_id,
                "text": token_index[token_id].text,
                "bbox": token_index[token_id].bbox,
                "confidence": round(token_index[token_id].confidence, 3),
                "unit_context": token_index[token_id].unit_context,
                "value_role": token_index[token_id].value_role,
            }
            for token_id in selected_token_ids
            if token_id in token_index
        ],
        "lines": [
            {
                "line_id": line_index[line_id].line_id,
                "type": line_index[line_id].type,
                "start": line_index[line_id].start,
                "end": line_index[line_id].end,
                "arrow_ids": line_index[line_id].arrow_ids,
            }
            for line_id in sorted(line_ids)
            if line_id in line_index
        ],
    }


def merge_view_semantics(
    classification: QwenViewClassificationResult,
    per_view: list[QwenViewSemanticResult],
) -> QwenSemanticResult:
    """确定性合并逐视图结果，不调用模型且不生成任何尺寸数值。"""
    features = list(classification.identified_features)
    assignments = []
    unresolved = list(classification.unresolved_fields)
    ambiguities = list(classification.ambiguities)
    # 空白或与尺寸无关的视图仍保留审计信息，但不应把真正贡献参数的
    # 尺寸表/视图置信度压成 0。
    confidences = [classification.overall_confidence]
    for item in per_view:
        for feature in item.identified_features:
            if feature not in features:
                features.append(feature)
        assignments.extend(item.assignments)
        unresolved.extend(item.unresolved_fields)
        ambiguities.extend(item.ambiguities)
        if item.assignments:
            confidences.append(item.confidence)
    assigned_names = {item.canonical_name for item in assignments}
    resolved_names = {*assigned_names, *features}
    unresolved = [name for name in unresolved if name not in resolved_names]
    return QwenSemanticResult(
        family_id=classification.family_id,
        category_id=classification.category_id,
        subcategory_id=classification.subcategory_id,
        package_type=classification.package_type,
        identified_views=classification.identified_views,
        identified_features=features,
        assignments=assignments,
        unresolved_fields=list(dict.fromkeys(unresolved)),
        ambiguities=list(dict.fromkeys(ambiguities)),
        overall_confidence=min(confidences),
    )
