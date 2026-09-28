"""MLCC、片式电阻等两端片式器件族规划器。"""

from __future__ import annotations

from typing import Any, Iterable

from backend.agents.step.drawing.gate import extraction_dimensions
from backend.agents.step.ir.schemas import (
    CADFeature,
    DrawingFeatureIR,
    EvidenceCADFeature,
    EvidenceFeatureIR,
    EvidenceValue,
)
from backend.agents.step.vision.schemas import (
    FusedEvidence, FusedParameter, parameter_has_traceable_source,
)


FAMILY_IDS: tuple[str, str] = (
    "resistor/two_terminal_chip",
    "capacitor/two_terminal_chip",
)

# 这里只定义可复用几何合同；具体数值必须来自当前图片证据。
REQUIRED_PARAMETERS: tuple[str, ...] = (
    "body_length",
    "body_width",
    "body_height",
    "terminal_length",
)

REQUIRED_FEATURES: tuple[str, ...] = (
    "chip_body",
    "end_cap_pair",
)


def _evidence_ids(parameters: Iterable[FusedParameter]) -> list[str]:
    """按原顺序合并一个派生尺寸依赖的证据 ID。"""
    return list(dict.fromkeys(
        evidence_id
        for parameter in parameters
        for evidence_id in parameter.evidence_ids
    ))


def _value(
    value: float,
    unit: str,
    parameters: Iterable[FusedParameter],
) -> EvidenceValue:
    """创建携带完整来源的 Feature IR 数值。"""
    return EvidenceValue(
        value=float(value),
        unit=unit,
        evidence_ids=_evidence_ids(parameters),
    )


def plan_from_evidence(
    fused: FusedEvidence,
    *,
    source_image_sha256: str,
) -> dict[str, Any]:
    """把两端片式器件的图片证据规划成参数化 Feature IR。

    Args:
        fused: 已完成数值解析、语义关联和门禁检查的图片证据。
        source_image_sha256: 当前唯一输入工程图的 SHA256。

    Returns:
        由中间本体和左右端电极组成的证据版 Feature IR。

    Raises:
        ValueError: Family、单位、证据或几何关系不满足合同时抛出。
    """
    if fused.family_id not in FAMILY_IDS:
        raise ValueError(
            f"两端片式器件规划器不能处理器件族：{fused.family_id or '<empty>'}"
        )
    if not source_image_sha256:
        raise ValueError("两端片式器件 Feature IR 缺少源图片 SHA256")
    indexed = {parameter.canonical_name: parameter for parameter in fused.parameters}
    missing = [name for name in REQUIRED_PARAMETERS if name not in indexed]
    if missing:
        raise ValueError(f"两端片式器件 Feature IR 缺少关键参数：{missing}")
    for name in REQUIRED_PARAMETERS:
        parameter = indexed[name]
        if parameter.unit != "mm":
            raise ValueError(f"两端片式器件参数单位错误：{name}={parameter.unit}")
        if not parameter_has_traceable_source(parameter):
            raise ValueError(f"两端片式器件参数缺少可追溯证据：{name}")
        if parameter.value <= 0.0:
            raise ValueError(f"两端片式器件参数必须大于零：{name}")

    body_length = indexed["body_length"]
    body_width = indexed["body_width"]
    body_height = indexed["body_height"]
    terminal_length = indexed["terminal_length"]
    center_length = body_length.value - 2.0 * terminal_length.value
    if center_length <= 0.0:
        raise ValueError("两个端电极长度之和必须小于器件总长")

    def source(name: str) -> EvidenceValue:
        parameter = indexed[name]
        return _value(parameter.value, parameter.unit, [parameter])

    body = EvidenceCADFeature(
        feature_id="chip_body",
        feature_type="chip_body_box",
        parameters={
            "length": _value(center_length, "mm", [body_length, terminal_length]),
            "width": source("body_width"),
            "height": source("body_height"),
        },
    )
    terminals = EvidenceCADFeature(
        feature_id="end_cap_pair",
        feature_type="end_cap_pair",
        parameters={
            "overall_length": source("body_length"),
            "terminal_length": source("terminal_length"),
            "width": source("body_width"),
            "height": source("body_height"),
        },
    )
    expected_geometry = {
        "minimum_solid_count": 3,
        "minimum_face_count": 18,
        "bounding_box": {
            "xmin": -body_length.value / 2.0,
            "xmax": body_length.value / 2.0,
            "ymin": -body_width.value / 2.0,
            "ymax": body_width.value / 2.0,
            "zmin": 0.0,
            "zmax": body_height.value,
        },
    }
    return EvidenceFeatureIR(
        source_image_sha256=source_image_sha256,
        family_id=fused.family_id,
        category_id=fused.category_id,
        subcategory_id=fused.subcategory_id,
        package_type=fused.package_type,
        coordinate_system=(
            "X 沿器件总长方向，Y 沿器件宽度方向，Z 向上；安装面为 Z=0。"
        ),
        features=[body, terminals],
        expected_geometry=expected_geometry,
        source_dimensions={name: source(name) for name in REQUIRED_PARAMETERS},
        assumptions=["图纸未给出内部电极层结构，STEP 仅表达外部本体和两个端电极。"],
    ).model_dump()


def plan(case: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    """兼容旧 Drawing Golden 流程的两端片式器件规划入口。"""
    indexed = extraction_dimensions(extraction)
    values = {
        name: float(indexed[name]["nominal_value"])
        for name in REQUIRED_PARAMETERS
    }
    center_length = values["body_length"] - 2.0 * values["terminal_length"]
    if center_length <= 0.0:
        raise ValueError("两个端电极长度之和必须小于器件总长")
    features = [
        CADFeature(
            feature_id="chip_body",
            feature_type="chip_body_box",
            parameters={
                "length": center_length,
                "width": values["body_width"],
                "height": values["body_height"],
            },
        ),
        CADFeature(
            feature_id="end_cap_pair",
            feature_type="end_cap_pair",
            parameters={
                "overall_length": values["body_length"],
                "terminal_length": values["terminal_length"],
                "width": values["body_width"],
                "height": values["body_height"],
            },
        ),
    ]
    return DrawingFeatureIR(
        case_id=str(case.get("case_id") or "two_terminal_chip"),
        family_id=str(case.get("family_id") or ""),
        part_type=str(case.get("expected_part_type") or "two_terminal_chip"),
        package_type=str(case.get("expected_package_type") or ""),
        coordinate_system="X 长、Y 宽、Z 高；安装面为 Z=0。",
        features=features,
        expected_geometry={
            "minimum_solid_count": 3,
            "minimum_face_count": 18,
            "bounding_box": {
                "xmin": -values["body_length"] / 2.0,
                "xmax": values["body_length"] / 2.0,
                "ymin": -values["body_width"] / 2.0,
                "ymax": values["body_width"] / 2.0,
                "zmin": 0.0,
                "zmax": values["body_height"],
            },
        },
        source_dimensions=values,
        assumptions=["图纸未给出内部电极层结构，模型仅表达外部结构。"],
        category_id=str(case.get("category_id") or ""),
        subcategory_id=case.get("subcategory_id"),
    ).model_dump()
