"""D-SUB 交错触点阵列与 90° 弯针特征。"""

from __future__ import annotations

from typing import Any


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """根据触点数量和节距构建交错母端接触阵列及 90° 弯针。"""
    count = int(round(float(parameters["count"])))
    if count < 2:
        raise ValueError("D-SUB 触点数量必须大于 1")
    pitch = float(parameters["pitch"])
    column_span = float(parameters["column_span"])
    row_spacing = float(parameters["row_spacing"])
    size = float(parameters["pin_width"])
    outer_diameter = float(parameters["contact_outer_diameter"])
    inner_diameter = float(parameters["contact_inner_diameter"])
    if not 0.0 < inner_diameter < outer_diameter:
        raise ValueError("母端接触孔直径必须小于外径")
    front_y = float(parameters["front_y"])
    bend_y = float(parameters["bend_y"])
    center_z = float(parameters["shell_center_z"])
    top_count = (count + 1) // 2
    bottom_count = count // 2
    expected_span = (top_count - 1) * pitch
    if abs(expected_span - column_span) > max(0.05, pitch * 0.03):
        raise ValueError("触点列跨度与触点数量、节距不一致")

    def centered_positions(item_count: int) -> list[float]:
        return [(index - (item_count - 1) / 2.0) * pitch for index in range(item_count)]

    layouts = [
        (centered_positions(top_count), center_z + row_spacing / 2.0),
        (centered_positions(bottom_count), center_z - row_spacing / 2.0),
    ]
    parts: list[Any] = []
    for xs, contact_z in layouts:
        for x in xs:
            outer = primitives.cylinder_y(
                x=x,
                y_start=front_y,
                z=contact_z,
                radius=outer_diameter / 2.0,
                depth=bend_y - front_y,
            )
            inner = primitives.cylinder_y(
                x=x,
                y_start=front_y - outer_diameter,
                z=contact_z,
                radius=inner_diameter / 2.0,
                depth=bend_y - front_y + 2.0 * outer_diameter,
            )
            female_contact = primitives.cut(outer, inner)
            horizontal = primitives.box(
                size,
                bend_y - front_y,
                size,
                center=(x, (front_y + bend_y) / 2.0, contact_z),
                centered_z=True,
            )
            vertical = primitives.box(
                size,
                size,
                contact_z + size / 2.0,
                center=(x, bend_y, 0.0),
            )
            lead = primitives.union(horizontal, vertical)
            parts.append(primitives.union(female_contact, lead))
    return parts
