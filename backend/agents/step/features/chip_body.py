"""两端片式元件的本体与端电极高级特征。"""

from __future__ import annotations

from typing import Any


def build_body(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """构建位于两个端电极之间的矩形介质本体。"""
    length = float(parameters["length"])
    width = float(parameters["width"])
    height = float(parameters["height"])
    if min(length, width, height) <= 0.0:
        raise ValueError("片式元件本体尺寸必须大于零")
    return [primitives.box(
        length,
        width,
        height,
        center=(0.0, 0.0, 0.0),
        centered_z=False,
    )]


def build_end_caps(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """按总长和端部长度构建左右两个独立端电极。"""
    overall_length = float(parameters["overall_length"])
    terminal_length = float(parameters["terminal_length"])
    width = float(parameters["width"])
    height = float(parameters["height"])
    if min(overall_length, terminal_length, width, height) <= 0.0:
        raise ValueError("片式元件端电极尺寸必须大于零")
    if terminal_length * 2.0 >= overall_length:
        raise ValueError("两个端电极长度之和必须小于器件总长")
    offset = overall_length / 2.0 - terminal_length / 2.0
    return [
        primitives.box(
            terminal_length,
            width,
            height,
            center=(x, 0.0, 0.0),
            centered_z=False,
        )
        for x in (-offset, offset)
    ]
