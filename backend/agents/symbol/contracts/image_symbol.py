"""Validate a single pin diagram and preserve its geometry in a Capture symbol."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
import warnings

from PIL import Image, ImageOps

from .extract import _valid_pin_number
from .layout import MIN_BODY_WIDTH, PIN_LEN, PITCH, PlacedPin, SymbolLayout
from .models import Evidence, Pin

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "BMP": ".bmp"}


def normalize_image(content: bytes) -> tuple[bytes, dict]:
    """Decode before accepting input; normalize orientation and alpha for vision."""
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 32 MB")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as source:
                source_format = source.format
                if source_format not in FORMATS:
                    raise ValueError("只支持 PNG、JPG、WEBP、BMP 图片")
                if getattr(source, "n_frames", 1) != 1:
                    raise ValueError("请上传单张静态封装图")
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise ValueError("图片分辨率过大，最多 3200 万像素")
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGBA")
                background = Image.new("RGBA", image.size, "white")
                background.alpha_composite(image)
                output = BytesIO()
                background.convert("RGB").save(output, format="PNG")
                return output.getvalue(), {"width": image.width, "height": image.height,
                                          "format": source_format, "source_suffix": FORMATS[source_format]}
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("无法读取图片，请上传有效的封装图") from exc


def _coordinate(value) -> float:
    if isinstance(value, bool):
        raise ValueError("图片坐标无效")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("图片坐标无效") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("图片坐标必须在 0 到 1 之间")
    return result


@dataclass
class ImageReading:
    device: str
    package: str
    pins: list[Pin]
    geometry: dict
    questions: list[dict]
    pin_count: int | None


def read_image_payload(payload: dict, *, image_size: dict) -> ImageReading:
    """Reject invented/duplicate IDs; uncertain text becomes a targeted question."""
    bbox = payload.get("body_bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("未能确定符号主体边界，请上传更清晰的封装图")
    x1, y1, x2, y2 = map(_coordinate, bbox)
    if x2 - x1 < 0.01 or y2 - y1 < 0.01:
        raise ValueError("符号主体边界无效")
    items = payload.get("pins")
    if not isinstance(items, list) or not 2 <= len(items) <= 1000:
        raise ValueError("图片中未识别出完整引脚，请上传完整封装图")
    pins, questions, positions, seen = [], [], {}, set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("引脚读取格式无效")
        number = str(item.get("pin_number") or "").strip()
        if not _valid_pin_number(number) or number in seen:
            raise ValueError(f"图片引脚编号缺失、无效或重复：{number}")
        seen.add(number)
        name = str(item.get("name") or "").strip()
        side = str(item.get("side") or "unknown").lower()
        x, y = _coordinate(item.get("x")), _coordinate(item.get("y"))
        if not x1 - .05 <= x <= x2 + .05 or not y1 - .05 <= y <= y2 + .05:
            raise ValueError(f"引脚 {number} 的连接位置不在主体边界附近")
        positions[number] = {"x": x, "y": y}
        # No electrical type is inferred when the diagram does not show it.
        pin = Pin(number, name or number, "PAS", side=side if side in {"left", "right", "top", "bottom"} else "unknown",
                  description=str(item.get("description") or ""), evidence=[Evidence(page=0, source="image")])
        if pin.side == "unknown":
            questions.append({"pin_number": number, "field": "side", "expected": "unknown",
                              "observed": "unknown", "needs_confirmation": True})
        try:
            confidence = float(item.get("confidence", 0))
        except (ValueError, TypeError):
            confidence = 0
        if not name or not .9 <= confidence <= 1 or item.get("uncertain"):
            questions.append({"pin_number": number, "field": "name", "expected": name,
                              "observed": name, "needs_confirmation": True})
        pins.append(pin)
    for side in ("left", "right", "top", "bottom"):
        axis = "y" if side in {"left", "right"} else "x"
        ordered = sorted((p for p in pins if p.side == side), key=lambda p: positions[p.pin_number][axis])
        for order, pin in enumerate(ordered):
            pin.side_order = order
    count = payload.get("pin_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 1000):
        raise ValueError("图片中的引脚数无效")
    return ImageReading(str(payload.get("device") or "").strip(), str(payload.get("package") or "").strip(),
                        pins, {"body_bbox": [x1, y1, x2, y2], "positions": positions,
                               "image_size": {"width": image_size["width"], "height": image_size["height"]}},
                        questions, count)


def build_image_layout(pins: list[Pin], geometry: dict, part_name: str) -> SymbolLayout:
    """Map source positions to a common grid; opposite sides share row coordinates."""
    x1, y1, x2, y2 = geometry["body_bbox"]
    size = geometry["image_size"]
    positions = {number: dict(point) for number, point in geometry["positions"].items()}
    # Nearby opposite-side anchors describe the same visible row/column. Snap
    # mutually nearest pairs before rounding, rather than drifting by one grid cell.
    for first, second, axis, span in (("left", "right", "y", y2 - y1), ("top", "bottom", "x", x2 - x1)):
        a, b = [p for p in pins if p.side == first], [p for p in pins if p.side == second]
        for p in a:
            if not b:
                continue
            q = min(b, key=lambda q: abs(positions[q.pin_number][axis] - positions[p.pin_number][axis]))
            closest = min(a, key=lambda r: abs(positions[r.pin_number][axis] - positions[q.pin_number][axis]))
            if closest is p and abs(positions[q.pin_number][axis] - positions[p.pin_number][axis]) <= span * .03:
                coordinate = (positions[p.pin_number][axis] + positions[q.pin_number][axis]) / 2
                positions[p.pin_number][axis] = positions[q.pin_number][axis] = coordinate
    aspect = (y2 - y1) * size["height"] / ((x2 - x1) * size["width"])
    longest = {side: max((len(p.name) for p in pins if p.side == side), default=0) for side in ("left", "right")}
    width = math.ceil(max(MIN_BODY_WIDTH, (longest["left"] + longest["right"]) * 6 + 30) / PITCH) * PITCH
    height = math.ceil(max(40, width * aspect) / PITCH) * PITCH
    scale = 1
    for side in ("left", "right", "top", "bottom"):
        axis, span, dimension = ("y", y2 - y1, height) if side in {"left", "right"} else ("x", x2 - x1, width)
        values = sorted(positions[p.pin_number][axis] for p in pins if p.side == side)
        for a, b in zip(values, values[1:]):
            if b - a < 1e-6:
                raise ValueError("同侧引脚位置重叠，请上传更清晰的图片")
            scale = max(scale, PITCH * span / ((b - a) * dimension))
    width, height = math.ceil(width * scale / PITCH) * PITCH, math.ceil(height * scale / PITCH) * PITCH
    if height > 10000 or width > 10000:
        raise ValueError("主体比例过大，请检查图片读取结果")
    half_w, half_h, grid = width // 2, height // 2, PITCH // 2
    placed, occupied = [], set()
    for pin in sorted(pins, key=lambda p: (p.side, p.side_order)):
        point = positions[pin.pin_number]
        if pin.side in {"left", "right"}:
            axis = round(((point["y"] - y1) / (y2 - y1) - .5) * height / grid) * grid
            axis = max(-half_h + grid, min(half_h - grid, axis))
            x, y = (-half_w if pin.side == "left" else half_w), axis
            hx, hy = x + (-PIN_LEN if pin.side == "left" else PIN_LEN), y
        elif pin.side in {"top", "bottom"}:
            axis = round(((point["x"] - x1) / (x2 - x1) - .5) * width / grid) * grid
            axis = max(-half_w + grid, min(half_w - grid, axis))
            x, y = axis, (-half_h if pin.side == "top" else half_h)
            hx, hy = x, y + (-PIN_LEN if pin.side == "top" else PIN_LEN)
        else:
            raise ValueError(f"引脚 {pin.pin_number} 的位置尚未确定")
        if (pin.side, axis) in occupied:
            raise ValueError(f"同侧引脚位置重叠：{pin.pin_number}，请上传更清晰的图片")
        occupied.add((pin.side, axis))
        placed.append(PlacedPin(pin.pin_number, pin.name, pin.side, x, y, hx, hy))
    return SymbolLayout(part_name, placed, width, height, half_h)
