"""③ 双通道交叉校验（docs/specs/Module1-Spec.md §8.2②）。

以**引脚号为主键** join 文本通道与视觉通道：
  - 两边都有 → 高置信，采纳，side 取视觉通道
  - 只有一边有 → 记 conflict，不猜

引脚名/类型/描述只由文本通道提供（视觉模型不读密集小字表格），
它们的独立复核在 ④ review.py 里做。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .extract import FigureInfo
from .models import Conflict, Evidence, Pin, Side


@dataclass
class MergeResult:
    pins: list[Pin] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.resolved is None]


def merge_channels(
    text_pins: list[Pin],
    figure: FigureInfo | None,
    *,
    page: int,
) -> MergeResult:
    """把文本通道的引脚列表与视觉通道的图形结构合并。"""
    result = MergeResult()
    if not text_pins:
        result.notes.append("文本通道没有解析出任何引脚")
        return result

    by_number: dict[str, Pin] = {}
    for pin in text_pins:
        if pin.pin_number in by_number:
            result.conflicts.append(
                Conflict(
                    pin_number=pin.pin_number,
                    field="pin_number",
                    values={"重复行": pin.name},
                    pages=[page],
                )
            )
            continue
        by_number[pin.pin_number] = pin

    if figure is None:
        result.notes.append("视觉通道没有可用引脚图，side 全部留空")
        result.pins = list(by_number.values())
        return result

    visual_numbers = {
        number
        for numbers in figure.pin_sides.values()
        for number in numbers
    }

    for number, pin in by_number.items():
        if number in visual_numbers:
            pin.side = figure.side_of(number)
            pin.side_order = figure.order_of(number)
            pin.evidence.append(
                Evidence(page=page, source="vision", agreement=True)
            )
        else:
            pin.evidence.append(
                Evidence(page=page, source="vision", agreement=False)
            )
            result.conflicts.append(
                Conflict(
                    pin_number=number,
                    field="side",
                    values={"text": pin.name, "vision": "引脚图中未出现"},
                    pages=[page],
                )
            )

    for number in sorted(visual_numbers - set(by_number)):
        result.conflicts.append(
            Conflict(
                pin_number=number,
                field="pin_number",
                values={"text": "引脚表中未出现", "vision": figure.side_of(number)},
                pages=[page],
            )
        )

    result.pins = sorted(by_number.values(), key=_pin_sort_key)
    return result


def _pin_sort_key(pin: Pin) -> tuple[int, str]:
    """数字引脚号按数值排，带字母的排后面，保持稳定。"""
    try:
        return (0, f"{int(pin.pin_number):08d}")
    except ValueError:
        return (1, pin.pin_number)


def apply_resolution(result: MergeResult, pin_number: str, field_name: str, value: str) -> None:
    """人工裁决：把某个冲突的值写回引脚，并标记冲突已解决。"""
    for conflict in result.conflicts:
        if conflict.pin_number == pin_number and conflict.field == field_name:
            conflict.resolved = value
            break

    if field_name == "side":
        for pin in result.pins:
            if pin.pin_number == pin_number:
                pin.side = value  # type: ignore[assignment]
                break


def describe(result: MergeResult) -> str:
    lines = [f"引脚 {len(result.pins)} 个，冲突 {len(result.conflicts)} 项"]
    for note in result.notes:
        lines.append(f"  · {note}")
    for conflict in result.conflicts:
        mark = "已裁决" if conflict.resolved is not None else "待裁决"
        lines.append(
            f"  [{mark}] 引脚 {conflict.pin_number} 的 {conflict.field}：{conflict.values}"
        )
    return "\n".join(lines)


__all__ = ["MergeResult", "merge_channels", "apply_resolution", "describe", "Side"]
