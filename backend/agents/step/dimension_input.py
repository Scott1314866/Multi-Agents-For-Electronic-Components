"""Parse and validate operator supplied package dimensions.

These values are explicitly human evidence. They never create OCR tokens or
pretend to have image coordinates.
"""

from __future__ import annotations

import re
from typing import Any


FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "nominal_pin_count": ("引脚数", "总引脚数", "pin count", "number of pins", "n-lead"),
    "terminal_pitch": ("引脚间距", "脚距", "pitch"),
    "pin_span": ("引脚跨距", "脚跨距", "lead span", "pin span"),
    "total_height": ("总高度", "整体高度", "overall height", "total height"),
    "housing_height": ("本体高度", "塑封本体高度", "package body height", "housing height"),
    "body_standoff": ("离板高度", "本体离板高度", "standoff"),
    "overall_width": ("总宽", "总体宽度", "整体宽度", "overall width"),
    "body_width": ("本体宽度", "塑封宽度", "body width"),
    "body_length": ("本体长度", "塑封长度", "body length"),
    "terminal_length": ("引脚长度", "脚长", "terminal length", "lead length"),
    "terminal_thickness": ("引脚厚度", "脚厚", "terminal thickness", "lead thickness"),
    "terminal_width": ("引脚宽度", "脚宽", "terminal width", "lead width"),
    "overall_length": ("总长度", "整体长度", "overall length"),
    "body_height": ("本体高度", "body height"),
    "lead_spacing": ("引脚间距", "脚距", "lead spacing"),
    "terminal_diameter": ("引脚直径", "脚径", "lead diameter"),
}

_NUMBER = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_UNIT = r"(mm|毫米|cm|厘米|um|μm|微米|mil|mils|inch|inches|in|英寸|deg|degree|degrees|度|pins?|个)?"


def expected_unit(field: str) -> str:
    if field.endswith("_count"):
        return "count"
    if field.endswith("_deg") or field.endswith("_angle"):
        return "deg"
    return "mm"


def _normalize_value(field: str, value: Any, unit: Any = None) -> dict[str, Any]:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数值") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{field} 不能是非有限数值")

    target = expected_unit(field)
    normalized_unit = str(unit or target).strip().casefold()
    if target == "count":
        if normalized_unit not in {"count", "pin", "pins", "个", "引脚", ""}:
            raise ValueError(f"{field} 单位应为引脚数")
        if number < 1 or number > 1000 or not number.is_integer():
            raise ValueError(f"{field} 必须是 1 到 1000 之间的整数")
        return {"value": int(number), "unit": "count"}
    if target == "deg":
        if normalized_unit not in {"deg", "degree", "degrees", "度", "°", ""}:
            raise ValueError(f"{field} 单位应为 deg")
        if not 0 <= number < 90:
            raise ValueError(f"{field} 必须在 0 到 90 度之间")
        return {"value": number, "unit": "deg"}

    factors = {
        "mm": 1.0, "毫米": 1.0,
        "cm": 10.0, "厘米": 10.0,
        "um": 0.001, "μm": 0.001, "微米": 0.001,
        "mil": 0.0254, "mils": 0.0254,
        "in": 25.4, "inch": 25.4, "inches": 25.4, "英寸": 25.4,
        "": 1.0,
    }
    if normalized_unit not in factors:
        raise ValueError(f"{field} 不支持单位 {unit!r}，请使用 mm、cm、um、mil 或 inch")
    number *= factors[normalized_unit]
    if not 0 < number <= 1000:
        raise ValueError(f"{field} 换算为 mm 后必须大于 0 且不超过 1000")
    return {"value": number, "unit": "mm"}


def parse_dimension_answer(request: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Intent + slots: accept structured slots or labelled natural language."""
    fields = set(request.get("fields", {}))
    if not fields:
        raise ValueError("当前问题没有可填写的参数")
    action = str(answer.get("action") or "provide").strip().casefold()
    if action not in {"provide", "cancel"}:
        raise ValueError("参数补充只接受 provide 或 cancel")
    if action == "cancel":
        return {"action": "cancel", "values": {}, "comment": str(answer.get("comment") or "")[:2000]}

    raw_slots = answer.get("values", answer.get("slots", {}))
    if raw_slots is None:
        raw_slots = {}
    if not isinstance(raw_slots, dict):
        raise ValueError("values/slots 必须是 JSON 对象")
    values: dict[str, dict[str, Any]] = {}
    unknown = set(raw_slots) - fields
    if unknown:
        raise ValueError(f"本轮未询问这些参数：{', '.join(sorted(unknown))}")
    for field, raw in raw_slots.items():
        if isinstance(raw, dict):
            values[field] = _normalize_value(field, raw.get("value"), raw.get("unit"))
        else:
            values[field] = _normalize_value(field, raw)

    text = answer.get("text")
    if text is not None:
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("text 必须是 4000 字以内的字符串")
        aliases = [
            (alias, field)
            for field in fields
            for alias in (field, *FIELD_LABELS.get(field, ()))
        ]
        aliases.sort(key=lambda item: len(item[0]), reverse=True)
        for alias, field in aliases:
            if field in values:
                continue
            prefix = r"(?<![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]", alias) else ""
            match = re.search(
                rf"{prefix}{re.escape(alias)}\s*(?:(?:[:=：])|(?:设定为|设为|等于|为|是|约))?\s*{_NUMBER}\s*{_UNIT}",
                text,
                flags=re.IGNORECASE,
            )
            if match:
                owners = {target for candidate, target in aliases if candidate.casefold() == alias.casefold()}
                if len(owners) > 1:
                    raise ValueError(
                        f"{alias} 对应多个参数槽位，请使用规范字段名：{', '.join(sorted(owners))}"
                    )
                values[field] = _normalize_value(field, match.group(1), match.group(2))

    if not values:
        labels = "、".join(sorted(fields))
        raise ValueError(f"没有识别到参数槽位。请使用 参数名=数值 的格式；本轮字段：{labels}")
    return {
        "action": "provide",
        "values": values,
        "comment": str(answer.get("comment") or "")[:2000],
    }


def parse_package_pin_count(package_type: str) -> int | None:
    """Read a pin count only from an explicit operator supplied package name."""
    value = package_type.strip()
    # Accept known industry package labels and explicit analog package codes;
    # never infer a pin count from arbitrary product IDs such as AD7792-16.
    match = re.search(
        r"(?:TSSOP|SSOP|SOIC|VSSOP|MSOP|SO|LQFP|TQFP|QFP|QFN|DIP|RU|PDSO)[-_ ]?(\d{1,3})$",
        value,
        re.I,
    )
    if not match:
        return None
    count = int(match.group(1))
    return count if 1 <= count <= 1000 else None
