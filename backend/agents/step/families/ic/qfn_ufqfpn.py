"""QFN、UFQFPN 等四边无引脚 IC 的参数化建模模板。"""

from __future__ import annotations

import re
from typing import Any

from backend.agents.step.ir.schemas import EvidenceCADFeature, EvidenceFeatureIR, EvidenceValue
from backend.agents.step.vision.schemas import (
    FusedEvidence,
    FusedParameter,
    QwenViewSemanticResult,
    SemanticAssignment,
    parameter_has_traceable_source,
)


FAMILY_ID = "ic/qfn_ufqfpn"
REQUIRED_PARAMETERS = (
    "nominal_pin_count",
    "body_length",
    "body_width",
    "total_height",
    "body_standoff",
    "terminal_pitch",
    "terminal_length",
    "terminal_width",
    "terminal_height",
)
OPTIONAL_PARAMETERS = ("exposed_pad_length", "exposed_pad_width")
REQUIRED_FEATURES = ("molded_body", "no_lead_terminal", "four_side_terminal_array")
PARAMETER_GUIDANCE = {
    "nominal_pin_count": "标题中的 N-lead/N-pin，或四角完整引脚编号序列的最大编号",
    "body_length": "符号 D；优先 TYP/NOM/BSC",
    "body_width": "符号 E；优先 TYP/NOM/BSC",
    "total_height": "符号 A；优先 TYP/NOM，否则 MAX",
    "body_standoff": "符号 A1；优先 TYP/NOM，否则 MAX",
    "terminal_pitch": "符号 e；优先 TYP/NOM/BSC",
    "terminal_length": "符号 L 或 L2；优先 TYP/NOM",
    "terminal_width": "符号 b；优先 TYP/NOM",
    "terminal_height": "符号 A3；优先 TYP/NOM",
    "exposed_pad_length": "可选中心裸露焊盘尺寸 D2；优先 TYP/NOM",
    "exposed_pad_width": "可选中心裸露焊盘尺寸 E2；优先 TYP/NOM",
}

_TABLE_SYMBOLS = {
    "A": "total_height",
    "A1": "body_standoff",
    "A3": "terminal_height",
    "B": "terminal_width",
    "D": "body_length",
    "E": "body_width",
    "D2": "exposed_pad_length",
    "E2": "exposed_pad_width",
    "L": "terminal_length",
    "L2": "terminal_length",
}


def _normalized_symbol(text: str) -> str:
    value = text.strip()
    return "b" if value == "b" else value.upper()


def semantics_from_table(
    evidence: dict[str, Any],
    *,
    region_id: str,
    view_type: str,
) -> QwenViewSemanticResult:
    """按 QFN/UFQFPN 表格符号绑定证据，不解释或填写数值。"""
    tokens = {item["token_id"]: item for item in evidence["ocr_tokens"]}
    assignments: list[SemanticAssignment] = []
    assigned: set[str] = set()
    symbol_scores: dict[str, float] = {}
    for group in evidence.get("dimension_groups", []):
        if group.get("evidence_type") == "identity_text":
            token_id = next((
                item for item in group.get("token_ids", [])
                if re.search(r"\b\d{1,3}\s*[-–—]?\s*(?:lead|pin)s?\b", tokens[item]["text"], re.I)
            ), None)
            if token_id and "nominal_pin_count" not in assigned:
                assignments.append(SemanticAssignment(
                    canonical_name="nominal_pin_count",
                    token_ids=[token_id],
                    target_feature="package_identity",
                    confidence=0.99,
                ))
                assigned.add("nominal_pin_count")
            continue
        label = next((
            token_id
            for token_id in group.get("row_label_token_ids", [])
            if _normalized_symbol(tokens.get(token_id, {}).get("text", ""))
            in {*_TABLE_SYMBOLS, "b", "E"}
        ), None)
        if not label:
            continue
        symbol = _normalized_symbol(tokens[label]["text"])
        name = "terminal_pitch" if symbol == "E" and tokens[label]["text"].strip() == "e" else (
            "terminal_width" if symbol == "b" else _TABLE_SYMBOLS.get(symbol)
        )
        columns = group.get("table_columns", {})
        value = columns.get("nom") or columns.get("max") or columns.get("min")
        if not name or not value:
            continue
        score = float(tokens[label]["confidence"])
        if score <= symbol_scores.get(name, -1.0):
            continue
        assignment = SemanticAssignment(
            canonical_name=name,
            token_ids=[label, value],
            target_feature="qfn_ufqfpn_package_table",
            confidence=0.99,
        )
        assignments = [item for item in assignments if item.canonical_name != name]
        assignments.append(assignment)
        assigned.add(name)
        symbol_scores[name] = score
    return QwenViewSemanticResult(
        region_id=region_id,
        view_type=view_type,
        identified_features=list(REQUIRED_FEATURES),
        assignments=assignments,
        unresolved_fields=[name for name in REQUIRED_PARAMETERS if name not in assigned],
        confidence=min((item.confidence for item in assignments), default=0.0),
    )


