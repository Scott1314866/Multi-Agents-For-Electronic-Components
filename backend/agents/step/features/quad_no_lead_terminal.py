"""四边无引脚封装的底部端子阵列。"""

from __future__ import annotations

from typing import Any


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """生成均匀分布在四边底面的矩形端子。"""
    count = int(parameters["count"])
    body_length = float(parameters["body_length"])
    body_width = float(parameters["body_width"])
    pitch = float(parameters["pitch"])
    length = float(parameters["terminal_length"])
    width = float(parameters["terminal_width"])
    height = float(parameters["terminal_height"])
    if count < 4 or count % 4:
        raise ValueError("四边无引脚端子数量必须是四的正整数倍")
    if min(body_length, body_width, pitch, length, width, height) <= 0.0:
        raise ValueError("四边无引脚端子尺寸必须大于零")
    if length * 2.0 >= min(body_length, body_width):
        raise ValueError("端子长度不能越过封装中心")

    per_side = count // 4
    offsets = [(index - (per_side - 1) / 2.0) * pitch for index in range(per_side)]
    y = body_width / 2.0 - length / 2.0
    x = body_length / 2.0 - length / 2.0
    terminals = []
    for offset in offsets:
        terminals.extend([
            primitives.box(width, length, height, center=(offset, y, 0.0)),
            primitives.box(width, length, height, center=(offset, -y, 0.0)),
            primitives.box(length, width, height, center=(x, offset, 0.0)),
            primitives.box(length, width, height, center=(-x, offset, 0.0)),
        ])
    pad_length = parameters.get("exposed_pad_length")
    pad_width = parameters.get("exposed_pad_width")
    if (pad_length is None) != (pad_width is None):
        raise ValueError("中心裸露焊盘长宽必须同时提供")
    if pad_length is not None:
        pad_length = float(pad_length)
        pad_width = float(pad_width)
        if min(pad_length, pad_width) <= 0.0:
            raise ValueError("中心裸露焊盘尺寸必须大于零")
        if pad_length >= body_length or pad_width >= body_width:
            raise ValueError("中心裸露焊盘必须小于封装本体")
        terminals.append(primitives.box(
            pad_length, pad_width, height, center=(0.0, 0.0, 0.0)
        ))
    return terminals
