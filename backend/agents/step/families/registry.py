"""器件族规划器映射。"""

from __future__ import annotations

import re
from typing import Any, Callable

from backend.agents.step.drawing.gate import validate_golden_extraction
from backend.agents.step.families.capacitor import (
    two_terminal_chip as capacitor_chip,
)
from backend.agents.step.families.connector.cn import dsub_connector
from backend.agents.step.families.ic import gullwing_ic, qfn_ufqfpn, quad_gullwing_ic
from backend.agents.step.families.resistor import two_terminal_chip as resistor_chip
from backend.agents.step.families.taxonomy import (
    CAPACITOR_CHIP_FAMILY_ID,
    RESISTOR_CHIP_FAMILY_ID,
    classification_catalog,
    component_category_catalog,
    resolve_family_selection,
)
from backend.agents.step.vision.schemas import FusedEvidence, FusedParameter


FamilyPlanner = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


_EXPLICIT_TERMINAL_COUNT_RE = re.compile(
    r"\b(\d{1,3})\s*[-–—]?\s*(?:lead|pin|circuit)s?\b",
    re.IGNORECASE,
)
_GULLWING_PACKAGE_RE = re.compile(
    r"\b(?:SOT\s*[-–—]?\s*\d+|SOIC|TSSOP|SSOP|MSOP)\b|"
    r"small\s+outline\s+transistor|gull\s*wing",
    re.IGNORECASE,
)
_QUAD_GULLWING_PACKAGE_RE = re.compile(
    r"\b(?:L?QFP|TQFP)\s*\d*\b|quad\s+flat\s+package",
    re.IGNORECASE,
)
_QFN_UFQFPN_PACKAGE_RE = re.compile(
    r"\b(?:U?F?QFPN|QFN|UQFNP)(?:\s*[-–—]?\s*\d+)?\b", re.I
)


def _has_qfn_ufqfpn_signature(texts: list[str]) -> bool:
    """根据封装名或 D2/E2 裸露焊盘尺寸符号识别四边无引脚拓扑。"""
    if _QFN_UFQFPN_PACKAGE_RE.search(" ".join(texts)):
        return True
    symbols = {re.sub(r"[^A-Za-z0-9]", "", text).upper() for text in texts}
    return {"D2", "E2"}.issubset(symbols) and len(
        symbols & {"A3", "B", "L", "L2"}
    ) >= 2

FAMILY_PLANNERS: dict[str, FamilyPlanner] = {
    gullwing_ic.FAMILY_ID: gullwing_ic.plan,
    dsub_connector.FAMILY_ID: dsub_connector.plan,
    resistor_chip.FAMILY_ID: resistor_chip.plan,
    capacitor_chip.FAMILY_ID: capacitor_chip.plan,
}


def image_family_catalog() -> dict[str, dict[str, Any]]:
    """返回图片 Agent 可执行的器件族合同，不包含占位模板。"""
    return {
        dsub_connector.FAMILY_ID: {
            "required_parameters": list(dsub_connector.REQUIRED_PARAMETERS),
            "required_features": list(dsub_connector.REQUIRED_FEATURES),
        },
        gullwing_ic.FAMILY_ID: {
            "required_parameters": list(gullwing_ic.REQUIRED_PARAMETERS),
            "optional_parameters": list(gullwing_ic.OPTIONAL_PARAMETERS),
            "required_features": list(gullwing_ic.REQUIRED_FEATURES),
            "parameter_guidance": getattr(gullwing_ic, "PARAMETER_GUIDANCE", {}),
            "view_parameter_groups": getattr(
                gullwing_ic, "VIEW_PARAMETER_GROUPS", {}
            ),
        },
        quad_gullwing_ic.FAMILY_ID: {
            "required_parameters": list(quad_gullwing_ic.REQUIRED_PARAMETERS),
            "required_features": list(quad_gullwing_ic.REQUIRED_FEATURES),
            "parameter_guidance": quad_gullwing_ic.PARAMETER_GUIDANCE,
        },
        qfn_ufqfpn.FAMILY_ID: {
            "required_parameters": list(qfn_ufqfpn.REQUIRED_PARAMETERS),
            "optional_parameters": list(qfn_ufqfpn.OPTIONAL_PARAMETERS),
            "required_features": list(qfn_ufqfpn.REQUIRED_FEATURES),
            "parameter_guidance": qfn_ufqfpn.PARAMETER_GUIDANCE,
        },
        resistor_chip.FAMILY_ID: {
            "required_parameters": list(resistor_chip.REQUIRED_PARAMETERS),
            "required_features": list(resistor_chip.REQUIRED_FEATURES),
        },
        capacitor_chip.FAMILY_ID: {
            "required_parameters": list(capacitor_chip.REQUIRED_PARAMETERS),
            "required_features": list(capacitor_chip.REQUIRED_FEATURES),
        },
    }


