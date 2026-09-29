"""OCR 数值、几何线证据与 Qwen 语义引用的确定性融合。"""

from __future__ import annotations

from collections import defaultdict
import re

from backend.agents.step.vision.ocr import (
    parse_dimension_expression,
    parse_explicit_count_expression,
    parse_table_nominal_expression,
)
from backend.agents.step.vision.schemas import (
    DimensionGateResult,
    FusedEvidence,
    FusedParameter,
    GeometryLine,
    OCRToken,
    QwenSemanticResult,
    SemanticAssignment,
    VisualEvidenceBundle,
)


def _stacked_range_separator(
    upper: OCRToken, lower: OCRToken, lines: list[GeometryLine]
) -> GeometryLine | None:
    """辨认有分隔横线的两行上下限；相邻数值本身不是范围证据。"""
    if (
        upper.source_region_id != lower.source_region_id
        or upper.source_region_id == "unassigned"
        or upper.unit_context != lower.unit_context
        or upper.unit_context != "mm"
        or upper.value_role.startswith("table_")
        or lower.value_role.startswith("table_")
        or not all(re.fullmatch(r"\d+(?:[.,]\d+)?", token.text.strip()) for token in (upper, lower))
    ):
        return None
    first = parse_dimension_expression(upper.text).nominal_value
    second = parse_dimension_expression(lower.text).nominal_value
    if first is None or second is None or first <= second:
        return None
    a, b = upper.bbox, lower.bbox
    width = min(a[2] - a[0], b[2] - b[0])
    height = max(a[3] - a[1], b[3] - b[1])
    if width <= 0 or height <= 0:
        return None
    gap = b[1] - a[3]
    if (
        not 0 <= gap <= height * 0.65
        or abs((a[0] + a[2]) - (b[0] + b[2])) / 2 > width * 0.20
        or min(a[2], b[2]) - max(a[0], b[0]) < width * 0.80
    ):
        return None
    for line in lines:
        x1, y1 = line.start
        x2, y2 = line.end
        if (
            line.source_region_id == upper.source_region_id
            and abs(y2 - y1) <= height * 0.10
            and a[3] <= (y1 + y2) / 2 <= b[1]
            and min(max(x1, x2), a[2], b[2]) - max(min(x1, x2), a[0], b[0]) >= width * 0.60
            and min(x1, x2) >= min(a[0], b[0]) - width * 0.20
            and max(x1, x2) <= max(a[2], b[2]) + width * 0.20
        ):
            return line
    return None


