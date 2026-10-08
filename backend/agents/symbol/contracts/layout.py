"""引脚 → OrCAD 符号布局几何（对应源程序的 ``app/symbol.py``）。

源文件是密文。本实现的公式全部由**两份真实生成物逐位复算**得出：

* ``spike/c_out4/ADS1115_gen.tcl``（181 行，仅左右两边，5+5 脚）
* ``spike/s_stm32_web/702bb3679978/STM32F105VCT6_gen.tcl``（990 行，四边 100 脚）

两份脚本的坐标可完全复现，故几何规则不是推测。

单位是 **DBO**（Cadence 的库单位）：**1 DBO = 10 mil**。实测取值 —— 引脚间距
20 单位（200 mil）、引脚长 10 单位（100 mil，配 ``SetIsLong 0`` 即显示为
Short）、体宽 100 单位。

**一个反直觉之处**（源程序 §10.5 用三条独立证据交叉验证过）：
DBO 符号坐标里 **+Y 向下**。y 越小的引脚，在 Capture 里显示得越靠上。
所以左侧是"自上而下 = 引脚号递增"，对应代码里 ``side_order`` 递增时 y 递增。

**另一个必须保留的隐式契约**：``build_layout`` 会把 ``side == "unknown"`` 的
引脚**原地改写**成 ``"left"``。源程序的 ``web/service.py`` 为此三处注释反复
强调「喂副本进去」—— 否则「全部侧别未定」这条 error 就永远不触发了。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.agents.symbol.contracts.models import Pin, Side

#: 引脚间距（DBO）。1 单位 = 10 mil，故 20 单位 = 200 mil。
PITCH = 20

#: 引脚长度（DBO）。10 单位 = 100 mil，配合 SetIsLong 0 即为 Shape=Short。
PIN_LEN = 10

#: 体宽下限（DBO）。左右各 5 脚的 ADS1115 就是 100。
MIN_BODY_WIDTH = 100

_SIDES = ("left", "right", "top", "bottom")


@dataclass
class PlacedPin:
    """落在符号上的一枚引脚（DBO 坐标）。

    ``(x, y)`` 是引脚起点（贴体外缘），``(hx, hy)`` 是 hotspot。实测两者相差
    恰好 ``PIN_LEN``：左/右引脚横向差，顶/底引脚纵向差。
    """

    number: str
    name: str
    side: Side
    x: int
    y: int
    hx: int
    hy: int


@dataclass
class SymbolLayout:
    """符号的完整布局。"""

    part_name: str
    pins: list[PlacedPin] = field(default_factory=list)
    #: 体宽/体高，DBO 单位。乘 10 即 mil（``web/service.py`` 就是这么换算的）。
    body_width: int = 0
    body_height: int = 0
    half_height: int = 0
    warnings: list[str] = field(default_factory=list)


def _sequence(count: int) -> list[int]:
    """一侧 ``count`` 个引脚的 y 坐标序列（自上而下）。

    实测 ADS1115 的 5 脚是 ``-40, -20, 0, 20, 40``（half_h=50），
    STM32 的 25 脚是 ``-240 … +240``（half_h=250），
    即 ``y_i = -(half_h - PITCH//2) + i × PITCH``。
    """
    half = count * PITCH // 2
    return [-(half - PITCH // 2) + index * PITCH for index in range(count)]


def build_layout(pins: list[Pin], part_name: str = "") -> SymbolLayout:
    """把引脚摆到一个矩形符号的四条边上。

    Args:
        pins: 引脚清单。**注意会被原地修改** —— ``side == "unknown"`` 的
            引脚会被改写成 ``"left"``（见模块文档）。需要保留原始侧别时，
            请先 :func:`copy.deepcopy`。
        part_name: 器件名，写入布局并在后续用于拼文件名与 TCL 标识符。

    Returns:
        含四条边坐标、体宽体高与警告的布局。

    Raises:
        ValueError: 一个引脚都没有。
    """
    warnings: list[str] = []
    unknown = 0
    for pin in pins:
        if pin.side not in _SIDES:
            pin.side = "left"  # 原地改写，源程序行为如此
            unknown += 1
    if unknown:
        warnings.append(f"{unknown} 个引脚侧别未定，已默认放到左侧")

    if not pins:
        raise ValueError("没有引脚可以布局")

    grouped: dict[str, list[Pin]] = {side: [] for side in _SIDES}
    for pin in pins:
        grouped[pin.side].append(pin)
    for side in _SIDES:
        grouped[side].sort(key=lambda item: item.side_order)

    left, right = grouped["left"], grouped["right"]
    top, bottom = grouped["top"], grouped["bottom"]

    # 体高只看左右两侧 —— 顶/底的引脚不参与半高计算（实测如此）。
    half_height = max(len(left), len(right)) * PITCH // 2
    body_height = half_height * 2

    # 体宽：两个数据点（ADS1115 = 100、STM32 = 520）推出的规则。
    # 没顶底脚时取下限 100；有顶底脚时要让它们排得下并留出一个引脚长。
    top_bottom = max(len(top), len(bottom))
    half_width = max(MIN_BODY_WIDTH // 2, top_bottom * PITCH // 2 + PIN_LEN)
    body_width = half_width * 2

    placed: list[PlacedPin] = []

    # 左右：y 自上而下递增，hotspot 向体外伸出
    for index, pin in enumerate(left):
        y = _sequence(len(left))[index]
        placed.append(
            PlacedPin(
                number=pin.pin_number, name=pin.name, side="left",
                x=-half_width, y=y, hx=-half_width - PIN_LEN, hy=y,
            )
        )
    for index, pin in enumerate(right):
        y = _sequence(len(right))[index]
        placed.append(
            PlacedPin(
                number=pin.pin_number, name=pin.name, side="right",
                x=half_width, y=y, hx=half_width + PIN_LEN, hy=y,
            )
        )
    # 顶/底：x 左右铺开，hotspot 纵向伸出
    for index, pin in enumerate(top):
        x = _sequence(len(top))[index]
        placed.append(
            PlacedPin(
                number=pin.pin_number, name=pin.name, side="top",
                x=x, y=-half_height, hx=x, hy=-half_height - PIN_LEN,
            )
        )
    for index, pin in enumerate(bottom):
        x = _sequence(len(bottom))[index]
        placed.append(
            PlacedPin(
                number=pin.pin_number, name=pin.name, side="bottom",
                x=x, y=half_height, hx=x, hy=half_height + PIN_LEN,
            )
        )

    return SymbolLayout(
        part_name=part_name,
        pins=placed,
        body_width=body_width,
        body_height=body_height,
        half_height=half_height,
        warnings=warnings,
    )


def describe(layout: SymbolLayout) -> str:
    """给人看的布局摘要（源程序 ``describe(layout)`` 无断言依赖，格式自由）。"""
    counts = {side: 0 for side in _SIDES}
    for pin in layout.pins:
        counts[pin.side] = counts.get(pin.side, 0) + 1
    lines = [
        f"{layout.part_name or '(未命名)'}：{len(layout.pins)} 脚"
        f"（左 {counts['left']} / 右 {counts['right']} / "
        f"上 {counts['top']} / 下 {counts['bottom']}）",
        f"  体尺寸 {layout.body_width} × {layout.body_height} DBO"
        f"（{layout.body_width * 10} × {layout.body_height * 10} mil）",
        f"  引脚间距 {PITCH} DBO（{PITCH * 10} mil），引脚长 {PIN_LEN} DBO",
    ]
    for warning in layout.warnings:
        lines.append(f"  ! {warning}")
    return "\n".join(lines)


def preview(layout: SymbolLayout) -> str:
    """ASCII 预览：把四条边的引脚号画成一个矩形。

    与 ``cli.py`` 的 ``show_layout_preview`` 同一个用途 —— 让人在按下"生成"
    之前，一眼看出引脚是不是全堆到了一边。
    """
    counts = {side: [] for side in _SIDES}
    for pin in layout.pins:
        counts[pin.side].append(pin.number)

    width = max((len(",".join(counts["top"])) for _ in [0]), default=0) + 4
    width = max(width, 12)
    pad = " " * 4

    lines = [pad + ",".join(counts["top"]).center(width, "-")]
    rows = max(len(counts["left"]), len(counts["right"]), 1)
    for index in range(rows):
        left = counts["left"][index] if index < len(counts["left"]) else ""
        right = counts["right"][index] if index < len(counts["right"]) else ""
        lines.append(f"{left:>4} |{' ' * width}| {right:<4}")
    lines.append(pad + ",".join(counts["bottom"]).center(width, "-"))
    return "\n".join(lines)
