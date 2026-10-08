"""从 :class:`PackageSpec` 派生几何（纯函数，可单测）。

源程序把这些数字直接写死在 ``build_wson8.il`` 的 ``bMain()`` 里
（L166-L203、L250-L274）：改一个焊盘尺寸要手算其余全部坐标，而且同一组
数字在 Python 侧的图例文字里又抄了一份（``render_preview.py``，现已损毁），
改一处必错一处。本模块把「给定 → 算出」的边界固化下来，只此一份真相。

对照样本：``build_wson8.il`` 的硬编码值全部可由下述规则复算出来 ——
``asm_half 1.50``、``pb_half 1.55``、``silk_half 1.60``、``silk_span 1.65``、
三脚标记 ``(-1.95, -1.65, 1.75, 1.45)``、引脚序列 ``[0.75, 0.25, -0.25, -0.75]``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.agents.pcb.spec import PackageSpec


@dataclass(frozen=True)
class PinPlacement:
    """一个引脚的最终落点（毫米）。"""

    number: str
    x: float
    y: float
    text_offset: tuple[float, float]


@dataclass(frozen=True)
class PadGeometry:
    """一个焊盘堆栈的三层尺寸（铜箔 / 钢网 / 阻焊）。"""

    copper: tuple[float, float]
    paste: tuple[float, float]
    mask: tuple[float, float]


@dataclass(frozen=True)
class DerivedPackage:
    """一个 :class:`PackageSpec` 派生出的全部几何。"""

    pins: list[PinPlacement]
    land_pad: PadGeometry
    ep_pad: PadGeometry | None
    asm_half: float
    pb_half: float
    silk_half: float
    silk_span: float
    refdes_silk_y: float
    #: 一脚标记的四角 (west, east, top, bottom)；pin1_marker=NONE 时为 None
    pin1_marker: tuple[float, float, float, float] | None
    warnings: list[str] = field(default_factory=list)


def pin_y_sequence(count: int, pitch: float) -> list[float]:
    """一侧 ``count`` 个引脚相对中心的 Y 序列，自上而下递减。

    对应 ``build_wson8.il`` L180-L183 的 ``0.75 / 0.25 / -0.25 / -0.75``：
    ``y_k = pitch × ((count-1)/2 − k)``。
    """
    half = (count - 1) / 2
    return [round(pitch * (half - k), 6) for k in range(count)]


def _pad_geometry(
    copper: tuple[float, float],
    *,
    expansion: float,
    paste_mode: str,
    paste_inset: float,
    nsmd: bool,
) -> PadGeometry:
    """由铜箔尺寸派生钢网与阻焊。

    规则（``build_wson8.il`` L170/L172 的调用点写死了结果，这里还原成公式）：

    * 钢网 ``paste = copper``（``MATCH_LAND``，1:1）；
    * 阻焊 ``mask = copper + 2 × expansion``（NSMD，每边外扩）。

    v1 把 ``land_y`` 从 0.40 收到 0.35 时，阻焊自动从 ``1.10 × 0.60``
    跟到 ``1.00 × 0.45`` —— 正是因为有这条规则，不需要手工同步。
    """
    w, h = copper
    sign = 1.0 if nsmd else -1.0
    mask = (round(w + 2 * sign * expansion, 6), round(h + 2 * sign * expansion, 6))
    if paste_mode == "MATCH_LAND":
        paste = (w, h)
    elif paste_mode == "INSET":
        paste = (round(w - 2 * paste_inset, 6), round(h - 2 * paste_inset, 6))
    else:  # NONE
        paste = (0.0, 0.0)
    return PadGeometry(copper=(w, h), paste=paste, mask=mask)


def _place_dual(spec: PackageSpec) -> list[PinPlacement]:
    """双列封装：左上起逆时针，左列自上而下、右列自上而下续排。

    引脚号顺序来自 ``pin_numbering``（WSON8_3X3 是 ``1,2,3,4,8,7,6,5``），
    与 ``build_wson8.il`` L180-L187 的调用顺序一致。

    引脚号文字横向偏移取「向外」方向：左列 −，右列 +（L178-L179 的
    ``m125=−1.25`` / ``p125=+1.25``，即 ``pin_x ± 0.10``）。
    """
    per_side = spec.pin_count // 2
    ys = pin_y_sequence(per_side, spec.pin_pitch)
    placements: list[PinPlacement] = []
    for index in range(spec.pin_count):
        column = 0 if index < per_side else 1
        row = index if index < per_side else index - per_side
        x = spec.pin_columns_x[column]
        outward = spec.pin_text_offset if x >= 0 else -spec.pin_text_offset
        placements.append(
            PinPlacement(
                number=spec.pin_numbering[index],
                x=x,
                y=ys[row],
                text_offset=(round(x + outward, 6), 0.0),
            )
        )
    return placements


def derive_package(spec: PackageSpec) -> DerivedPackage:
    """计算一个封装的全部几何。

    Args:
        spec: 已通过结构自洽校验的封装规格。

    Returns:
        引脚落点、两个焊盘堆栈的三层尺寸、各层框线与一脚标记。

    Raises:
        NotImplementedError: ``pin_layout`` 不是 ``DUAL``。
            目前只有 WSON8_3X3 这一个经样本验证的布局；QUAD 的引脚次序
            需要新的器件样本与 Allegro 日志才能确证，不猜测。
    """
    if spec.pin_layout != "DUAL":
        raise NotImplementedError(
            f"暂不支持 {spec.pin_layout} 布局：只有 DUAL（WSON8_3X3）经过样本验证。"
            "QUAD 的引脚次序与四边几何需要新的器件样本和 Allegro 日志才能确证。"
        )

    warnings: list[str] = []
    if abs(spec.body_x - spec.body_y) > 1e-9:
        warnings.append(
            f"本体非正方形（{spec.body_x} × {spec.body_y}）；丝印与装配框目前"
            "按 X 半宽派生（源自 WSON8_3X3 的正方形样本），矩形本体需要新样本验证"
        )

    pins = _place_dual(spec)
    land_pad = _pad_geometry(
        (spec.land_x, spec.land_y),
        expansion=spec.mask_expansion,
        paste_mode=spec.paste_mode,
        paste_inset=spec.paste_inset,
        nsmd=spec.nsmd,
    )
    ep_pad = None
    if spec.ep_present and spec.ep_x is not None and spec.ep_y is not None:
        ep_pad = _pad_geometry(
            (spec.ep_x, spec.ep_y),
            expansion=spec.mask_expansion,
            paste_mode=spec.paste_mode,
            paste_inset=spec.paste_inset,
            nsmd=spec.nsmd,
        )

    asm_half = round(spec.body_x / 2, 6)
    pb_half = round(asm_half + spec.mold_flash, 6)
    silk_half = round(asm_half + spec.silk_clearance, 6)
    silk_span = round(silk_half + spec.silk_span_extra, 6)

    refdes_silk_y = (
        spec.refdes_silk_y
        if spec.refdes_silk_y is not None
        else round(silk_half + 0.70, 6)
    )

    marker: tuple[float, float, float, float] | None = None
    if spec.pin1_marker == "TRIANGLE":
        size = spec.pin1_marker_size
        marker = (
            round(-(silk_span + size), 6),  # west：贴丝印框左上角外侧
            round(-silk_span, 6),  # east
            round(silk_half + size / 2, 6),  # top
            round(silk_half - size / 2, 6),  # bottom
        )

    return DerivedPackage(
        pins=pins,
        land_pad=land_pad,
        ep_pad=ep_pad,
        asm_half=asm_half,
        pb_half=pb_half,
        silk_half=silk_half,
        silk_span=silk_span,
        refdes_silk_y=refdes_silk_y,
        pin1_marker=marker,
        warnings=warnings,
    )


def check_clearances(spec: PackageSpec) -> list[str]:
    """生成前的硬校验：铜箔间距是否够。返回问题清单，空列表表示通过。

    Allegro 的 DRC 判的是**铜箔**间距，阈值落在 ``(0.10, 0.15]`` —— 这是
    v1 与最终版对照出来的唯一实证：最终版两条间隙都恰好 0.15 时
    ``object_count`` 是 16 且无 ``drc`` 对象；v1 两条都是 0.10，
    ``object_count`` 变成 30、多出 14 个 ``layer=("DRC ERROR CLASS/TOP")``
    的对象（6 个相邻引脚 + 8 个引脚↔散热盘）。

    必须在写第一个几何对象之前跑完，否则错误只在编译期的
    ``<符号名>.log`` 里以 ``SPMHA1-301`` 出现，排查成本高得多。
    """
    issues: list[str] = []

    # 间距是十进制值（0.15 这类），但二进制浮点算出来可能是
    # 0.14999999999999991。先归整到 1e-9 再比较，否则恰好达标的规格会被误报。
    def _gap(value: float) -> float:
        return round(value, 9)

    gap_pin = _gap(spec.pin_pitch - spec.land_y)
    if gap_pin < spec.min_clearance:
        issues.append(
            f"相邻引脚铜箔间距 {gap_pin:.3f} mm < {spec.min_clearance} mm"
            f"（pitch {spec.pin_pitch} - land_y {spec.land_y}）"
            f"，会报 {spec.pin_count // 2 - 1} × 2 处相邻引脚 DRC；"
            "按 TI 图纸惯例应收窄 land_y 而不是 pitch"
        )

    if spec.ep_present and spec.ep_x is not None:
        inner_x = min(abs(x) for x in spec.pin_columns_x)
        gap_ep = _gap(inner_x - spec.land_x / 2 - spec.ep_x / 2)
        if gap_ep < spec.min_clearance:
            issues.append(
                f"引脚-散热盘铜箔间距 {gap_ep:.3f} mm < {spec.min_clearance} mm"
                f"（|pin_x| {inner_x} - land_x/2 - ep_x/2）"
                f"，会报 {spec.pin_count} 处引脚-散热盘 DRC"
            )

    return issues


def summarize(spec: PackageSpec, derived: DerivedPackage) -> str:
    """给日志与人工复核用的一行人话摘要。"""
    parts = [
        f"{spec.name}（{spec.family}，{spec.pin_count} 脚 + {'EP' if derived.ep_pad else '无 EP'}）",
        f"焊盘 {spec.land_x:.2f}×{spec.land_y:.2f} → 阻焊 "
        f"{derived.land_pad.mask[0]:.2f}×{derived.land_pad.mask[1]:.2f}",
        f"本体 {spec.body_x:.2f}×{spec.body_y:.2f}，装配框 ±{derived.asm_half:.2f}，"
        f"PlaceBound ±{derived.pb_half:.2f}，丝印 ±{derived.silk_half:.2f}",
        f"间距：相邻引脚 {spec.pin_pitch - spec.land_y:.3f}",
    ]
    if derived.ep_pad is not None and spec.ep_x is not None:
        inner_x = min(abs(x) for x in spec.pin_columns_x)
        parts.append(f"引脚-EP {inner_x - spec.land_x / 2 - spec.ep_x / 2:.3f}")
    return "；".join(parts)
