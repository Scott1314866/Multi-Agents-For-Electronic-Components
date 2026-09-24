"""STEP 工作流共享的 CAD 运行时工具。

这些工具原本位于 ``nodes.py``（参数流程），现由二维图纸流程和图片证据链流程
共同使用：延迟导入 CadQuery、安全文件名、包围盒兼容读取、STEP 回读指标和
Pillow 预览渲染。
"""

from __future__ import annotations

import math
import re
import shutil
from pathlib import Path
from statistics import mean
from typing import Any


def load_cadquery():
    """延迟导入 CadQuery，避免模块导入阶段阻断整个后端。

    Returns:
        已导入的 ``cadquery`` 模块。

    Raises:
        RuntimeError: 当前 Python 环境没有安装可用的 CadQuery。
    """
    try:
        import cadquery as cq  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "缺少 CadQuery 运行依赖；请在当前环境安装 cadquery==2.8.0 和 pyparsing>=3.1。"
        ) from exc
    return cq


def safe_file_stem(value: str) -> str:
    """把料号或用户文件名转换成安全的本地文件主名。

    Args:
        value: 原始文件名或料号。

    Returns:
        不含路径分隔符和危险字符的文件主名。
    """
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return stem or "step_model"


def bbox_value(bbox: Any, lower_name: str, camel_name: str) -> float:
    """兼容不同 CadQuery 版本的包围盒属性命名。

    Args:
        bbox: CadQuery/OpenCascade 包围盒对象。
        lower_name: 新版小写属性名，例如 ``xmin``。
        camel_name: 旧版驼峰属性名，例如 ``xMin``。

    Returns:
        包围盒属性的浮点值。
    """
    if hasattr(bbox, lower_name):
        return float(getattr(bbox, lower_name))
    return float(getattr(bbox, camel_name))


def read_step_metrics(cq: Any, step_path: Path) -> dict[str, Any]:
    """使用 OpenCascade 回读 STEP 并计算实体级指标。

    Args:
        cq: 已导入的 CadQuery 模块。
        step_path: 待验证 STEP 文件路径。

    Returns:
        实体数、体积、包围盒和可继续用于渲染的 Shape。
    """
    imported = cq.importers.importStep(str(step_path))
    shape = imported.val()
    bbox = shape.BoundingBox()
    return {
        "shape": shape,
        "is_valid": bool(shape.isValid()),
        "solid_count": len(shape.Solids()),
        "face_count": len(shape.Faces()),
        "edge_count": len(shape.Edges()),
        "volume_mm3": float(shape.Volume()),
        "bounding_box": {
            "xmin": bbox_value(bbox, "xmin", "xMin"),
            "xmax": bbox_value(bbox, "xmax", "xMax"),
            "ymin": bbox_value(bbox, "ymin", "yMin"),
            "ymax": bbox_value(bbox, "ymax", "yMax"),
            "zmin": bbox_value(bbox, "zmin", "zMin"),
            "zmax": bbox_value(bbox, "zmax", "zMax"),
        },
    }


