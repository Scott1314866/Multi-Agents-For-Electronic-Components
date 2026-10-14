"""Bind visible electrical IDs to physical rows, independently of table order."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class PinReferenceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    side: Literal["left", "right", "top", "bottom"]
    numbers: list[str | None] = Field(description="Natural image reading order: top/bottom rows left to right; left/right rows TOP TO BOTTOM. Only printed IDs, null if unreadable")
    evidence: str


class PinReferenceReading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: Literal["top", "bottom", "unknown"] = "unknown"
    view_evidence: str = ""
    image_index: int | None = Field(default=None, ge=1)
    rows: list[PinReferenceRow] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)


_VECTORS = {"left": (-1, 0), "right": (1, 0), "top": (0, 1), "bottom": (0, -1)}


def perimeter_rows(pads: list[dict]) -> dict[str, list[int]]:
    """Group two opposite straight rows; reject unsupported/ambiguous layouts."""
    if not pads or any(p.get('x') is None or p.get('y') is None for p in pads):
        raise ValueError("缺少可核对的焊盘坐标")
    for axis, along, negative, positive in (("x", "y", "left", "right"), ("y", "x", "bottom", "top")):
        groups = defaultdict(list)
        for index, pad in enumerate(pads):
            groups[round(float(pad[axis]), 6)].append(index)
        if len(groups) == 2 and min(groups) < 0 < max(groups):
            return {negative: sorted(groups[min(groups)], key=lambda i: pads[i][along]),
                    positive: sorted(groups[max(groups)], key=lambda i: pads[i][along])}
    raise ValueError("引脚图核对需要两侧直排布局；请补充与焊盘方向一致的编号图")


def bind_pin_reference(pads: list[dict], source: PinReferenceReading) -> dict:
    """Only a unique rotation of an explicit complete TOP VIEW can bind IDs.

    No electrical IDs are derived from a package name or a contiguous sequence.
    Equal-count symmetric rows are ambiguous without further orientation proof.
    """
    if source.view != 'top' or not source.view_evidence.strip() or source.confidence < .9:
        raise ValueError("引脚编号图缺少明确的高置信度 TOP VIEW 依据")
    if not source.image_index or not source.rows or len({r.side for r in source.rows}) != len(source.rows):
        raise ValueError("引脚图来源或排布不完整")
    numbers = [n for r in source.rows for n in r.numbers]
    import re
    if len(numbers) != len(pads) or any(not n or not re.fullmatch(r'[A-Za-z0-9_]{1,32}', n) for n in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("引脚图没有完整、唯一的可见脚号，保留人工核对")
    if any(not r.evidence.strip() for r in source.rows):
        raise ValueError("引脚图缺少逐排编号证据")
    target = perimeter_rows(pads)
    matches = []
    for turns in range(4):
        mapped = {}
        for row in source.rows:
            x, y = _VECTORS[row.side]
            ax, ay = (0, 1) if row.side in {'left', 'right'} else (1, 0)
            # The image is read top-to-bottom; geometry uses Y positive upward.
            row_numbers = list(reversed(row.numbers)) if row.side in {'left', 'right'} else row.numbers
            for _ in range(turns):
                x, y, ax, ay = -y, x, -ay, ax
            side = next(k for k, v in _VECTORS.items() if v == (x, y))
            mapped[side] = row_numbers if ax + ay > 0 else list(reversed(row_numbers))
        if set(mapped) == set(target) and all(len(mapped[s]) == len(target[s]) for s in target):
            matches.append((turns, mapped))
    if len(matches) != 1:
        raise ValueError("编号图与焊盘布局的旋转方向不唯一，请补充同方向顶视图")
    turns, mapped = matches[0]
    expected = deepcopy(pads)
    for side, indices in target.items():
        for index, number in zip(indices, mapped[side]):
            expected[index]['number'] = number
    return {'pads': expected, 'source': source.model_dump(), 'rotation_deg': turns * 90,
            'evidence': source.view_evidence + '; ' + '; '.join(r.evidence for r in source.rows)}


def pin_reference_issues(pads: list[dict], reference: dict | None) -> list[str]:
    if not reference:
        return []
    try:
        actual = perimeter_rows(pads)
        expected_pads = reference['pads']
        expected = perimeter_rows(expected_pads)
        if set(actual) != set(expected) or any(len(actual[s]) != len(expected[s]) for s in actual):
            return ["焊盘布局与来源引脚图不一致，请补充对应顶视编号图后重新识别"]
        issues = []
        labels = {'left': '左侧', 'right': '右侧', 'top': '上侧', 'bottom': '下侧'}
        for side, indices in actual.items():
            for index, source_index in zip(indices, expected[side]):
                wanted = expected_pads[source_index]['number']
                if pads[index].get('number') != wanted:
                    issues.append(f"第 {index+1} 行（{labels[side]}，X={pads[index]['x']:g}，Y={pads[index]['y']:g} mm）脚号应为 {wanted}，当前为 {pads[index].get('number') or '空'}；请按来源顶视图核对，表格行号不是脚号")
        return issues
    except (ValueError, KeyError, TypeError):
        return ["无法核对来源引脚位置，请补充完整坐标和顶视编号图"]
