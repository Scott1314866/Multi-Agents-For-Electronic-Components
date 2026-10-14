"""Deterministic review decisions; substantive disagreements stay unresolved."""
from __future__ import annotations

import re
from typing import Any

from .models import Pin
from .pkgname import pin_count_from_package
from backend.agents.symbol.tools.review import ocr_confusable


def documented_exposed_pad(name: str, description: str) -> bool:
    return bool(re.fullmatch(r"EP(?:AD)?|EXPOSED\s*PAD", name, re.I)
                and re.search(r"exposed\s*pad|裸露焊盘|散热焊盘", description, re.I))


def verified_exposed_pad(pins: list[Pin], package: str) -> Pin | None:
    """Only accept N peripherals plus a separately documented numbered pad."""
    count = pin_count_from_package(package)
    pads = [p for p in pins if documented_exposed_pad(p.name, p.description)]
    if count is None or len(pads) != 1 or len(pins) != count + 1:
        return None
    pad = pads[0]
    peripheral = [p for p in pins if p is not pad]
    if pad.pin_number == str(count + 1) and {p.pin_number for p in peripheral} == {
        str(n) for n in range(1, count + 1)
    }:
        return pad
    return None


def _norm(name: str) -> str:
    # Preserve polarity marks and separators: RESET and /RESET are different.
    return re.sub(r"\s+", "", name).upper()


def automatic_name_resolution(
    pin: dict[str, Any], expected: str, observed: str, *,
    observation: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Return a verified value and reason, never a guess based on confidence alone."""
    current = str(pin.get("name") or "")
    if not current or not observed:
        return None
    if _norm(current) == _norm(observed):
        return current, "当前引脚名称已与复核一致"
    if _norm(current) != _norm(expected):
        return None  # Stale review must not overwrite another correction.

    confidence = (observation or {}).get("confidence")
    if confidence is not None:
        try:
            if isinstance(confidence, bool) or not 0.9 <= float(confidence) <= 1:
                return None
        except (TypeError, ValueError):
            return None
    if (observation or {}).get("uncertain"):
        return None

    if {_norm(current), _norm(observed)} <= {"EP", "EPAD", "EXPOSEDPAD"}:
        if documented_exposed_pad(current, str(pin.get("description") or "")):
            return observed, "手册已明确标注裸露焊盘，EP/EPAD 为同一引脚名称"

    agreement = any(e.get("source") == "vision" and e.get("agreement") is True
                    for e in pin.get("evidence") or [] if isinstance(e, dict))
    if not agreement or pin.get("side") not in {"left", "right", "top", "bottom"}:
        return None

    number = str(pin.get("pin_number") or "")
    if (current == number and observed != number
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_/.+()\-]{0,63}", observed)):
        return observed, "文本名称为空占位，编号及位置已由双通道确认，采用独立复核名称"

    # INO -> IN0 is allowed only when the description independently says channel 0.
    # IN1 -> IN2, arbitrary O/0 swaps, or multi-model ambiguous descriptions never qualify.
    candidate = re.fullmatch(r"(?:AIN|IN)(\d{1,2})", _norm(observed))
    channels = {int(n) for n in re.findall(r"analog\s+input\s+channel\s+(\d+)\b",
                                          str(pin.get("description") or ""), re.I)}
    if (candidate and channels == {int(candidate[1])}
            and ocr_confusable(_norm(current), _norm(observed))):
        return observed, "编号、图中位置及手册通道描述一致，自动纠正 OCR 字形"
    return None
