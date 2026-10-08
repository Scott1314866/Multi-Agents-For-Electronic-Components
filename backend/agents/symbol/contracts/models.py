"""符号生成的数据模型（对应源程序的 ``app/models.py``）。

源文件在磁盘上是密文（DLP 透明加密），本地无明文副本、无 ``.pyc``、无 git
历史。本实现依据下列**硬证据**重建：

* 六份 ``*_spec.json`` 的实际字段名与出现顺序；
* 五处 import 站点 —— ``merge.py:16``、``cli.py:14``、``review.py:14``、
  ``selfcheck.py:26``、``tests/regress.py:46``；
* ``merge.py`` 里 ``Evidence(page=…, source="vision", agreement=True)`` 的
  构造式，以及 ``pin.evidence.append(...)`` 的用法；
* ``review.py:31`` 的提示词原文：「side 只能是 left / right / top / bottom /
  unknown」。

**字段顺序即 JSON 序列化顺序**，改动它会让新落盘快照与历史数据对不上。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

#: 引脚所在边。``unknown`` 表示视觉通道未给出（会被 build_layout 兜底到左侧）。
Side = Literal["left", "right", "top", "bottom", "unknown"]

#: 源程序实测出现过的引脚类型短码：IN / OUT / IO / PAS。
PinType = str


@dataclass
class Evidence:
    """一条证据。页码必填，来源标注它出自哪条通道。

    ``bbox`` 目前恒为 ``None`` —— 源程序的设计里它是"证据可回溯到图号"的
    兑现点，但因 MinerU 的归一化坐标与 PDF 点坐标不成比例而未落地
    （见 Module1-Spec §11.4）。这里保留字段以维持快照兼容。
    """

    page: int
    source: str  # "text" | "vision"
    bbox: list[float] | None = None
    agreement: bool = False


@dataclass
class Pin:
    """一个引脚。

    前三个字段必填，其余有默认值 —— ``tests/regress.py:513`` 用
    ``Pin(pin_number=…, name=…, type=…, side=…)`` 全关键字构造，
    证明了此后所有字段都必须是可省的。
    """

    pin_number: str
    name: str
    type: PinType
    side: Side = "unknown"
    #: 同侧内的次序（0 起）。``side == "unknown"`` 时恒为 0。
    side_order: int = 0
    #: 引脚描述（表格 DESCRIPTION 列）。**恒为 str，不能是 None** ——
    #: ``cli.py:59`` 会直接切片 ``pin.description[:44]``。
    description: str = ""
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Conflict:
    """两个通道对同一字段给出不同值，或只有一个通道有值。

    源程序的纪律是「不猜」：冲突显式记录，由人工裁决
    （``merge.apply_resolution``）后再写回 ``resolved``。
    """

    pin_number: str
    field: str  # "pin_number" | "side" | "name"
    values: dict[str, str]
    pages: list[int]
    resolved: str | None = None


@dataclass
class DeviceSpec:
    """一次提取的完整结果，也是 ``<型号>_spec.json`` 的结构。

    注意这里**没有** ``review`` —— 复核结果不进快照，只在
    ``pipeline.Outcome`` 里随内存传递（``web/service.py:6`` 明确
    「app/ 里只有 DeviceSpec 有 to_dict」）。
    """

    device: str
    package: str = ""
    package_full_name: str = ""
    #: ``{"file": 绝对路径, "pages": [页码…]}``，pages 是 locate.all_pages。
    source: dict[str, Any] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化成落盘快照的结构（键名与字段同名、顺序一致）。"""
        return {
            "device": self.device,
            "package": self.package,
            "package_full_name": self.package_full_name,
            "source": dict(self.source),
            "pins": [asdict(pin) for pin in self.pins],
            "conflicts": [asdict(conflict) for conflict in self.conflicts],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DeviceSpec":
        """从落盘快照还原（``pipeline.load_spec`` 用）。

        快照里的 ``evidence`` 是 dict 列表，这里还原成 :class:`Evidence`。
        """
        return cls(
            device=str(payload.get("device") or ""),
            package=str(payload.get("package") or ""),
            package_full_name=str(payload.get("package_full_name") or ""),
            source=dict(payload.get("source") or {}),
            pins=[
                Pin(
                    **{
                        **item,
                        "evidence": [
                            Evidence(**entry) for entry in (item.get("evidence") or [])
                        ],
                    }
                )
                for item in (payload.get("pins") or [])
            ],
            conflicts=[Conflict(**item) for item in (payload.get("conflicts") or [])],
        )