def template_category_catalog() -> dict[str, Any]:
    """返回包含全部八类和未实现占位模板的展示/分类目录。"""
    return classification_catalog(image_family_catalog())


def reconcile_image_family_classification(
    classification: Any,
    ocr_tokens: list[Any],
) -> Any:
    """用明确的 OCR 身份证据校验 Qwen 器件族分类。

    Args:
        classification: 已通过 Schema 校验的第一阶段 Qwen 分类结果。
        ocr_tokens: 唯一输入工程图产生的完整 OCR token；只读取文字和置信度，
            不读取任何 Golden、参考模型或人工尺寸。

    Returns:
        已迁移到层级 Family、并与 OCR 身份证据相容的分类结果。

    失败状态:
        本函数不抛出业务异常；证据冲突且无法确定目标 Family 时返回空
        ``family_id``，由 Feature IR 前的白名单门禁停止。
    """
    def token_field(token: Any, name: str, default: Any) -> Any:
        return token.get(name, default) if isinstance(token, dict) else getattr(
            token, name, default
        )

    reliable_texts = [
        str(token_field(token, "text", ""))
        for token in ocr_tokens
        if float(token_field(token, "confidence", 0.0)) >= 0.80
    ]
    joined_text = " \n ".join(reliable_texts)

    def signature_ambiguities(family_id: str, package_pattern: Any, reason: str) -> list[str]:
        """Successful identity confirmation is not a new ambiguity.

        Keep model-reported uncertainty and require confirmation when OCR
        actually changes the declared family or contradicts its package name.
        """
        existing = list(getattr(classification, "ambiguities", []))
        declared = resolve_family_selection(
            str(getattr(classification, "family_id", "")),
            category_id=str(getattr(classification, "category_id", "")),
            subcategory_id=getattr(classification, "subcategory_id", None),
        )
        package_type = str(getattr(classification, "package_type", ""))
        if (
            declared["status"] == "resolved"
            and declared["family_id"] == family_id
            and (not package_type or package_pattern.search(package_type))
        ):
            return existing
        return list(dict.fromkeys([*existing, reason]))

    if _has_qfn_ufqfpn_signature(reliable_texts):
        contract = image_family_catalog()[qfn_ufqfpn.FAMILY_ID]
        return classification.model_copy(update={
            "category_id": "ic",
            "subcategory_id": None,
            "family_id": qfn_ufqfpn.FAMILY_ID,
            "package_type": (
                classification.package_type
                if _QFN_UFQFPN_PACKAGE_RE.search(classification.package_type)
                else "QFN/UFQFPN"
            ),
            "identified_features": list(contract["required_features"]),
            "unresolved_fields": list(contract["required_parameters"]),
            "ambiguities": signature_ambiguities(
                qfn_ufqfpn.FAMILY_ID, _QFN_UFQFPN_PACKAGE_RE,
                "OCR 无引脚封装证据与原始身份分类不一致，需确认 QFN/UFQFPN 模板。",
            ),
            "overall_confidence": min(
                float(getattr(classification, "overall_confidence", 0.0)), 0.95
            ),
        })
    if _QUAD_GULLWING_PACKAGE_RE.search(joined_text):
        contract = image_family_catalog()[quad_gullwing_ic.FAMILY_ID]
        return classification.model_copy(update={
            "category_id": "ic",
            "subcategory_id": None,
            "family_id": quad_gullwing_ic.FAMILY_ID,
            "identified_features": list(contract["required_features"]),
            "unresolved_fields": list(contract["required_parameters"]),
            "ambiguities": signature_ambiguities(
                quad_gullwing_ic.FAMILY_ID, _QUAD_GULLWING_PACKAGE_RE,
                "OCR QFP/LQFP 标题与原始身份分类不一致，需确认四边鸥翼模板。",
            ),
            "overall_confidence": min(
                float(getattr(classification, "overall_confidence", 0.0)), 0.95
            ),
        })
    explicit_counts = {
        int(match.group(1))
        for match in _EXPLICIT_TERMINAL_COUNT_RE.finditer(joined_text)
    }
    multi_terminal_counts = sorted(count for count in explicit_counts if count > 2)
    raw_family_id = str(getattr(classification, "family_id", ""))
    chip_ids = {
        "two_terminal_chip",
        RESISTOR_CHIP_FAMILY_ID,
        CAPACITOR_CHIP_FAMILY_ID,
    }
    if raw_family_id in chip_ids and multi_terminal_counts:
        reason = (
            "确定性 Family 校验：OCR 明确给出多引脚数量 "
            f"{multi_terminal_counts}，与两端片式模板冲突。"
        )
        ambiguities = [*getattr(classification, "ambiguities", []), reason]
        if _GULLWING_PACKAGE_RE.search(joined_text):
            contract = image_family_catalog()[gullwing_ic.FAMILY_ID]
            return classification.model_copy(update={
                "category_id": "ic",
                "subcategory_id": None,
                "family_id": gullwing_ic.FAMILY_ID,
                "identified_features": list(contract["required_features"]),
                "unresolved_fields": list(contract["required_parameters"]),
                "ambiguities": ambiguities,
                "overall_confidence": min(
                    float(getattr(classification, "overall_confidence", 0.0)),
                    0.90,
                ),
            })
        return classification.model_copy(update={
            "family_id": "",
            "identified_features": [],
            "unresolved_fields": ["supported_family"],
            "ambiguities": ambiguities,
            "overall_confidence": 0.0,
        })

    selection = resolve_family_selection(
        raw_family_id,
        category_id=str(getattr(classification, "category_id", "")),
        subcategory_id=getattr(classification, "subcategory_id", None),
        identity_texts=reliable_texts,
    )
    if selection["status"] == "resolved":
        family_id = selection["family_id"]
        contract = image_family_catalog().get(family_id)
        updates: dict[str, Any] = {
            "category_id": selection["category_id"],
            "subcategory_id": selection["subcategory_id"],
            "family_id": family_id,
        }
        if contract and not getattr(classification, "identified_features", []):
            updates["identified_features"] = list(contract["required_features"])
            updates["unresolved_fields"] = list(contract["required_parameters"])
        return classification.model_copy(update=updates)

    unresolved_marker = (
        "human_follow_up:family_id"
        if selection["status"] == "needs_human_follow_up"
        else "supported_family"
    )
    return classification.model_copy(update={
        "category_id": selection["category_id"],
        "subcategory_id": selection["subcategory_id"],
        "family_id": "",
        "identified_features": [],
        "unresolved_fields": list(dict.fromkeys([
            *getattr(classification, "unresolved_fields", []),
            unresolved_marker,
        ])),
        "ambiguities": list(dict.fromkeys([
            *getattr(classification, "ambiguities", []),
            selection["reason"],
        ])),
        "overall_confidence": 0.0,
    })


