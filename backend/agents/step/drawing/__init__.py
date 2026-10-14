"""
drawing:图纸信息抽取层
主要负责把工程图/器件图纸中的信息，变成程序能理解的结构化数据
二维工程图提取、证据门禁与兼容导出入口。
"""

from backend.agents.step.drawing.extractor import (
    DEFAULT_GOLDEN_SET_PATH,
    PROJECT_ROOT,
    image_as_data_url,
    load_golden_cases,
)
from backend.agents.step.drawing.gate import (
    extraction_dimensions,
    validate_golden_extraction,
)
from backend.agents.step.drawing.schemas import DrawingDimension, DrawingExtraction
from backend.agents.step.ir.executor import build_feature_model
from backend.agents.step.ir.schemas import CADFeature, DrawingFeatureIR

__all__ = [
    "CADFeature",
    "DEFAULT_GOLDEN_SET_PATH",
    "DrawingDimension",
    "DrawingExtraction",
    "DrawingFeatureIR",
    "PROJECT_ROOT",
    "build_feature_model",
    "create_feature_ir",
    "extraction_dimensions",
    "image_as_data_url",
    "load_golden_cases",
    "validate_golden_extraction",
]


def __getattr__(name: str):
    if name!='create_feature_ir':
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    # Family planners import drawing.gate; importing the registry eagerly here
    # would re-enter the partially initialized family.
    from backend.agents.step.families.registry import create_feature_ir
    globals()[name]=create_feature_ir
    return create_feature_ir
