"""CadQuery/OpenCascade 基础几何操作的唯一封装层。"""

from __future__ import annotations

from typing import Any

from backend.agents.step.cad_utils import bbox_value


class CadQueryPrimitives:
    """执行 box、loft、cut、union、extrude、fillet 和 compound。"""

    def __init__(self, cq: Any):
        self.cq = cq

    def box(
        self,
        length: float,
        width: float,
        height: float,
        *,
        center: tuple[float, float, float],
        centered_z: bool = False,
    ):
        """创建轴对齐盒体。"""
        shape = self.cq.Workplane("XY").box(
            length, width, height, centered=(True, True, centered_z)
        )
        return shape.translate(center)

    def rectangle_loft(
        self,
        *,
        bottom_length: float,
        bottom_width: float,
        top_length: float,
        top_width: float,
        z_min: float,
        height: float,
    ):
        """创建上下矩形截面的直纹实体。"""
        return (
            self.cq.Workplane("XY", origin=(0.0, 0.0, z_min))
            .rect(bottom_length, bottom_width)
            .workplane(offset=height)
            .rect(top_length, top_width)
            .loft(combine=True)
        )

    def extrude_xz_profile(
        self,
        points: list[tuple[float, float]],
        *,
        y_start: float,
        depth: float,
    ):
        """在 XZ 平面绘制闭合轮廓并沿 Y 方向拉伸。"""
        shape = (
            self.cq.Workplane("XZ", origin=(0.0, y_start, 0.0))
            .polyline(points)
            .close()
            .extrude(depth)
        )
        bbox = shape.val().BoundingBox()
        ymin = bbox_value(bbox, "ymin", "yMin")
        ymax = bbox_value(bbox, "ymax", "yMax")
        target_min = min(y_start, y_start + depth)
        if abs(ymin - target_min) > 1e-5:
            shape = shape.translate((0.0, target_min - ymin, 0.0))
        if abs((ymax - ymin) - abs(depth)) > 1e-4:
            raise RuntimeError("XZ 轮廓拉伸深度异常")
        return shape

    def extrude_yz_profile(
        self,
        points: list[tuple[float, float]],
        *,
        depth: float,
        translation: tuple[float, float, float],
    ):
        """在 YZ 平面拉伸闭合轮廓。"""
        return (
            self.cq.Workplane("YZ")
            .polyline(points)
            .close()
            .extrude(depth / 2.0, both=True)
            .translate(translation)
        )

    def cylinder_y(
        self, *, x: float, y_start: float, z: float, radius: float, depth: float
    ):
        """创建沿 Y 方向的圆柱。"""
        return (
            self.cq.Workplane("XZ", origin=(0.0, y_start, 0.0))
            .center(x, z)
            .circle(radius)
            .extrude(depth)
        )

    def cylinder_z(
        self, *, x: float, y: float, z_start: float, radius: float, height: float
    ):
        """创建沿 Z 方向的圆柱。"""
        return (
            self.cq.Workplane("XY", origin=(0.0, 0.0, z_start))
            .center(x, y)
            .circle(radius)
            .extrude(height)
        )

    @staticmethod
    def cut(base: Any, tool: Any):
        """执行布尔减。"""
        return base.cut(tool)

    @staticmethod
    def union(base: Any, addition: Any):
        """执行布尔合并。"""
        return base.union(addition)

    @staticmethod
    def try_fillet(shape: Any, selector: str, radius: float):
        """执行允许失败的外观圆角，不改变尺寸语义。"""
        try:
            return shape.edges(selector).fillet(radius)
        except Exception:
            return shape

    def compound(self, parts: list[Any]):
        """把独立零件实体组合为 STEP Compound。"""
        values = [part.val() if hasattr(part, "val") else part for part in parts]
        return self.cq.Compound.makeCompound(values)
