"""完全由证据参数驱动的 D-SUB 连接器器件族规划器。"""

from __future__ import annotations

from typing import Iterable

from backend.agents.step.ir.schemas import (
    EvidenceCADFeature,
    EvidenceFeatureIR,
    EvidenceValue,
)
from backend.agents.step.vision.schemas import (
    FusedEvidence, FusedParameter, parameter_has_traceable_source,
)


FAMILY_ID = "connector/cn/dsub_connector"

# 这里只定义建模合同字段，不包含任何型号尺寸或 Golden 数值。
REQUIRED_PARAMETERS: tuple[str, ...] = (
    "circuit_count",
    "overall_width",
    "front_plate_height",
    "plate_thickness",
    "front_shell_top_width",
    "front_shell_bottom_width",
    "front_shell_height",
    "front_projection_depth",
    "shell_wall_thickness",
    "mounting_center_span",
    "mounting_hole_diameter",
    "mounting_outer_diameter",
    "rear_housing_width",
    "rear_body_depth",
    "rear_housing_height",
    "contact_pitch",
    "contact_column_span",
    "contact_row_spacing",
    "contact_outer_diameter",
    "contact_inner_diameter",
    "signal_pin_width",
    "pin_tail_length",
    "overall_height",
)

REQUIRED_FEATURES: tuple[str, ...] = (
    "front_metal_shell",
    "mounting_flange",
    "rear_insulator_housing",
    "female_contact_array",
    "right_angle_leads",
    "mounting_hardware",
)


def _evidence_ids(parameters: Iterable[FusedParameter]) -> list[str]:
    """合并派生参数所依赖的证据 ID。"""
    return list(dict.fromkeys(
        evidence_id
        for parameter in parameters
        for evidence_id in parameter.evidence_ids
    ))


def _value(value: float, unit: str, parameters: Iterable[FusedParameter]) -> EvidenceValue:
    """创建带完整来源的 Feature IR 数值。"""
    return EvidenceValue(
        value=float(value), unit=unit, evidence_ids=_evidence_ids(parameters)
    )


