"""OCR 数值、几何线证据与 Qwen 语义引用的确定性融合。"""

from __future__ import annotations

from collections import defaultdict

from backend.agents.step.vision.ocr import (
    parse_dimension_expression,
    parse_explicit_count_expression,
    parse_table_nominal_expression,
)
from backend.agents.step.vision.schemas import (
    DimensionGateResult,
    FusedEvidence,
    FusedParameter,
    QwenSemanticResult,
    VisualEvidenceBundle,
)


def fuse_evidence(
    evidence: VisualEvidenceBundle,
    semantics: QwenSemanticResult,
) -> FusedEvidence:
    """只根据 Qwen 引用的现有证据生成参数，Qwen 不参与数值填写。"""
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
        if parameter.ocr_confidence < minimum_ocr_confidence
        or parameter.semantic_confidence < (
            min(minimum_semantic_confidence, 0.80)
            if parameter.evidence_kind == "table_row"
            else min(minimum_semantic_confidence, 0.75)
            if parameter.evidence_kind == "explicit_range"
            else minimum_semantic_confidence
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
