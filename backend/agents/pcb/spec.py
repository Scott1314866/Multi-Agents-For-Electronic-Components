"""PCB 封装生成的声明式参数（PackageSpec）。

原先这些数值全部硬编码在 SKILL 脚本里（``Module_02/wson8_3x3/build_wson8.il``
的 L5-L9、L166-L203、L250-L274），改一处就要重跑整条 Allegro 流水线。
这里把它们外提为可校验的规格：SKILL 源码由
:mod:`backend.agents.pcb.skill_emitter` 渲染，几何由
:mod:`backend.agents.pcb.derive` 计算。

单位一律毫米，与 Allegro 侧的 ``axlDBChangeDesignUnits("millimeters" 4)`` 一致。

「给定」与「算出」的边界（v1 → 最终版踩坑换来的结论）：

* **器件图纸给定** —— 焊盘铜箔尺寸、散热盘尺寸、引脚跨距与 pitch、本体尺寸、体高；
* **规则算出** —— 阻焊外扩、钢网、引脚 Y 序列、丝印框、Place Bound、一脚标记。

把后者做成派生规则（而非独立参数）的价值，在 v1 那次修改里体现得很清楚：
``land_y 0.40 → 0.35`` 时阻焊自动跟着从 ``1.10 × 0.60`` 收到 ``1.00 × 0.45``，
``mask = copper + 0.10`` 这条关系不需要人去同步。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PinLayout = Literal["DUAL", "QUAD"]
PasteMode = Literal["MATCH_LAND", "INSET", "NONE"]
Pin1Marker = Literal["TRIANGLE", "NONE"]


class PackageSpec(BaseModel):
    """一个封装的全部生成参数。

    字段分组与 ``build_wson8.il`` 的段落一一对应，便于逐段对照迁移。
    """

    model_config = ConfigDict(extra="forbid")

    # ── 标识（对应 bName / bLand / bEp，L7-L9）─────────────────────
    name: str = Field(
        description="设计名；同时派生 padstack 名与 .dra/.psm 文件名，如 WSON8_3X3"
    )
    family: str = Field(default="WSON", description="封装族，决定几何规则集与编号约定")
    drawing_code: str = Field(default="", description="几何出处，仅写入文件头与日志")

    # ── 本体 ─────────────────────────────────────────────────────
    body_x: float = Field(gt=0, description="本体标称 X 尺寸 → 装配框边长")
    body_y: float = Field(gt=0, description="本体标称 Y 尺寸")
    body_height_max: float = Field(gt=0, description="最大高度 → PACKAGE_HEIGHT_MAX 属性")
    mold_flash: float = Field(
        default=0.05, ge=0, description="溢料/公差，决定 Place Bound 相对本体的外扩"
    )

    # ── 引脚 ─────────────────────────────────────────────────────
    pin_count: int = Field(gt=0, description="引脚总数，不含散热盘")
    pin_layout: PinLayout = Field(default="DUAL", description="DUAL 双列 / QUAD 四边")
    pin_pitch: float = Field(gt=0, description="同侧相邻引脚中心距")
    pin_columns_x: list[float] = Field(
        min_length=2, max_length=2, description="两列引脚的 X 坐标（左、右）"
    )
    pin_numbering: list[str] = Field(
        description="按物理位置排列的引脚号：左上起、逆时针逐列填"
    )
    pin_text_offset: float = Field(
        default=0.10, ge=0, description="引脚号文字相对焊盘向外偏移"
    )

    # ── 焊盘 ─────────────────────────────────────────────────────
    land_x: float = Field(gt=0, description="信号焊盘 X 尺寸（长边，指向本体中心）")
    land_y: float = Field(gt=0, description="信号焊盘 Y 尺寸（跨 pitch 方向）")
    nsmd: bool = Field(
        default=True, description="阻焊按 expansion 外扩（NSMD）；False 则内缩"
    )
    mask_expansion: float = Field(default=0.05, ge=0, description="阻焊每边外扩量")
    paste_mode: PasteMode = Field(default="MATCH_LAND", description="钢网尺寸策略")
    paste_inset: float = Field(default=0.0, ge=0, description="paste_mode=INSET 时的内缩量")
    min_clearance: float = Field(
        default=0.15,
        gt=0,
        description="铜箔最小间距；Allegro DRC 的实测阈值落在 (0.10, 0.15]",
    )

    # ── 散热盘 ───────────────────────────────────────────────────
    ep_present: bool = Field(default=True, description="是否生成散热焊盘与 EP 引脚")
    ep_x: float | None = Field(default=None, gt=0, description="散热盘 X 尺寸")
    ep_y: float | None = Field(default=None, gt=0, description="散热盘 Y 尺寸")
    ep_pin_number: str = Field(default="EP", description="散热盘的引脚号")

    # ── 丝印 / 装配 / Place Bound ────────────────────────────────
    silk_line_width: float = Field(default=0.12, gt=0)
    silk_clearance: float = Field(
        default=0.10, ge=0, description="丝印横杠相对本体半宽的外扩"
    )
    silk_span_extra: float = Field(
        default=0.05, ge=0, description="丝印横杠相对 silk_half 的横向延伸"
    )
    asm_line_width: float = Field(default=0.10, gt=0, description="装配框线宽（降级用）")
    pb_clearance: float = Field(
        default=0.05, ge=0, description="Place Bound 相对本体半宽的外扩"
    )
    pin1_marker: Pin1Marker = Field(default="TRIANGLE", description="一脚标记样式")
    pin1_marker_size: float = Field(default=0.30, gt=0, description="一脚标记边长")

    # ── 位号 ─────────────────────────────────────────────────────
    refdes_prefix: str = Field(default="U*", description="位号占位符")
    refdes_text_block: str = Field(default="3")
    refdes_silk_y: float | None = Field(
        default=None, description="丝印层位号 Y；留空则按丝印框派生"
    )

    # ── 运行环境 ─────────────────────────────────────────────────
    units: str = Field(default="millimeters")
    accuracy: int = Field(default=4, ge=0)
    extents: tuple[float, float, float, float] = Field(
        default=(-20.0, -20.0, 20.0, 20.0), description="图纸范围 (x0, y0, x1, y1)"
    )
    pin_text_block: str = Field(default="2", description="引脚号文字块号")

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> "PackageSpec":
        """结构自洽检查；间隙约束在 derive.check_clearances 里单独做。"""
        if len(self.pin_numbering) != self.pin_count:
            raise ValueError(
                f"pin_numbering 有 {len(self.pin_numbering)} 项，"
                f"与 pin_count={self.pin_count} 不符"
            )
        if len(set(self.pin_numbering)) != self.pin_count:
            raise ValueError("pin_numbering 含重复引脚号")
        if self.pin_layout == "DUAL" and self.pin_count % 2:
            raise ValueError("DUAL 封装的引脚数必须是偶数")
        if self.pin_layout == "QUAD" and self.pin_count % 4:
            raise ValueError("QUAD 封装的引脚数必须能被 4 整除")
        if self.ep_present and (self.ep_x is None or self.ep_y is None):
            raise ValueError("ep_present=True 时必须同时给出 ep_x 与 ep_y")
        if self.pin_columns_x[0] >= self.pin_columns_x[1]:
            raise ValueError("pin_columns_x 必须左小右大")
        return self

    # ── 派生命名（对应 bLand / bEp，L8-L9）──────────────────────

    @property
    def land_padstack_name(self) -> str:
        """信号脚焊盘堆栈名。"""
        return f"{self.name}_LAND"

    @property
    def ep_padstack_name(self) -> str:
        """散热盘焊盘堆栈名。"""
        return f"{self.name}_EP"

    @property
    def height_property_value(self) -> str:
        """PACKAGE_HEIGHT_MAX 的写入值。

        写入用 ``"0.8 mm"``，但 Allegro 回读时会归一化成大写 ``"0.8 MM"``，
        所以断言侧必须大写比较（见 :mod:`backend.agents.pcb.assertions`）。
        """
        return f"{self.body_height_max:g} mm"


def wson8_3x3() -> PackageSpec:
    """WSON-8 3.0×3.0×0.8 —— TI DRG0008A / 图号 4218885A（2020-03）。

    这是本次迁移的**对照样本**：``skill_emitter`` 用它渲染出的 SKILL，
    应与 ``Module_02/wson8_3x3/build_wson8.il`` 语义一致（同一组坐标、
    同一组焊盘尺寸、同一套层名）。

    数值来源：``build_wson8.il`` L166-L203、L250-L274，以及 ``_scratch/lp_full8x.png``
    上那条红字批注「焊盘可以放长但是不可以放宽」—— v1 把 ``land_y`` 收到 0.35
    正是照这句话改的。
    """
    return PackageSpec(
        name="WSON8_3X3",
        family="WSON",
        drawing_code="TI DRG0008A / drawing code 4218885A (03/2020)",
        body_x=3.00,
        body_y=3.00,
        body_height_max=0.8,
        mold_flash=0.05,
        pin_count=8,
        pin_layout="DUAL",
        pin_pitch=0.50,
        pin_columns_x=[-1.15, 1.15],
        pin_numbering=["1", "2", "3", "4", "8", "7", "6", "5"],
        pin_text_offset=0.10,
        land_x=0.90,
        land_y=0.35,
        nsmd=True,
        mask_expansion=0.05,
        paste_mode="MATCH_LAND",
        min_clearance=0.15,
        ep_present=True,
        ep_x=1.10,
        ep_y=2.00,
        ep_pin_number="EP",
        silk_line_width=0.12,
        silk_clearance=0.10,
        silk_span_extra=0.05,
        asm_line_width=0.10,
        pb_clearance=0.05,
        pin1_marker="TRIANGLE",
        pin1_marker_size=0.30,
        refdes_prefix="U*",
        refdes_text_block="3",
        refdes_silk_y=2.30,
    )