def plan_from_evidence(
    fused: FusedEvidence,
    *,
    source_image_sha256: str,
) -> dict:
    """把已通过门禁的 D-SUB 证据规划为参数化 Feature IR。

    Args:
        fused: 已融合且通过门禁的尺寸证据。
        source_image_sha256: 唯一输入图片的哈希。

    Returns:
        所有数值均携带 OCR/几何证据 ID 的 Feature IR。

    Raises:
        ValueError: 缺字段、单位错误或证据为空。
    """
    indexed = {parameter.canonical_name: parameter for parameter in fused.parameters}
    missing = [name for name in REQUIRED_PARAMETERS if name not in indexed]
    if missing:
        raise ValueError(f"D-SUB Feature IR 缺少关键参数：{missing}")
    for name in REQUIRED_PARAMETERS:
        parameter = indexed[name]
        if not parameter_has_traceable_source(parameter):
            raise ValueError(f"D-SUB 参数缺少可追溯证据：{name}")
        expected_unit = "count" if name == "circuit_count" else "mm"
        if parameter.unit != expected_unit:
            raise ValueError(f"D-SUB 参数单位错误：{name}={parameter.unit}")

    def source(name: str) -> EvidenceValue:
        parameter = indexed[name]
        return _value(parameter.value, parameter.unit, [parameter])

    count = int(round(indexed["circuit_count"].value))
    plate_bottom = indexed["pin_tail_length"].value
    plate_y = 0.0
    front_y = -indexed["front_projection_depth"].value
    rear_y = indexed["rear_body_depth"].value
    shell_center_z = plate_bottom + indexed["front_plate_height"].value / 2.0

    features = [
        EvidenceCADFeature(
            feature_id="front_shell_and_flange",
            feature_type="dsub_shell_frame",
            parameters={
                "overall_width": source("overall_width"),
                "plate_height": source("front_plate_height"),
                "plate_thickness": source("plate_thickness"),
                "shell_top_width": source("front_shell_top_width"),
                "shell_bottom_width": source("front_shell_bottom_width"),
                "shell_height": source("front_shell_height"),
                "shell_depth": source("front_projection_depth"),
                "shell_wall": source("shell_wall_thickness"),
                "mounting_center_span": source("mounting_center_span"),
                "mounting_hole_diameter": source("mounting_hole_diameter"),
                "front_y": _value(front_y, "mm", [indexed["front_projection_depth"]]),
                "plate_y": _value(plate_y, "mm", [indexed["plate_thickness"]]),
                "plate_bottom_z": _value(plate_bottom, "mm", [indexed["pin_tail_length"]]),
                "shell_center_z": _value(
                    shell_center_z,
                    "mm",
                    [indexed["pin_tail_length"], indexed["front_plate_height"]],
                ),
            },
        ),
        EvidenceCADFeature(
            feature_id="rear_insulator_housing",
            feature_type="dsub_rear_housing",
            parameters={
                "width": source("rear_housing_width"),
                "depth": source("rear_body_depth"),
                "height": source("rear_housing_height"),
                "y_min": _value(plate_y, "mm", [indexed["plate_thickness"]]),
                "y_max": _value(rear_y, "mm", [indexed["rear_body_depth"]]),
                "z_min": _value(plate_bottom, "mm", [indexed["pin_tail_length"]]),
            },
        ),
        EvidenceCADFeature(
            feature_id="female_contacts_and_right_angle_leads",
            feature_type="dsub_right_angle_contacts",
            parameters={
                "count": source("circuit_count"),
                "pitch": source("contact_pitch"),
                "column_span": source("contact_column_span"),
                "row_spacing": source("contact_row_spacing"),
                "contact_outer_diameter": source("contact_outer_diameter"),
                "contact_inner_diameter": source("contact_inner_diameter"),
                "pin_width": source("signal_pin_width"),
                "tail_length": source("pin_tail_length"),
                "front_y": _value(front_y, "mm", [indexed["front_projection_depth"]]),
                "bend_y": _value(rear_y, "mm", [indexed["rear_body_depth"]]),
                "shell_center_z": _value(
                    shell_center_z,
                    "mm",
                    [indexed["pin_tail_length"], indexed["front_plate_height"]],
                ),
            },
        ),
        EvidenceCADFeature(
            feature_id="mounting_hardware",
            feature_type="dsub_mounting_hardware",
            parameters={
                "center_span": source("mounting_center_span"),
                "hole_diameter": source("mounting_hole_diameter"),
                "outer_diameter": source("mounting_outer_diameter"),
                "front_y": _value(front_y, "mm", [indexed["front_projection_depth"]]),
                "rear_y": _value(rear_y, "mm", [indexed["rear_body_depth"]]),
                "center_z": _value(
                    shell_center_z,
                    "mm",
                    [indexed["pin_tail_length"], indexed["front_plate_height"]],
                ),
                "top_z": source("overall_height"),
            },
        ),
    ]
    expected_geometry = {
        "minimum_solid_count": count + 7,
        "minimum_face_count": 50,
        "bounding_box": {
            "xmin": -indexed["overall_width"].value / 2.0,
            "xmax": indexed["overall_width"].value / 2.0,
            "ymin": front_y,
            "ymax": rear_y,
            "zmin": 0.0,
            "zmax": indexed["overall_height"].value,
        },
    }
    return EvidenceFeatureIR(
        source_image_sha256=source_image_sha256,
        family_id=FAMILY_ID,
        category_id="connector",
        subcategory_id="cn",
        package_type=fused.package_type,
        coordinate_system=(
            "X 沿连接器宽度，Y 从插合面指向后部，Z 向上；PCB 安装面为 Z=0。"
        ),
        features=features,
        expected_geometry=expected_geometry,
        source_dimensions={name: source(name) for name in REQUIRED_PARAMETERS},
        assumptions=[],
    ).model_dump()


def plan(case: dict, extraction: dict) -> dict:
    """阻止旧 Golden 数值接口继续为 D-SUB 建模。"""
    raise RuntimeError("D-SUB 已切换为图片证据链，请使用 plan_from_evidence()")
