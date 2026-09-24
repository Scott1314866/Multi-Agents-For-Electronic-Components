"""CAD Feature IR 与确定性执行器。"""

from backend.agents.step.ir.executor import build_feature_model
from backend.agents.step.ir.schemas import CADFeature, DrawingFeatureIR

__all__ = ["CADFeature", "DrawingFeatureIR", "build_feature_model"]
