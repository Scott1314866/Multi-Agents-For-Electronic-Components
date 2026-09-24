"""带拔模角的塑封本体特征。"""

from __future__ import annotations

import math
from typing import Any


def build(primitives: Any, parameters: dict[str, Any]) -> list[Any]:
    """把塑封本体高级特征展开为两个正交拔模角控制的矩形 Loft。"""
    length = float(parameters["length"])
    width = float(parameters["width"])
    height = float(parameters["height"])
    # 工程图在两个正投影视图中分别标注 α、β；两者控制长度、宽度方向
    # 的侧壁收缩，禁止丢弃任一有证据参数。
    length_draft = math.radians(float(parameters["draft_top_deg"]))
    width_draft = math.radians(float(parameters["draft_bottom_deg"]))
    length_shrink = max(0.0, height * math.tan(length_draft))
    width_shrink = max(0.0, height * math.tan(width_draft))
    return [primitives.rectangle_loft(
        bottom_length=length,
        bottom_width=width,
        top_length=max(length - 2.0 * length_shrink, length * 0.7),
        top_width=max(width - 2.0 * width_shrink, width * 0.7),
        z_min=float(parameters["standoff"]),
        height=height,
    )]
