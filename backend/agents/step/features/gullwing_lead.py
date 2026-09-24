"""鸥翼引脚及两侧阵列特征。"""

from __future__ import annotations

from typing import Any


def _profile(parameters: dict[str, Any], side: int) -> list[tuple[float, float]]:
    """生成一个鸥翼引脚在 YZ 平面上的闭合截面。"""
    thickness = float(parameters["lead_thickness"])
    body_edge = float(parameters["body_width"]) / 2.0 - min(0.03, thickness * 0.2)
    outer_edge = float(parameters["overall_width"]) / 2.0
    foot_inner = outer_edge - float(parameters["foot_length"])
    exit_z = float(parameters["body_standoff"]) + float(parameters["body_height"]) * 0.58
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
    """按 Feature IR 的数量和间距构建两侧对称鸥翼引脚阵列。"""
    count = int(parameters["count"])
    if count < 2 or count % 2:
        raise ValueError("鸥翼引脚数量必须是大于等于 2 的偶数")
    per_side = count // 2
    pitch = float(parameters["pitch"])
    expected_span = max(0, per_side - 1) * pitch
    row_span = float(parameters["row_span"])
    if abs(row_span - expected_span) > 0.05:
        raise ValueError("鸥翼引脚阵列跨距与引脚数量、间距不一致")
    lead_width = float(parameters["lead_width"])
    thickness = float(parameters["lead_thickness"])
    x_positions = [
        (index - (per_side - 1) / 2.0) * pitch for index in range(per_side)
    ]
    parts: list[Any] = []
    for side in (-1, 1):
        profile = _profile(parameters, side)
        for x in x_positions:
            lead = primitives.extrude_yz_profile(
                profile, depth=lead_width, translation=(x, 0.0, 0.0)
            )
            parts.append(primitives.try_fillet(lead, "|X", min(0.04, thickness * 0.25)))
    return parts