def _reconcile_split_stacked_ranges(
    evidence: VisualEvidenceBundle, semantics: QwenSemanticResult
) -> QwenSemanticResult:
    """修复被拆开的同一印刷范围，不以置信度压掉独立尺寸矛盾。

    跨字段归并还要求：下限没有自己的独立外部尺寸线/标签，且该字段在
    另一视图有完整范围作旁证。没有分隔线、单位/视图不同或存在独立
    尺寸线时保留原 assignment，交由既有冲突门禁处理。
    """
    tokens = {item.token_id: item for item in evidence.ocr_tokens}
    lines = {item.line_id: item for item in evidence.lines}
    assignments = list(semantics.assignments)
    consumed: set[int] = set()
    changes: list[str] = []

    def outside_lines(item: SemanticAssignment, token: OCRToken) -> set[str]:
        x0, y0, x1, y1 = token.bbox
        margin = max(1.0, (y1 - y0) * 0.10)
        return {
            line_id for line_id in item.line_ids
            if line_id in lines and lines[line_id].type == "dimension_line"
            and any(
                not (x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin)
                for x, y in (lines[line_id].start, lines[line_id].end)
            )
        }

    def has_independent_label(token: OCRToken) -> bool:
        """附近 A1/A2 等独立尺寸标签阻止把两行当作同一范围。"""
        x0, y0, x1, y1 = token.bbox
        height = max(1, y1 - y0)
        for label in evidence.ocr_tokens:
            if label.source_region_id != token.source_region_id:
                continue
            text = label.text.strip()
            if not re.search(r"[A-Za-zΑ-ω]", text) or text.casefold() in {"min", "max", "nom", "typ", "mm"}:
                continue
            a, b, c, d = label.bbox
            overlap = min(y1, d) - max(y0, b)
            horizontal_gap = max(x0 - c, a - x1, 0)
            if overlap >= min(height, max(1, d - b)) * 0.50 and horizontal_gap <= height:
                return True
        return False

    for upper_index, upper_assignment in enumerate(assignments):
        if upper_index in consumed or len(upper_assignment.token_ids) != 1:
            continue
        upper = tokens.get(upper_assignment.token_ids[0])
        if upper is None or upper_assignment.canonical_name.endswith(("_deg", "_count")):
            continue
        candidates = []
        for lower_index, lower_assignment in enumerate(assignments):
            if lower_index == upper_index or lower_index in consumed or len(lower_assignment.token_ids) != 1:
                continue
            lower = tokens.get(lower_assignment.token_ids[0])
            if lower is None or lower_assignment.canonical_name.endswith(("_deg", "_count")):
                continue
            separator = _stacked_range_separator(upper, lower, evidence.lines)
            if separator is not None:
                candidates.append((lower_index, lower_assignment, lower, separator))
        if len(candidates) != 1:
            continue
        lower_index, lower_assignment, lower, separator = candidates[0]
        if has_independent_label(upper) or has_independent_label(lower):
            continue
        # 有两个不同的真实尺寸标注，不能仅凭文字对齐将其合并。
        upper_external = outside_lines(upper_assignment, upper) - {separator.line_id}
        lower_external = outside_lines(lower_assignment, lower) - {separator.line_id}
        if upper_external and lower_external and upper_external != lower_external:
            continue
        support_ids: list[str] = []
        if upper_assignment.canonical_name != lower_assignment.canonical_name:
            if lower_external - upper_external:
                continue
            for support in assignments:
                if support.canonical_name != lower_assignment.canonical_name or len(support.token_ids) != 2:
                    continue
                pair = [tokens[token_id] for token_id in support.token_ids if token_id in tokens]
                if len(pair) != 2 or any(item.source_region_id == lower.source_region_id for item in pair):
                    continue
                first, second = sorted(pair, key=lambda item: item.bbox[1])
                if _stacked_range_separator(first, second, evidence.lines) is None:
                    continue
                lower_bound = parse_dimension_expression(second.text).nominal_value
                upper_bound = parse_dimension_expression(first.text).nominal_value
                split_value = parse_dimension_expression(lower.text).nominal_value
                anchor_value = parse_dimension_expression(upper.text).nominal_value
                if lower_bound <= split_value <= upper_bound < anchor_value:
                    support_ids = list(support.token_ids)
                    break
            if not support_ids:
                continue
        assignments[upper_index] = upper_assignment.model_copy(update={
            "token_ids": [*upper_assignment.token_ids, *lower_assignment.token_ids],
            "line_ids": list(dict.fromkeys([
                *upper_assignment.line_ids, *lower_assignment.line_ids, separator.line_id,
            ])),
            "confidence": min(upper_assignment.confidence, lower_assignment.confidence),
            "target_feature": f"{upper_assignment.target_feature};stacked_range",
        })
        consumed.add(lower_index)
        changes.append(
            f"stacked_range_reconciled:{upper.token_id}+{lower.token_id}"
            f"->{upper_assignment.canonical_name};separator={separator.line_id};"
            f"split_field={lower_assignment.canonical_name};corroboration={support_ids}"
        )
    return semantics.model_copy(update={
        "assignments": [item for index, item in enumerate(assignments) if index not in consumed],
        "ambiguities": [*semantics.ambiguities, *changes],
    })


