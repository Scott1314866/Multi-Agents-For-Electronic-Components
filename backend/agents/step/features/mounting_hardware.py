"""D-SUB 安装套筒与顶端紧固柱特征。"""

from __future__ import annotations

from typing import Any


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """构建两侧安装套筒和顶端紧固柱。"""
    span = float(parameters["center_span"])
    hole_radius = float(parameters["hole_diameter"]) / 2.0
    outer_radius = float(parameters["outer_diameter"]) / 2.0
    if outer_radius <= hole_radius:
        raise ValueError("安装结构外径必须大于安装孔径")
    front_y = float(parameters["front_y"])
    rear_y = float(parameters["rear_y"])
    center_z = float(parameters["center_z"])
    top_z = float(parameters["top_z"])
    parts: list[Any] = []
    for x in (-span / 2.0, span / 2.0):
        barrel = primitives.cylinder_y(
            x=x, y_start=front_y, z=center_z,
            radius=outer_radius, depth=rear_y - front_y,
        )
        bore = primitives.cylinder_y(
            x=x, y_start=front_y - 0.1, z=center_z,
            radius=hole_radius, depth=rear_y - front_y + outer_radius,
        )
        parts.append(primitives.cut(barrel, bore))
        post_height = top_z - (center_z + outer_radius)
        if post_height <= 0:
            raise ValueError("安装柱顶高必须高于安装套筒")
        parts.append(primitives.cylinder_z(
            x=x,
            y=rear_y - outer_radius,
            z_start=top_z - post_height,
            radius=outer_radius,
            height=post_height,
        ))
    return parts