def create_evidence_feature_ir(
    family_id: str,
    fused_evidence: dict[str, Any],
    *,
    source_image_sha256: str,
) -> dict[str, Any]:
    """调用图片证据版 Family 规划器。"""
    from backend.agents.step.vision.schemas import FusedEvidence

    fused = FusedEvidence.model_validate(fused_evidence)
    selection = resolve_family_selection(
        family_id,
        category_id=fused.category_id,
        subcategory_id=fused.subcategory_id,
        identity_texts=[fused.package_type],
    )
    if selection["status"] == "needs_human_follow_up":
        raise ValueError(
            "needs_human_follow_up: "
            f"{selection['reason']} 候选：{selection['candidates']}"
        )
    normalized_family_id = str(selection.get("family_id") or "")
    planners = {
        dsub_connector.FAMILY_ID: dsub_connector.plan_from_evidence,
        gullwing_ic.FAMILY_ID: gullwing_ic.plan_from_evidence,
        quad_gullwing_ic.FAMILY_ID: quad_gullwing_ic.plan_from_evidence,
        qfn_ufqfpn.FAMILY_ID: qfn_ufqfpn.plan_from_evidence,
        resistor_chip.FAMILY_ID: resistor_chip.plan_from_evidence,
        capacitor_chip.FAMILY_ID: capacitor_chip.plan_from_evidence,
    }
    planner = planners.get(normalized_family_id)
    if planner is None:
        raise NotImplementedError(
            "图片 Agent 尚未实现器件族："
            f"{normalized_family_id or family_id or '<empty>'}"
        )
    fused = fused.model_copy(update={
        "category_id": selection["category_id"],
        "subcategory_id": selection["subcategory_id"],
        "family_id": normalized_family_id,
    })
    return planner(
        fused,
        source_image_sha256=source_image_sha256,
    )


