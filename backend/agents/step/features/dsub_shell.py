"""D-SUB 前法兰、梯形金属壳、绝缘体和后壳特征。"""

from __future__ import annotations

from typing import Any


def _trapezoid(
    top_width: float, bottom_width: float, height: float, center_z: float
) -> list[tuple[float, float]]:
    """根据图纸给出的上下宽度生成 D-SUB 壳体截面。"""
    top_half_w = top_width / 2.0
    bottom_half_w = bottom_width / 2.0
    half_h = height / 2.0
    return [
        (-top_half_w, center_z + half_h),
        (top_half_w, center_z + half_h),
        (bottom_half_w, center_z - half_h),
        (-bottom_half_w, center_z - half_h),
    ]


def build_shell_frame(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """构建前法兰、梯形金属壳和前端绝缘体。"""
    plate_depth = float(parameters["plate_thickness"])
    plate_y = float(parameters["plate_y"])
    plate = primitives.box(
        float(parameters["overall_width"]),
        plate_depth,
        float(parameters["plate_height"]),
        center=(0.0, plate_y, float(parameters["plate_bottom_z"])),
    )
    hole_radius = float(parameters["mounting_hole_diameter"]) / 2.0
    for x in (
        -float(parameters["mounting_center_span"]) / 2.0,
        float(parameters["mounting_center_span"]) / 2.0,
    ):
        cutter = primitives.cylinder_y(
            x=x,
            y_start=plate_y - plate_depth,
            z=float(parameters["shell_center_z"]),
            radius=hole_radius,
            depth=plate_depth + 1.0,
        )
        plate = primitives.cut(plate, cutter)

    shell_top_width = float(parameters["shell_top_width"])
    shell_bottom_width = float(parameters["shell_bottom_width"])
    shell_height = float(parameters["shell_height"])
    center_z = float(parameters["shell_center_z"])
    wall = float(parameters["shell_wall"])
    shell_depth = float(parameters["shell_depth"])
    front_y = float(parameters["front_y"])
    outer = primitives.extrude_xz_profile(
        _trapezoid(shell_top_width, shell_bottom_width, shell_height, center_z),
        y_start=front_y,
        depth=shell_depth,
    )
    inner = primitives.extrude_xz_profile(
        _trapezoid(
            shell_top_width - 2.0 * wall,
            shell_bottom_width - 2.0 * wall,
            shell_height - 2.0 * wall,
            center_z,
        ),
        y_start=front_y - 0.1,
        depth=shell_depth + 0.2,
    )
    shell = primitives.cut(outer, inner)
    insert = primitives.extrude_xz_profile(
        _trapezoid(
            shell_top_width - 2.0 * wall,
            shell_bottom_width - 2.0 * wall,
            shell_height - 2.0 * wall,
            center_z,
        ),
        y_start=front_y + wall,
        depth=shell_depth - 2.0 * wall,
    )
    return [plate, shell, insert]


def build_rear_housing(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """构建受总深度约束的后部绝缘壳体。"""
    y_min = float(parameters["y_min"])
    y_max = float(parameters["y_max"])
    return [primitives.box(
        float(parameters["width"]),
        y_max - y_min,
        float(parameters["height"]),
        center=(0.0, (y_min + y_max) / 2.0, float(parameters["z_min"])),
    )]