def fuse_evidence(
    evidence: VisualEvidenceBundle,
    semantics: QwenSemanticResult,
) -> FusedEvidence:
    """只根据 Qwen 引用的现有证据生成参数，Qwen 不参与数值填写。"""
    semantics = _reconcile_split_stacked_ranges(evidence, semantics)
    tokens = {token.token_id: token for token in evidence.ocr_tokens}
    lines = {line.line_id: line for line in evidence.lines}
    missing_evidence: list[str] = []
    unit_conflicts: list[str] = []
    by_name: dict[str, list[FusedParameter]] = defaultdict(list)
    for assignment in semantics.assignments:
        referenced_tokens = [tokens[token_id] for token_id in assignment.token_ids if token_id in tokens]
        referenced_lines = [lines[line_id] for line_id in assignment.line_ids if line_id in lines]
        has_table_value = any(
            token.value_role.startswith("table_") for token in referenced_tokens
        )
        has_identity_count = (
            assignment.canonical_name in {"circuit_count", "nominal_pin_count"}
            and any(
                parse_explicit_count_expression(token.text) is not None
                for token in referenced_tokens
            )
        )
        if len(referenced_tokens) != len(assignment.token_ids):
            missing_evidence.append(assignment.canonical_name)
            continue
        if (
            len(referenced_lines) != len(assignment.line_ids)
            or (not referenced_lines and not (has_table_value or has_identity_count))
        ):
            missing_evidence.append(assignment.canonical_name)
            continue
        parsed_items = [
            (
                parse_table_nominal_expression(token.text)
                if token.value_role.startswith("table_")
                else parse_dimension_expression(token.text)
            )
            for token in referenced_tokens
        ]
        if assignment.canonical_name in {"circuit_count", "nominal_pin_count"}:
            for index, (token, parsed) in enumerate(zip(referenced_tokens, parsed_items)):
                if parsed.nominal_value is not None:
                    continue
                count = parse_explicit_count_expression(token.text)
                if count is not None:
                    parsed_items[index] = parsed.model_copy(update={
                        "nominal_value": float(count),
                        "unit": "count",
                    })
        numeric = [
            item
            for item in parsed_items
            if item.nominal_value is not None or item.maximum_value is not None
        ]
        if not numeric:
            missing_evidence.append(assignment.canonical_name)
            continue
        values = [
            float(
                item.nominal_value
                if item.nominal_value is not None
                else item.maximum_value
            )
            for item in numeric
        ]
        selected_value = values[0]
        if max(values) - min(values) > 1e-6:
            # 常见封装图会把同一尺寸的上、下限上下堆叠，并共享同一组
            # 尺寸线。Qwen 已将它们绑定到同一个 canonical_name 时，
            # 使用图纸明确给出的最大包络值；超过两个离散数值仍视为
            # 证据混杂，禁止进入 Feature IR。
            numeric_tokens = [
                token
                for token, item in zip(referenced_tokens, parsed_items)
                if item.nominal_value is not None or item.maximum_value is not None
            ]
            centers = [
                ((token.bbox[0] + token.bbox[2]) / 2.0,
                 (token.bbox[1] + token.bbox[3]) / 2.0)
                for token in numeric_tokens
            ]
            average_width = sum(
                token.bbox[2] - token.bbox[0] for token in numeric_tokens
            ) / len(numeric_tokens)
            average_height = sum(
                token.bbox[3] - token.bbox[1] for token in numeric_tokens
            ) / len(numeric_tokens)
            aligned_stack = bool(centers) and (
                max(item[0] for item in centers) - min(item[0] for item in centers)
                <= max(20.0, average_width * 0.75)
                or max(item[1] for item in centers) - min(item[1] for item in centers)
                <= max(20.0, average_height * 0.75)
            )
            if len(values) == 2 and referenced_lines:
                selected_value = max(values)
            elif len(values) == 3 and referenced_lines and aligned_stack:
                # 工程图常把 MAX/NOM/MIN 三个值沿同一尺寸线垂直或水平堆叠。
                # Family 建模消费外包络，因此选择图纸明确给出的最大值。
                selected_value = max(values)
            else:
                missing_evidence.append(assignment.canonical_name)
                continue
        units = {
            token.unit_context if item.unit == "unknown" else item.unit
            for token, item in zip(referenced_tokens, parsed_items)
            if item.nominal_value is not None
        }
        if assignment.canonical_name in {"circuit_count", "nominal_pin_count"}:
            unit = "count"
        elif assignment.canonical_name.endswith("_deg"):
            unit = "deg"
        elif len(units) == 1 and "unknown" not in units:
            unit = next(iter(units))
        else:
            unit_conflicts.append(assignment.canonical_name)
            continue
        parameter = FusedParameter(
            canonical_name=assignment.canonical_name,
            value=selected_value,
            unit=unit,
            evidence_ids=[*assignment.token_ids, *assignment.line_ids],
            token_ids=list(assignment.token_ids),
            line_ids=list(assignment.line_ids),
            token_bboxes=[token.bbox for token in referenced_tokens],
            target_feature=assignment.target_feature,
            # 行名可参与语义引用，但数值门禁只评价真正承载数值的 OCR token，
            # 避免可选且低置信度的表格符号把一整行错误判为不可用。
            ocr_confidence=min(
                token.confidence
                for token, item in zip(referenced_tokens, parsed_items)
                if item.nominal_value is not None or item.maximum_value is not None
            ),
            semantic_confidence=assignment.confidence,
            raw_texts=[token.text for token in referenced_tokens],
            evidence_kind=(
                "table_row"
                if has_table_value
                else "identity_text"
                if has_identity_count
                else "explicit_range"
                if assignment.canonical_name.endswith("_deg")
                and any(
                    item.minimum_value is not None and item.maximum_value is not None
                    for item in parsed_items
                )
                else "dimension_line"
            ),
        )
        by_name[assignment.canonical_name].append(parameter)

    parameters: list[FusedParameter] = []
    conflicts: list[str] = []
    for name, candidates in by_name.items():
        distinct = {(round(item.value, 8), item.unit) for item in candidates}
        if len(distinct) > 1:
            # 尺寸表的数值单元格具有明确的行列语义；普通视图中的 OCR 数值
            # 可能被 Qwen 误挂到相邻尺寸线上。仅当冲突候选中存在唯一的表格
            # 数值时，允许确定性地采用该候选。若表格自身也存在多个不同值，
            # 仍然保留冲突并在 Feature IR 之前停止。
            table_candidates = [
                item
                for item in candidates
                if any(
                    tokens[token_id].value_role.startswith("table_")
                    for token_id in item.token_ids
                    if token_id in tokens
                )
            ]
            table_distinct = {
                (round(item.value, 8), item.unit) for item in table_candidates
            }
            if table_candidates and len(table_distinct) == 1:
                parameters.append(
                    max(table_candidates, key=lambda item: item.semantic_confidence)
                )
                continue
            conflicts.append(name)
            continue
        parameters.append(max(candidates, key=lambda item: item.semantic_confidence))
    return FusedEvidence(
        family_id=semantics.family_id,
        category_id=semantics.category_id,
        subcategory_id=semantics.subcategory_id,
        package_type=semantics.package_type,
        identified_views=semantics.identified_views,
        identified_features=semantics.identified_features,
        parameters=parameters,
        # 同一字段可能在轮廓视图中误关联、但在尺寸表中具有完整证据。
        # 只要至少一个候选已成功融合，就不让失败的重复 assignment 污染门禁。
        missing_evidence_assignments=sorted(
            name for name in set(missing_evidence) if name not in by_name
        ),
        conflicting_fields=sorted(set(conflicts)),
        unit_conflicts=sorted(
            name for name in set(unit_conflicts) if name not in by_name
        ),
        unresolved_fields=semantics.unresolved_fields,
        ambiguities=semantics.ambiguities,
    )