def _derived_parameter(
    name: str,
    value: float,
    unit: str,
    sources: list[FusedParameter],
    formula: str,
) -> FusedParameter:
    """从同一输入图的已融合参数构造可追溯尺寸链结果。"""
    image_sources = [item for item in sources if item.evidence_kind != "human_input"]
    return FusedParameter(
        canonical_name=name,
        value=float(value),
        unit=unit,
        evidence_ids=list(dict.fromkeys(
            evidence_id for source in sources for evidence_id in source.evidence_ids
        )),
        token_ids=list(dict.fromkeys(
            token_id for source in sources for token_id in source.token_ids
        )),
        line_ids=list(dict.fromkeys(
            line_id for source in sources for line_id in source.line_ids
        )),
        token_bboxes=list(dict.fromkeys(
            bbox for source in sources for bbox in source.token_bboxes
        )),
        target_feature=f"derived_chain:{formula}",
        ocr_confidence=min((source.ocr_confidence for source in image_sources), default=0.0),
        semantic_confidence=min((source.semantic_confidence for source in image_sources), default=0.0),
        evidence_kind="derived",
        raw_texts=[
            *[text for source in sources for text in source.raw_texts],
            f"derived:{formula}",
        ],
    )


def derive_family_parameters(fused: FusedEvidence) -> FusedEvidence:
    """在门禁前执行白名单 Family 的确定性尺寸链派生。

    只消费已有 OCR/几何证据参数，不读取参考图、Golden STEP 或型号尺寸库。
    """
    parameters = list(fused.parameters)
    indexed = {item.canonical_name: item for item in parameters}
    derived_names: set[str] = set()

    def add(name: str, value: float, sources: list[FusedParameter], formula: str) -> None:
        if name in indexed or not sources:
            return
        item = _derived_parameter(name, value, "mm", sources, formula)
        parameters.append(item)
        indexed[name] = item
        derived_names.add(name)

    if fused.family_id == gullwing_ic.FAMILY_ID:
        if "pin_span" not in indexed and {"nominal_pin_count", "terminal_pitch"}.issubset(indexed):
            count_value = indexed["nominal_pin_count"].value
            count = int(round(count_value))
            # 只有可由双侧对称多脚布局推出的跨度才允许派生。
            # 显式跨度保留给尺寸链门禁检查，不能用推导值覆盖冲突证据。
            if count >= 4 and count % 2 == 0 and abs(count_value - count) <= 1e-9:
                add(
                    "pin_span",
                    (count // 2 - 1) * indexed["terminal_pitch"].value,
                    [indexed["nominal_pin_count"], indexed["terminal_pitch"]],
                    "(nominal_pin_count/2-1)*terminal_pitch",
                )
        if {"total_height", "body_standoff"}.issubset(indexed):
            add(
                "housing_height",
                indexed["total_height"].value - indexed["body_standoff"].value,
                [indexed["total_height"], indexed["body_standoff"]],
                "total_height-body_standoff",
            )
    if fused.family_id == quad_gullwing_ic.FAMILY_ID:
        if {"total_height", "body_standoff"}.issubset(indexed):
            sources = [indexed["total_height"], indexed["body_standoff"]]
            add(
                "housing_height",
                sources[0].value - sources[1].value,
                sources,
                "total_height-body_standoff",
            )
        if (
            "overall_length" not in indexed
            and {"overall_width", "body_length", "body_width"}.issubset(indexed)
            and abs(indexed["body_length"].value - indexed["body_width"].value) <= 0.05
        ):
            source = indexed["overall_width"]
            copied = source.model_copy(update={
                "canonical_name": "overall_length",
                "target_feature": "derived_chain:square_package_symmetry",
            })
            parameters.append(copied)
            indexed[copied.canonical_name] = copied
            derived_names.add(copied.canonical_name)
        if (
            "overall_width" not in indexed
            and {"overall_length", "body_length", "body_width"}.issubset(indexed)
            and abs(indexed["body_length"].value - indexed["body_width"].value) <= 0.05
        ):
            source = indexed["overall_length"]
            copied = source.model_copy(update={
                "canonical_name": "overall_width",
                "target_feature": "derived_chain:square_package_symmetry",
            })
            parameters.append(copied)
            indexed[copied.canonical_name] = copied
            derived_names.add(copied.canonical_name)
        if {"nominal_pin_count", "terminal_span"}.issubset(indexed):
            per_side = int(round(indexed["nominal_pin_count"].value)) // 4
            if per_side > 1:
                add(
                    "terminal_pitch",
                    indexed["terminal_span"].value / (per_side - 1),
                    [indexed["nominal_pin_count"], indexed["terminal_span"]],
                    "terminal_span/(nominal_pin_count/4-1)",
                )
        length_sources = [
            indexed[name]
            for name in ("overall_length", "body_length", "overall_width", "body_width")
            if name in indexed
        ]
        if {"overall_length", "body_length", "overall_width", "body_width"}.issubset(indexed):
            x_length = (indexed["overall_length"].value - indexed["body_length"].value) / 2.0
            y_length = (indexed["overall_width"].value - indexed["body_width"].value) / 2.0
            if abs(x_length - y_length) <= 0.05:
                add(
                    "lead_projection",
                    (x_length + y_length) / 2.0,
                    length_sources,
                    "mean((overall_length-body_length)/2,(overall_width-body_width)/2)",
                )

    if fused.family_id == qfn_ufqfpn.FAMILY_ID:
        identity = indexed.get("nominal_pin_count")
        identity_text = " ".join(identity.raw_texts) if identity else ""
        size_match = re.search(
            r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm",
            identity_text,
            re.I,
        )
        if identity and size_match:
            add("body_length", float(size_match.group(1)), [identity], "package_title_length")
            add("body_width", float(size_match.group(2)), [identity], "package_title_width")
        pitch_match = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*pitch", identity_text, re.I)
        if identity and pitch_match:
            add("terminal_pitch", float(pitch_match.group(1)), [identity], "package_title_pitch")

    return fused.model_copy(update={
        "parameters": parameters,
        "missing_evidence_assignments": [
            name for name in fused.missing_evidence_assignments if name not in derived_names
        ],
        "unresolved_fields": [
            name for name in fused.unresolved_fields if name not in derived_names
        ],
        "ambiguities": [
            *fused.ambiguities,
            *[f"{name}:由同图尺寸链确定性派生" for name in sorted(derived_names)],
        ],
    })


def resolve_family(case: dict[str, Any]) -> FamilyPlanner:
    """根据 family_id 返回普通规划函数。"""
    selection = resolve_family_selection(
        str(case.get("family_id") or ""),
        category_id=str(case.get("category_id") or ""),
        subcategory_id=case.get("subcategory_id"),
        identity_texts=[
            str(case.get("name") or ""),
            str(case.get("expected_part_type") or ""),
            str(case.get("expected_package_type") or ""),
        ],
    )
    family_id = str(selection.get("family_id") or "")
    if selection["status"] == "needs_human_follow_up":
        raise ValueError(
            "needs_human_follow_up: "
            f"{selection['reason']} 候选：{selection['candidates']}"
        )
    planner = FAMILY_PLANNERS.get(family_id)
    if planner is None:
        raise NotImplementedError(
            f"数模模板尚未实现：{family_id or case.get('family_id') or '<empty>'}"
        )
    return planner


def create_feature_ir(case: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    """通过证据门禁后调用已注册器件族规划器。"""
    selection = resolve_family_selection(
        str(case.get("family_id") or ""),
        category_id=str(case.get("category_id") or ""),
        subcategory_id=case.get("subcategory_id"),
        identity_texts=[
            str(case.get("name") or ""),
            str(extraction.get("part_type") or ""),
            str(extraction.get("package_type") or ""),
        ],
    )
    normalized_case = {
        **case,
        "category_id": selection["category_id"],
        "subcategory_id": selection["subcategory_id"],
        "family_id": selection["family_id"],
    }
    gate = validate_golden_extraction(normalized_case, extraction)
    if not gate["passed"]:
        raise ValueError(gate["stop_reason"])
    return resolve_family(normalized_case)(normalized_case, extraction)
