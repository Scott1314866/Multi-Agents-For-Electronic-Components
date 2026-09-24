"""器件族规划器与几何执行器之间的稳定中间表示。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


FeatureType = Literal[
    "drafted_body_loft",
    "molded_body_box",
    "gullwing_lead_array",
    "quad_gullwing_lead_array",
    "quad_no_lead_terminal_array",
    "chip_body_box",
    "end_cap_pair",
    "dsub_shell_frame",
    "dsub_rear_housing",
    "dsub_right_angle_contacts",
    "dsub_mounting_hardware",
]


class CADFeature(BaseModel):
    """器件族规划器输出的一条白名单高级特征。"""

    feature_id: str
    feature_type: FeatureType
    parameters: dict[str, Any]


class DrawingFeatureIR(BaseModel):
    """通过证据门禁后，可交给确定性执行器的完整 Feature IR。"""

    case_id: str
    family_id: str
    part_type: str
    package_type: str
    coordinate_system: str
    features: list[CADFeature]
    expected_geometry: dict[str, Any]
    source_dimensions: dict[str, float]
    assumptions: list[str] = Field(default_factory=list)
    category_id: str = ""
    subcategory_id: str | None = None


class EvidenceValue(BaseModel):
    """Feature IR 中带视觉证据来源的一个数值。"""

    value: float
    unit: str
    evidence_ids: list[str] = Field(min_length=1)


class EvidenceCADFeature(BaseModel):
    """所有数值参数都必须可追溯的高级 CAD 特征。"""

    feature_id: str
    feature_type: FeatureType
    parameters: dict[str, EvidenceValue]


class EvidenceFeatureIR(BaseModel):
    """图片证据链生成的第二版 Feature IR。"""

    schema_version: Literal["2.0-evidence"] = "2.0-evidence"
    source_image_sha256: str
    family_id: str
    package_type: str
    coordinate_system: str
    features: list[EvidenceCADFeature]
    expected_geometry: dict[str, Any]
    source_dimensions: dict[str, EvidenceValue]
    assumptions: list[str] = Field(default_factory=list)
    category_id: str = ""
    subcategory_id: str | None = None