def reconcile_view_semantics(
    result: QwenViewSemanticResult,
    evidence: dict[str, Any],
) -> QwenViewSemanticResult:
    """从完整四角引脚编号序列恢复无引脚封装的端子总数。"""
    tokens = {item["token_id"]: item for item in evidence["ocr_tokens"]}
    numbered = {
        int(item["text"].strip()): item["token_id"]
        for item in evidence["ocr_tokens"]
        if item["text"].strip().isdigit()
    }
    candidate = next((
        count
        for count in sorted(numbered, reverse=True)
        if count >= 4
        and count % 4 == 0
        and len({1, count // 4, count // 4 + 1, count // 2, count // 2 + 1,
                 3 * count // 4, 3 * count // 4 + 1, count} & set(numbered)) >= 4
    ), None)
    if candidate is None:
        return result
    token_id = numbered[candidate]
    group = next((
        item for item in evidence.get("dimension_groups", [])
        if token_id in item.get("token_ids", [])
    ), None)
    if group is None:
        return result
    line_ids = list(dict.fromkeys([
        *group.get("dimension_line_ids", []),
        *group.get("extension_line_ids", []),
    ]))
    if not line_ids:
        return result
    assignment = SemanticAssignment(
        canonical_name="nominal_pin_count",
        token_ids=[token_id],
        line_ids=line_ids,
        target_feature="four_corner_pin_number_sequence",
        confidence=min(0.98, float(tokens[token_id]["confidence"])),
    )
    return result.model_copy(update={
        "identified_features": list(REQUIRED_FEATURES),
        "assignments": [
            item for item in result.assignments
            if item.canonical_name != "nominal_pin_count"
        ] + [assignment],
        "unresolved_fields": [
            name for name in result.unresolved_fields if name != "nominal_pin_count"
        ],
    })


def _value(parameter: FusedParameter) -> EvidenceValue:
    return EvidenceValue(
        value=parameter.value,
        unit=parameter.unit,
        evidence_ids=list(parameter.evidence_ids),
    )


def plan_from_evidence(
    fused: FusedEvidence,
    *,
    source_image_sha256: str,
) -> dict[str, Any]:
    """把 QFN/UFQFPN 证据转换为本体、四边端子和可选裸露焊盘。"""
    if fused.family_id != FAMILY_ID:
        raise ValueError(f"QFN/UFQFPN 规划器不能处理器件族：{fused.family_id}")
    indexed = {item.canonical_name: item for item in fused.parameters}
    missing = [name for name in REQUIRED_PARAMETERS if name not in indexed]
    if missing:
        raise ValueError(f"QFN/UFQFPN Feature IR 缺少关键参数：{missing}")
    for name in (*REQUIRED_PARAMETERS, *OPTIONAL_PARAMETERS):
        if name not in indexed:
            continue
        item = indexed[name]
        expected_unit = "count" if name == "nominal_pin_count" else "mm"
        if item.unit != expected_unit or not parameter_has_traceable_source(item):
            raise ValueError(f"QFN/UFQFPN 参数证据或单位无效：{name}")

    count_value = indexed["nominal_pin_count"].value
    count = int(round(count_value))
    if count < 4 or count % 4 or abs(count_value - count) > 1e-9:
        raise ValueError("QFN/UFQFPN 引脚数量必须是四的正整数倍")
    if any(indexed[name].value <= 0.0 for name in REQUIRED_PARAMETERS):
        raise ValueError("QFN/UFQFPN 尺寸必须大于零")
    total_height = indexed["total_height"].value
    standoff = indexed["body_standoff"].value
    terminal_height = indexed["terminal_height"].value
    if standoff >= total_height or terminal_height > total_height:
        raise ValueError("QFN/UFQFPN 高度尺寸链无效")

    length = indexed["body_length"].value
    width = indexed["body_width"].value
    exposed = all(name in indexed for name in OPTIONAL_PARAMETERS)
    if exposed and (
        indexed["exposed_pad_length"].value >= length
        or indexed["exposed_pad_width"].value >= width
    ):
        raise ValueError("QFN/UFQFPN 中心裸露焊盘必须小于封装本体")

    body = EvidenceCADFeature(
        feature_id="molded_body",
        feature_type="molded_body_box",
        parameters={
            "length": _value(indexed["body_length"]),
            "width": _value(indexed["body_width"]),
            "height": EvidenceValue(
                value=total_height - standoff,
                unit="mm",
                evidence_ids=list(dict.fromkeys([
                    *indexed["total_height"].evidence_ids,
                    *indexed["body_standoff"].evidence_ids,
                ])),
            ),
            "standoff": _value(indexed["body_standoff"]),
        },
    )
    terminal_parameters = {
        "count": _value(indexed["nominal_pin_count"]),
        "body_length": _value(indexed["body_length"]),
        "body_width": _value(indexed["body_width"]),
        "pitch": _value(indexed["terminal_pitch"]),
        "terminal_length": _value(indexed["terminal_length"]),
        "terminal_width": _value(indexed["terminal_width"]),
        "terminal_height": _value(indexed["terminal_height"]),
    }
    if exposed:
        terminal_parameters.update({
            "exposed_pad_length": _value(indexed["exposed_pad_length"]),
            "exposed_pad_width": _value(indexed["exposed_pad_width"]),
        })
    terminals = EvidenceCADFeature(
        feature_id="four_side_terminal_array",
        feature_type="quad_no_lead_terminal_array",
        parameters=terminal_parameters,
    )
    solid_count = count + 1 + int(exposed)
    return EvidenceFeatureIR(
        source_image_sha256=source_image_sha256,
        family_id=FAMILY_ID,
        category_id="ic",
        subcategory_id=None,
        package_type=fused.package_type,
        coordinate_system="X/Y 沿封装边，Z 向上；PCB 安装面为 Z=0。",
        features=[body, terminals],
        expected_geometry={
            "solid_count": solid_count,
            "minimum_solid_count": solid_count,
            "minimum_face_count": solid_count * 6,
            "bounding_box": {
                "xmin": -length / 2.0,
                "xmax": length / 2.0,
                "ymin": -width / 2.0,
                "ymax": width / 2.0,
                "zmin": 0.0,
                "zmax": total_height,
            },
        },
        source_dimensions={
            name: _value(indexed[name])
            for name in (*REQUIRED_PARAMETERS, *OPTIONAL_PARAMETERS)
            if name in indexed
        },
        assumptions=[
            "支持引脚数为四的正整数倍的 QFN/UFQFPN 四边无引脚封装。",
            "忽略丝印、Pin-1 凹点、圆角端子和未进入合同的局部细节。",
            *([] if exposed else ["图纸未提供 D2/E2，因此不生成中心裸露焊盘。"]),
        ],
    ).model_dump()
