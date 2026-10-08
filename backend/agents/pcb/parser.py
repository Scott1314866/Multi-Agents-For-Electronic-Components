"""把 Allegro 写出的日志解析成结构化证据。

``.dra``/``.psm``/``.pad`` 都是私有二进制，**不能靠文本 diff 判定成败**
（``strings`` 只能取到内嵌的 QuickView 文字，且完全不含尺寸数字）。
唯一可靠的机读真相是 SKILL 自己转储的文本日志：

===========================  ============================================
文件                          内容
===========================  ============================================
``build.log``                构建过程 + ``bDumpDesign`` 的引脚与对象清单
``verify.log``               重新打开成品后的回读（几何的权威来源）
``props.log``                 属性在存盘-重开之后是否还在
``<小写符号名>.log``          Allegro 自己的编译报告（``SPMHA1-301`` 在这里）
===========================  ============================================

四份都要读：DRC 警告只在最后一份里，而 ``batch_drc.log`` 是**诱饵** ——
它记录的是打开模板时的状态，不是构建结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_BBOX_RE = re.compile(
    r"\(\(\((-?[\d.]+) (-?[\d.]+)\) \((-?[\d.]+) (-?[\d.]+)\)\)\)"
)
_QUOTED_RE = re.compile(r'\("([^"]+)"\)')
_NUMBER_RE = re.compile(r"\((-?[\d.]+)\)")
_PROP_ENTRY_RE = re.compile(r'(\w+)\s+"([^"]*)"')

#: Allegro 编译期的 DRC 警告码（只在 ``<符号名>.log`` 里出现）。
DRC_WARNING_CODE = "SPMHA1-301"


@dataclass(frozen=True)
class BBox:
    """SKILL 打印的矩形 ``(((x0 y0) (x1 y1)))``。"""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return round(abs(self.x1 - self.x0), 6)

    @property
    def height(self) -> float:
        return round(abs(self.y1 - self.y0), 6)

    @property
    def center(self) -> tuple[float, float]:
        return (round((self.x0 + self.x1) / 2, 6), round((self.y0 + self.y1) / 2, 6))


@dataclass(frozen=True)
class PinRecord:
    """``verify.log`` 的一个引脚。"""

    number: str
    bbox: BBox | None = None
    padstack: str | None = None
    rotation: float | None = None


@dataclass(frozen=True)
class ObjectRecord:
    """``verify.log`` / ``build.log`` 的一个图形对象。"""

    obj_type: str
    layer: str | None
    bbox: BBox | None = None


@dataclass(frozen=True)
class PropertyRecord:
    """``props.log`` 的一个对象的属性表。"""

    layer: str | None
    raw: str

    @property
    def entries(self) -> dict[str, str]:
        """属性名 → 值。注意 Allegro 会把值**大写归一**（``0.8 mm`` → ``0.8 MM``）。"""
        return {name: value for name, value in _PROP_ENTRY_RE.findall(self.raw)}

    @property
    def is_empty(self) -> bool:
        return "nil" in self.raw and not self.entries


@dataclass(frozen=True)
class BuildLog:
    version: str = ""
    design_name: str = ""
    padstacks: dict[str, dict[str, str]] = field(default_factory=dict)
    placed_pins: list[tuple[str, float, float]] = field(default_factory=list)
    dumped_pin_count: int = 0
    dumped_objects: list[ObjectRecord] = field(default_factory=list)
    saved_dra: str | None = None
    compiled_psm: str | None = None
    complete: bool = False

    @property
    def drc_objects(self) -> list[ObjectRecord]:
        return [o for o in self.dumped_objects if o.obj_type == "drc"]


@dataclass(frozen=True)
class VerifyLog:
    version: str = ""
    design_type: str | None = None
    units: str | None = None
    accuracy: int | None = None
    extents: BBox | None = None
    pin_count: int = 0
    pins: list[PinRecord] = field(default_factory=list)
    object_count: int = 0
    objects: list[ObjectRecord] = field(default_factory=list)
    properties: list[PropertyRecord] = field(default_factory=list)
    complete: bool = False

    @property
    def layers(self) -> set[str]:
        return {o.layer for o in self.objects if o.layer}

    @property
    def drc_objects(self) -> list[ObjectRecord]:
        return [o for o in self.objects if o.obj_type == "drc"]


@dataclass(frozen=True)
class PropsLog:
    object_count: int = 0
    properties: list[PropertyRecord] = field(default_factory=list)
    complete: bool = False

    @property
    def with_properties(self) -> list[PropertyRecord]:
        return [p for p in self.properties if not p.is_empty]


def parse_bbox(text: str) -> BBox | None:
    """从一段 SKILL 输出里取第一个矩形。"""
    match = _BBOX_RE.search(text)
    if match is None:
        return None
    return BBox(*(float(g) for g in match.groups()))


def _quoted(text: str) -> str | None:
    match = _QUOTED_RE.search(text)
    return match.group(1) if match else None


def _number(text: str) -> int | None:
    match = _NUMBER_RE.search(text)
    return int(float(match.group(1))) if match else None


def parse_build_log(text: str) -> BuildLog:
    """解析 ``build.log``。"""
    version = ""
    design_name = ""
    padstacks: dict[str, dict[str, str]] = {}
    placed: list[tuple[str, float, float]] = []
    dumped_pins = 0
    objects: list[ObjectRecord] = []
    saved: str | None = None
    compiled: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version="):
            version = _quoted(stripped) or ""
        elif stripped.startswith("rename "):
            design_name = _quoted(stripped) or ""
        elif match := re.match(r"PADSTACK (\S+) (\w+)=(.*)$", stripped):
            padstacks.setdefault(match.group(1), {})[match.group(2)] = match.group(3)
        elif match := re.match(r"PIN (\S+) xy=(-?[\d.]+):(-?[\d.]+) ", stripped):
            placed.append((match.group(1), float(match.group(2)), float(match.group(3))))
        elif stripped.startswith("DUMP pin_count="):
            dumped_pins = int(stripped.split("=", 1)[1])
        elif stripped.startswith("DUMP object_count="):
            pass  # 对象清单以逐行 OBJ 为准
        elif stripped.startswith("OBJ "):
            objects.append(_parse_object(stripped))
        elif stripped.startswith("SAVE dra ="):
            saved = _quoted(stripped)
        elif stripped.startswith("COMPILE psm ="):
            compiled = _quoted(stripped)

    return BuildLog(
        version=version,
        design_name=design_name,
        padstacks=padstacks,
        placed_pins=placed,
        dumped_pin_count=dumped_pins,
        dumped_objects=objects,
        saved_dra=saved,
        compiled_psm=compiled,
        complete="==== BUILD END ====" in text,
    )


def _parse_object(line: str) -> ObjectRecord:
    obj_type = _quoted(line.split("type=", 1)[1]) if "type=" in line else ""
    layer = _quoted(line.split("layer=", 1)[1]) if "layer=" in line else None
    return ObjectRecord(obj_type=obj_type or "", layer=layer, bbox=parse_bbox(line))


def parse_verify_log(text: str) -> VerifyLog:
    """解析 ``verify.log``：几何的权威来源。"""
    version = ""
    design_type: str | None = None
    units: str | None = None
    accuracy: int | None = None
    extents: BBox | None = None
    pin_count = 0
    pins: list[PinRecord] = []
    object_count = 0
    objects: list[ObjectRecord] = []
    properties: list[PropertyRecord] = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version="):
            version = _quoted(stripped) or ""
        elif stripped.startswith("designType="):
            design_type = _quoted(stripped)
        elif stripped.startswith("units="):
            units = _quoted(stripped)
            accuracy = _number(stripped.split("accuracy=", 1)[1])
            extents = parse_bbox(stripped)
        elif stripped.startswith("pin_count="):
            pin_count = int(stripped.split("=", 1)[1])
        elif stripped.startswith("PIN number="):
            pins.append(
                PinRecord(
                    number=_quoted(stripped) or "",
                    padstack=_quoted(stripped.split("padstack=", 1)[1])
                    if "padstack=" in stripped
                    else None,
                    bbox=parse_bbox(stripped),
                )
            )
        elif stripped.startswith("object_count="):
            object_count = int(stripped.split("=", 1)[1])
        elif stripped.startswith("OBJ type="):
            objects.append(_parse_object(stripped))
        elif stripped.startswith("PROPOBJ "):
            layer = _quoted(stripped.split("layer=", 1)[1]) if "layer=" in stripped else None
            raw = stripped.split("prop=", 1)[1] if "prop=" in stripped else "nil"
            properties.append(PropertyRecord(layer=layer, raw=raw))

    return VerifyLog(
        version=version,
        design_type=design_type,
        units=units,
        accuracy=accuracy,
        extents=extents,
        pin_count=pin_count,
        pins=pins,
        object_count=object_count,
        objects=objects,
        properties=properties,
        complete="==== VERIFY END ====" in text,
    )


def parse_props_log(text: str) -> PropsLog:
    """解析 ``props.log``（``OBJ layer=`` 与下一行 ``props=`` 成对出现）。"""
    object_count = 0
    properties: list[PropertyRecord] = []
    pending_layer: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("object_count="):
            object_count = int(stripped.split("=", 1)[1])
        elif stripped.startswith("OBJ layer="):
            pending_layer = _quoted(stripped)
        elif stripped.startswith("props="):
            properties.append(
                PropertyRecord(layer=pending_layer, raw=stripped.split("=", 1)[1])
            )
            pending_layer = None

    return PropsLog(
        object_count=object_count,
        properties=properties,
        complete="==== PROPERTY CHECK END ====" in text,
    )


def has_drc_warning(text: str) -> bool:
    """``<符号名>.log`` 里是否出现编译期 DRC 警告。

    这是构建期唯一能反映几何间距违规的信号 —— ``build.log`` 通篇不含
    "DRC" 字样，只能从 ``object_count`` 变大间接看出。
    """
    return DRC_WARNING_CODE in text


def read_logs(work_dir: Path, design_name: str) -> dict[str, str]:
    """读取一个工作目录下的四份日志（缺失的以空串占位）。

    路径约定与 :mod:`backend.agents.pcb.skill_emitter` 一致：
    日志名固定，而 Allegro 的编译报告取**小写**符号名。
    """
    work_dir = Path(work_dir)
    stem = design_name.lower()
    candidates = {
        "build": work_dir / "build.log",
        "verify": work_dir / "verify.log",
        "props": work_dir / "props.log",
        "symbol": work_dir / f"{stem}.log",
    }
    return {
        key: path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        for key, path in candidates.items()
    }