def validate_fused_dimensions(
    fused: FusedEvidence,
    *,
    required_fields: tuple[str, ...],
    required_features: tuple[str, ...],
    minimum_ocr_confidence: float = 0.80,
    minimum_semantic_confidence: float = 0.85,
) -> DimensionGateResult:
    """在 Feature IR 前执行证据完整性、置信度、单位和冲突门禁。"""
    indexed = {parameter.canonical_name: parameter for parameter in fused.parameters}
    missing = [name for name in required_fields if name not in indexed]
    missing.extend(
        f"feature:{feature}"
        for feature in required_features
        if feature not in fused.identified_features
    )
    required_field_set = set(required_fields)
    # Qwen 可能把图纸中存在、但当前 Family 不消费的额外尺寸行写入
    # unresolved_fields。它们应保留供审计，不能伪装成关键字段缺失。
    missing.extend(
        name
        for name in fused.missing_evidence_assignments
        if name in required_field_set
    )
    low_confidence = [
        name
        for name, parameter in indexed.items()
        if (
            parameter.evidence_kind != "human_input"
            and not (
                parameter.evidence_kind == "derived"
                and parameter.evidence_ids
                and all(item.startswith("human_input:") for item in parameter.evidence_ids)
            )
            and (
                parameter.ocr_confidence < minimum_ocr_confidence
                or parameter.semantic_confidence < (
                    min(minimum_semantic_confidence, 0.80)
                    if parameter.evidence_kind == "table_row"
                    else min(minimum_semantic_confidence, 0.75)
                    if parameter.evidence_kind == "explicit_range"
                    else minimum_semantic_confidence
                )
            )
        )
    ]
    conflicting = [*fused.conflicting_fields, *fused.unit_conflicts]
    missing.extend(
        name for name in fused.unresolved_fields if name in required_field_set
    )
    missing = sorted(set(missing))
    conflicting = sorted(set(conflicting))
    low_confidence = sorted(set(low_confidence))
    passed = not missing and not conflicting and not low_confidence
    evidence_report = (
        f"有效参数 {len(indexed)} 个；缺失 {len(missing)} 个；"
        f"冲突 {len(conflicting)} 个；低置信度 {len(low_confidence)} 个。"
    )
    return DimensionGateResult(
        passed=passed,
        status="dimensions_valid" if passed else "stopped_insufficient_extraction",
        missing_fields=missing,
        conflicting_fields=conflicting,
        low_confidence_fields=low_confidence,
        evidence_report=evidence_report,
    )
