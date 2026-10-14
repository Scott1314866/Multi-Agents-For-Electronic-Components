"""鸥翼引脚及两侧阵列特征。"""

from __future__ import annotations

from typing import Any
from backend.agents.step.features.gullwing_layout import row_positions, row_widths, validate_row_envelopes, row_profile_values, ROW_LENGTH_PARAMETERS, ROW_THICKNESS_PARAMETERS


def _profile(parameters: dict[str, Any], side: int) -> list[tuple[float, float]]:
    """生成一个鸥翼引脚在 YZ 平面上的闭合截面。"""
    index=0 if side<0 else 1
    thickness = row_profile_values(parameters,ROW_THICKNESS_PARAMETERS,"lead_thickness")[index]
    foot_length = row_profile_values(parameters,ROW_LENGTH_PARAMETERS,"foot_length")[index]
    body_edge = float(parameters["body_width"]) / 2.0 - min(0.03, thickness * 0.2)
    outer_edge = float(parameters["overall_width"]) / 2.0
    foot_inner = outer_edge - foot_length
    exit_z = float(parameters["body_standoff"]) + float(parameters["body_height"]) * 0.58
    if any(name in parameters for name in ROW_LENGTH_PARAMETERS):
        if foot_inner<=body_edge or foot_length<=thickness:
            name=ROW_LENGTH_PARAMETERS[index]
            raise ValueError(f'{name},overall_width,body_width:脚底长度必须小于该侧外伸范围且大于金属厚度')
        bend_center=foot_inner+thickness/2.0
    else:
        bend_center = max(body_edge + thickness, foot_inner)
    points = [
        (body_edge, exit_z + thickness / 2.0),
        (bend_center + thickness / 2.0, exit_z + thickness / 2.0),
        (bend_center + thickness / 2.0, thickness),
        (outer_edge, thickness),
        (outer_edge, 0.0),
        (bend_center - thickness / 2.0, 0.0),
        (bend_center - thickness / 2.0, exit_z - thickness / 2.0),
        (body_edge, exit_z - thickness / 2.0),
    ]
    return [(-y, z) for y, z in reversed(points)] if side < 0 else points


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """构建对称偶数阵列，或已明确每排数量和跨度的独立阵列。"""
    rows = row_positions(parameters)
    widths = row_widths(parameters)
    validate_row_envelopes(parameters)
    thicknesses = row_profile_values(parameters,ROW_THICKNESS_PARAMETERS,"lead_thickness")
    parts: list[Any] = []
    for (side, x_positions), lead_width,thickness in zip(rows,widths,thicknesses):
        profile = _profile(parameters, side)
        for x in x_positions:
            lead = primitives.extrude_yz_profile(
                profile, depth=lead_width, translation=(x, 0.0, 0.0)
            )
            parts.append(primitives.try_fillet(lead, "|X", min(0.04, thickness * 0.25)))
    return parts
