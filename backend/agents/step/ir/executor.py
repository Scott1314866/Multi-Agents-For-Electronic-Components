"""Feature IR 到 CadQuery/OpenCascade 的确定性执行器。"""

from __future__ import annotations

from typing import Any, Callable

from backend.agents.step.features import (
    chip_body,
    dsub_shell,
    gullwing_lead,
    molded_body,
    molded_body_box,
    mounting_hardware,
    terminal_array,
    quad_gullwing_lead,
    quad_no_lead_terminal,
)
from backend.agents.step.ir.primitives import CadQueryPrimitives


FeatureBuilder = Callable[[CadQueryPrimitives, dict[str, Any]], list[Any]]


FEATURE_BUILDERS: dict[str, FeatureBuilder] = {
    "chip_body_box": chip_body.build_body,
    "end_cap_pair": chip_body.build_end_caps,
    "drafted_body_loft": molded_body.build,
    "molded_body_box": molded_body_box.build,
    "gullwing_lead_array": gullwing_lead.build,
    "quad_gullwing_lead_array": quad_gullwing_lead.build,
    "quad_no_lead_terminal_array": quad_no_lead_terminal.build,
    "dsub_shell_frame": dsub_shell.build_shell_frame,
    "dsub_rear_housing": dsub_shell.build_rear_housing,
    "dsub_right_angle_contacts": terminal_array.build,
    "dsub_mounting_hardware": mounting_hardware.build,
}


def build_feature_model(cq: Any, feature_ir: dict[str, Any]):
    """按注册顺序执行高级特征，并返回独立实体 Compound。

    图片证据版 IR 的每个数值必须包含 ``value``、``unit`` 和非空
    ``evidence_ids``；执行器在交给 Builder 前统一解包。
    """
    primitives = CadQueryPrimitives(cq)
    parts: list[Any] = []
    evidence_mode = feature_ir.get("schema_version") == "2.0-evidence"
    for feature in feature_ir["features"]:
        feature_type = str(feature["feature_type"])
        builder = FEATURE_BUILDERS.get(feature_type)
        if builder is None:
            raise ValueError(f"Feature IR 包含未注册特征：{feature_type}")
        parameters = feature["parameters"]
        if evidence_mode:
            unwrapped: dict[str, Any] = {}
            for name, parameter in parameters.items():
                if not isinstance(parameter, dict) or not parameter.get("evidence_ids"):
                    raise ValueError(f"Feature IR 参数缺少证据来源：{name}")
                if "value" not in parameter or not parameter.get("unit"):
                    raise ValueError(f"Feature IR 参数结构不完整：{name}")
                unwrapped[name] = parameter["value"]
            parameters = unwrapped
        parts.extend(builder(primitives, parameters))
    if not parts:
        raise ValueError("Feature IR 未产生任何几何实体")
    return primitives.compound(parts)
