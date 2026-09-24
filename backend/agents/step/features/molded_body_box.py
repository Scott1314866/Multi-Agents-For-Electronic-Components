"""无拔模标注时使用的参数化塑封本体。"""

from __future__ import annotations

from typing import Any


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """按 Feature IR 的长宽高和离板高度构建矩形塑封本体。

    Args:
        primitives: 通用 CadQuery 基础几何操作。
        parameters: ``length``、``width``、``height``、``standoff``。

    Returns:
        只包含一个有效实体的列表。

    失败状态:
        任一尺寸非正或离板高度为负时抛出 ``ValueError``。
    """
    length = float(parameters["length"])
    width = float(parameters["width"])
    height = float(parameters["height"])
    standoff = float(parameters["standoff"])
    if min(length, width, height) <= 0.0 or standoff < 0.0:
        raise ValueError("塑封本体尺寸无效")
    return [primitives.box(
        length,
        width,
        height,
        center=(0.0, 0.0, standoff),
        centered_z=False,
    )]
