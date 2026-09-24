"""定义图纸结构化数据"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator


class DrawingDimension(BaseModel):
    """视觉模型从工程图读取的一条尺寸证据。"""

    canonical_name: str
    symbol: str = ""
    raw_value: str
    nominal_value: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("nominal_value", "nominal_mm")
    )
    minimum_value: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("minimum_value", "minimum_mm")
    )
    maximum_value: Optional[float] = Field(
        default=None, validation_alias=AliasChoices("maximum_value", "maximum_mm")
    )
    tolerance_plus: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("tolerance_plus", "tolerance_plus_mm"),
    )
    tolerance_minus: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("tolerance_minus", "tolerance_minus_mm"),
    )
    unit: str = "mm"
    evidence_text: str
    evidence_type: Literal[
        "drawing_label", "dimension_table", "derived_chain", "visual_reference"
    ]
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> str:
        """没有字母符号的直接尺寸允许模型返回 null。"""
        return "" if value is None else str(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: Any) -> Any:
        """兼容视觉模型偶尔返回的定性置信度。"""
        if isinstance(value, str):
            mapped = {"high": 0.95, "medium": 0.70, "low": 0.40}.get(value.casefold())
            return mapped if mapped is not None else value
        return value


class DrawingExtraction(BaseModel):
    """二维图纸理解节点的结构化输出。"""

    part_type: str
    package_type: str
    identified_views: list[str] = Field(default_factory=list)
    identified_features: list[str] = Field(default_factory=list)
    dimensions: list[DrawingDimension] = Field(default_factory=list)
    unresolved_required_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("overall_confidence", mode="before")
    @classmethod
    def normalize_overall_confidence(cls, value: Any) -> Any:
        """把定性置信度规范化为 0–1 数值。"""
        if isinstance(value, str):
            mapped = {"high": 0.95, "medium": 0.70, "low": 0.40}.get(value.casefold())
            return mapped if mapped is not None else value
        return value
