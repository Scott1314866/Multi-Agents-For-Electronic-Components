"""四边鸥翼引脚阵列高级特征。"""

from __future__ import annotations

from typing import Any

from backend.agents.step.features import gullwing_lead


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """复用双边引脚特征生成 X/Y 两组正交阵列。

    Args:
        primitives: 通用 CadQuery 基础几何操作。
        parameters: 总引脚数、间距、阵列跨距、本体/总体长宽及引脚截面尺寸。

    Returns:
        四边独立鸥翼引脚实体列表。

    失败状态:
        引脚数不能被四整除，或尺寸链不一致时抛出 ``ValueError``。
    """
    count = int(parameters["count"])
    if count < 4 or count % 4:
        raise ValueError("四边鸥翼引脚数量必须是四的正整数倍")
    pair_count = count // 2
    common = {
        "count": pair_count,
        "pitch": float(parameters["pitch"]),
        "row_span": float(parameters["terminal_span"]),
        "foot_length": float(parameters["foot_length"]),
        "lead_width": float(parameters["lead_width"]),
        "lead_thickness": float(parameters["lead_thickness"]),
        "body_standoff": float(parameters["body_standoff"]),
        "body_height": float(parameters["body_height"]),
    }
    y_sides = gullwing_lead.build(primitives, {
        **common,
        "overall_width": float(parameters["overall_width"]),
        "body_width": float(parameters["body_width"]),
    })
    x_sides_local = gullwing_lead.build(primitives, {
        **common,
        "overall_width": float(parameters["overall_length"]),
        "body_width": float(parameters["body_length"]),
    })
    x_sides = [
        shape.rotate((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), 90.0)
        for shape in x_sides_local
    ]
    return [*y_sides, *x_sides]
