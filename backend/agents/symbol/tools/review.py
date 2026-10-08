"""④ 独立复核（docs/specs/Module1-Spec.md §8.2④）。

用一次**不带前序上下文**的独立模型调用，重新从页面图读一遍引脚，
再和 ③ 的合并结果比对。目的是打掉自我确认偏差：复核者不知道前面
是怎么提取的，只看到「整页图 + 一句任务说明」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.agents.symbol.contracts.models import Pin
from .vision import VisionClient

_REVIEW_PROMPT = """这是一份芯片 datasheet 的第 {page} 页整页渲染图。

请独立地读出**目标型号 {device}** 的全部引脚，包括引脚号、引脚名、以及引脚在封装图上的位置。

只输出 JSON：
{{
  "pins": [
    {{"pin_number": "1", "name": "ADDR", "side": "left"}}
  ],
  "notes": "读不清或不确定的地方"
}}

规则：
- side 只能是 left / right / top / bottom / unknown。
- 引脚名照抄图上的印刷，不要凭常识纠正，也不要补全。
- 只列你在这张图上真正看到的引脚。
"""


@dataclass
class ReviewMismatch:
    pin_number: str
    field: str
    expected: str
    observed: str

    def __str__(self) -> str:
        return f"引脚 {self.pin_number} 的 {self.field}：提取值 {self.expected!r}，复核值 {self.observed!r}"


@dataclass
class ReviewResult:
    mismatches: list[ReviewMismatch] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)     # 复核没看到
    extra: list[str] = field(default_factory=list)       # 复核多出来
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not (self.mismatches or self.missing or self.extra)


def _norm_name(text: str) -> str:
    """比对用归一化：大小写、空白、连字符差异不算差异。"""
    return re.sub(r"[\s\-_/]+", "", text.strip().lower())


# OCR 高频混淆字形。只用于「提示 + 给默认值」，绝不自动改写。
_CONFUSABLE = [
    set("O0"), set("I1l|"), set("S5"), set("B8"), set("Z2"), set("G6"), set("D0"),
]


def ocr_confusable(a: str, b: str) -> bool:
    """两个字符串长度相同、且差异全是字形易混字符时返回 True。"""
    if len(a) != len(b) or a == b:
        return False
    for left, right in zip(a, b):
        if left == right:
            continue
        if not any(left in group and right in group for group in _CONFUSABLE):
            return False
    return True


def review_pins(
    client: VisionClient,
    image_path: str,
    *,
    device: str,
    pins: list[Pin],
    page: int,
) -> ReviewResult:
    payload = client.ask_json(
        [image_path],
        _REVIEW_PROMPT.format(page=page, device=device or "（未指定）"),
    )
    observed = {
        str(item.get("pin_number", "")).strip(): item
        for item in (payload.get("pins") or [])
        if str(item.get("pin_number", "")).strip()
    }

    result = ReviewResult(notes=str(payload.get("notes") or ""), raw=payload)
    expected_numbers = {pin.pin_number for pin in pins}

    result.missing = sorted(expected_numbers - set(observed))
    result.extra = sorted(set(observed) - expected_numbers)

    for pin in pins:
        item = observed.get(pin.pin_number)
        if item is None:
            continue

        seen_name = str(item.get("name") or "").strip()
        if seen_name and _norm_name(seen_name) != _norm_name(pin.name):
            result.mismatches.append(
                ReviewMismatch(pin.pin_number, "name", pin.name, seen_name)
            )

        seen_side = str(item.get("side") or "").strip().lower()
        if seen_side and seen_side != "unknown" and pin.side != "unknown":
            if seen_side != pin.side:
                result.mismatches.append(
                    ReviewMismatch(pin.pin_number, "side", pin.side, seen_side)
                )
    return result


def describe(result: ReviewResult) -> str:
    if result.passed:
        return "独立复核通过：引脚号、名称、侧别全部一致"
    lines = ["独立复核发现差异："]
    for mismatch in result.mismatches:
        lines.append(f"  · {mismatch}")
    if result.missing:
        lines.append(f"  · 复核没看到这些引脚：{'、'.join(result.missing)}")
    if result.extra:
        lines.append(f"  · 复核多出这些引脚：{'、'.join(result.extra)}")
    if result.notes:
        lines.append(f"  · 复核备注：{result.notes}")
    return "\n".join(lines)
