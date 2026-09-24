"""二维工程图证据与置信度门禁。做门控/检查，判断当前提取的数据够不够生成 CAD"""

from __future__ import annotations

from typing import Any


def extraction_dimensions(extraction: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把尺寸列表转换为按规范字段名索引的字典。"""
    result: dict[str, dict[str, Any]] = {}
    for item in extraction.get("dimensions", []):
        if not item.get("canonical_name"):
            continue
        result[str(item["canonical_name"])] = item
    return result


def _identity_matches(case: dict[str, Any], extraction: dict[str, Any]) -> tuple[bool, bool]:
    """按器件族兼容常见视觉模型语义别名。"""
    part_text = str(extraction.get("part_type") or "").casefold()
    package_text = str(extraction.get("package_type") or "").casefold()
    if case.get("family_id") in {"gullwing_ic", "ic/gullwing_ic"}:
        return (
            "sot" in part_text or "small outline transistor" in part_text,
            "sot-23" in package_text or "sot23" in package_text,
        )
    if case.get("family_id") in {
        "dsub_connector", "connector/cn/dsub_connector"
    }:
        compact = package_text.replace("-", "").replace("_", "").replace(" ", "")
        return (
            "dsub" in part_text.replace("-", "") or "d-sub" in part_text,
            ("dsub9" in compact or "db9" in compact)
            and ("female" in package_text or "母" in package_text)
            and ("right" in package_text or "90" in package_text),
        )
    return (
        extraction.get("part_type") == case["expected_part_type"],
        extraction.get("package_type") == case["expected_package_type"],
    )


def _feature_present(
    case: dict[str, Any], feature: str, features: set[str], dimensions: dict[str, Any]
) -> bool:
    """检查必要特征，并兼容同义命名。"""
    if feature == "drafted_molded_body":
        has_body = "molded_body" in features or feature in features
        has_draft = {
            "mold_draft_angle_top_deg", "mold_draft_angle_bottom_deg"
        }.issubset(dimensions)
        return has_body and has_draft
    if case.get("family_id") in {
        "dsub_connector", "connector/cn/dsub_connector"
    }:
        aliases = {
            "d_sub_shell": {"d_sub_shell", "dsub_shell", "metal_shell"},
            "female_contact_array": {"female_contact_array", "female_contacts"},
            "right_angle_dip_leads": {
                "right_angle_dip_leads", "right_angle_leads", "dip_leads"
            },
            "mounting_hardware": {
                "mounting_hardware", "mounting_screws", "mounting_posts"
            },
            "rear_insulator_housing": {
                "rear_insulator_housing", "insulator_housing", "rear_housing"
            },
        }
        return bool(features.intersection(aliases.get(feature, {feature})))
    return feature in features


def validate_golden_extraction(
    case: dict[str, Any],
    extraction: dict[str, Any],
    *,
    minimum_confidence: float = 0.85,
) -> dict[str, Any]:
    """按 Family 建模合同校验视觉提取，证据不足时硬阻断。

    普通回归用例可同时检查期望值；``blind_mode`` 只检查证据完整性，
    不在候选 STEP 生成前读取或比对 Golden 数值。
    """
    dimensions = extraction_dimensions(extraction)
    missing: list[str] = []
    conflicts: list[str] = []
    low_confidence: list[str] = []
    part_ok, package_ok = _identity_matches(case, extraction)
    if not part_ok:
        conflicts.append("part_type")
    if not package_ok:
        conflicts.append("package_type")

    features = set(extraction.get("identified_features", []))
    for feature in case.get("required_features", []):
        if not _feature_present(case, feature, features, dimensions):
            missing.append(f"feature:{feature}")

    for name, expectation in case.get("required_dimensions", {}).items():
        evidence = dimensions.get(name)
        if evidence is None or evidence.get("nominal_value") is None:
            missing.append(name)
            continue
        if evidence.get("evidence_type") == "visual_reference":
            conflicts.append(f"{name}:reference_not_numeric_evidence")
        if float(evidence.get("confidence", 0.0)) < minimum_confidence:
            low_confidence.append(name)
        expected = expectation.get("nominal")
        tolerance = expectation.get("tolerance")
        # 盲测只检查 Family 建模合同所需字段是否有图纸证据，不在建模前
        # 读取或比对 Golden 数值。数值差异只能在候选 STEP 生成后评估。
        if not case.get("blind_mode", False) and expected is not None and tolerance is not None:
            if abs(float(evidence["nominal_value"]) - float(expected)) > float(tolerance):
                conflicts.append(name)

    for name in case.get("known_missing_dimensions", []):
        if name not in missing:
            conflicts.append(f"{name}:not_explicitly_dimensioned")
            missing.append(name)

    unresolved = list(dict.fromkeys([
        *extraction.get("unresolved_required_fields", []),
        *case.get("known_missing_dimensions", []),
    ]))
    overall_confidence = float(extraction.get("overall_confidence", 0.0))
    passed = not missing and not conflicts and not low_confidence and not unresolved
    passed = passed and overall_confidence >= minimum_confidence
    return {
        "passed": passed,
        "missing_required_fields": list(dict.fromkeys(missing)),
        "conflicting_fields": list(dict.fromkeys(conflicts)),
        "low_confidence_fields": list(dict.fromkeys(low_confidence)),
        "unresolved_required_fields": unresolved,
        "overall_confidence": overall_confidence,
        "minimum_confidence": minimum_confidence,
        "stop_reason": "" if passed else "工程图提取信息不足，已在 CAD 建模前停止。",
    }