def prepare_reference_step(
    cq: Any,
    source_path: Path,
    output_path: Path,
    expected_bbox: dict[str, float],
    *,
    adapt_dimensions: bool,
) -> None:
    """复制相同产品 STEP，或受限缩放相似封装 STEP。

    Args:
        cq: 已导入的 CadQuery 模块。
        source_path: 已下载并通过 STEP 文件头检查的候选文件。
        output_path: 标准候选 STEP 输出路径。
        expected_bbox: Feature IR 根据图纸证据给出的目标包围盒。
        adapt_dimensions: ``False`` 直接复制；``True`` 按三个轴的包围盒比例
            执行仿射变换。

    Returns:
        无返回值，成功时写入 ``output_path``。

    Raises:
        ValueError: 原文件尺寸无效或相似文件所需缩放超出安全范围。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not adapt_dimensions:
        shutil.copy2(source_path, output_path)
        return

    imported = cq.importers.importStep(str(source_path))
    shape = imported.val()
    bbox = shape.BoundingBox()
    source_min = (
        bbox_value(bbox, "xmin", "xMin"),
        bbox_value(bbox, "ymin", "yMin"),
        bbox_value(bbox, "zmin", "zMin"),
    )
    source_max = (
        bbox_value(bbox, "xmax", "xMax"),
        bbox_value(bbox, "ymax", "yMax"),
        bbox_value(bbox, "zmax", "zMax"),
    )
    target_min = tuple(float(expected_bbox[f"{axis}min"]) for axis in "xyz")
    target_max = tuple(float(expected_bbox[f"{axis}max"]) for axis in "xyz")
    source_size = tuple(high - low for low, high in zip(source_min, source_max))
    target_size = tuple(high - low for low, high in zip(target_min, target_max))
    if min(source_size) <= 1e-9 or min(target_size) <= 1e-9:
        raise ValueError("参考 STEP 或目标包围盒尺寸无效")
    scales = tuple(target / source for target, source in zip(target_size, source_size))
    if any(scale < 0.80 or scale > 1.25 for scale in scales):
        raise ValueError(f"相似 STEP 缩放比例超出 0.80~1.25：{scales}")

    from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
    from OCP.gp import gp_GTrsf

    transform = gp_GTrsf()
    for row, scale in enumerate(scales, start=1):
        for column in range(1, 5):
            transform.SetValue(row, column, 0.0)
        transform.SetValue(row, row, scale)
        transform.SetValue(
            row,
            4,
            target_min[row - 1] - scale * source_min[row - 1],
        )
    adapted = BRepBuilderAPI_GTransform(shape.wrapped, transform, True).Shape()
    cq.exporters.export(cq.Shape.cast(adapted), str(output_path), exportType="STEP")


def render_shape_to_png(
    shape: Any,
    output_path: Path,
    size: tuple[int, int] = (1200, 800),
    view: str = "isometric",
) -> None:
    """使用 Pillow 将 OCC 三角网格渲染成指定方向的预览图。

    该实现不依赖 matplotlib，可避开 NumPy ABI 冲突。

    Args:
        shape: CadQuery Shape 对象。
        output_path: PNG 输出路径。
        size: 输出图像宽高。
        view: ``isometric``、``front``、``top`` 或 ``right``。
    """
    from PIL import Image, ImageDraw

    vertices, triangles = shape.tessellate(0.08)
    points_3d = [(float(v.x), float(v.y), float(v.z)) for v in vertices]
    projection = {
        "isometric": lambda x, y, z: (
            (x - y) * 0.8660254,
            (x + y) * 0.5 - z,
            (x + y) * 0.5 + z,
        ),
        "front": lambda x, y, z: (x, -z, y),
        "top": lambda x, y, z: (x, -y, z),
        "right": lambda x, y, z: (y, -z, x),
    }.get(view)
    if projection is None:
        raise ValueError(f"不支持的预览方向：{view}")
    projected = [projection(x, y, z) for x, y, z in points_3d]
    min_u = min(point[0] for point in projected)
    max_u = max(point[0] for point in projected)
    min_v = min(point[1] for point in projected)
    max_v = max(point[1] for point in projected)
    width, height = size
    margin = 64
    scale = min(
        (width - margin * 2) / max(max_u - min_u, 1e-9),
        (height - margin * 2) / max(max_v - min_v, 1e-9),
    )
    drawing_width = (max_u - min_u) * scale
    drawing_height = (max_v - min_v) * scale
    offset_x = (width - drawing_width) / 2.0
    offset_y = (height - drawing_height) / 2.0

    def screen(index: int) -> tuple[float, float]:
        u, v, _ = projected[index]
        return (
            offset_x + (u - min_u) * scale,
            offset_y + (v - min_v) * scale,
        )

    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    ordered = sorted(
        triangles,
        key=lambda tri: mean(projected[index][2] for index in tri),
    )
    for triangle in ordered:
        i1, i2, i3 = triangle
        p1, p2, p3 = (points_3d[i1], points_3d[i2], points_3d[i3])
        edge1 = tuple(p2[i] - p1[i] for i in range(3))
        edge2 = tuple(p3[i] - p1[i] for i in range(3))
        normal = (
            edge1[1] * edge2[2] - edge1[2] * edge2[1],
            edge1[2] * edge2[0] - edge1[0] * edge2[2],
            edge1[0] * edge2[1] - edge1[1] * edge2[0],
        )
        length = math.sqrt(sum(component * component for component in normal)) or 1.0
        light = max(0.2, min(1.0, 0.45 + 0.55 * abs(normal[2]) / length))
        fill = (int(90 * light), int(155 * light), int(220 * light))
        draw.polygon([screen(i1), screen(i2), screen(i3)], fill=fill, outline=(55, 85, 120))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
