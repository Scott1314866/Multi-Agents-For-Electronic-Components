"""对 Allegro 日志做断言，判定一次封装生成是否成功。

四条纪律，全部有实证：

1. **只信副作用，不信返回值** —— ``bMakePadstack`` 恒返 ``nil`` 而实际成功
   （``build.log`` 的 ``land padstack ok=nil`` 与紧随其后的 ``toDisk=(t)``
   并存）；``axlDBCreatePin`` 返回的 flag 更离谱，同一份代码两次运行会给出
   相反的值（最终版 9 个引脚全 ``nil``，v1 里 8 个是 ``t``）。
2. **只比集合，不比顺序** —— ``build.log`` 与 ``verify.log`` 的对象枚举顺序
   不同，逐行 diff 会误报。
3. **属性值大写归一** —— 源码写 ``"0.8 mm"``，回读是 ``"0.8 MM"``。
4. **DRC 警告在 ``<符号名>.log``**，不在 ``build.log``；``batch_drc.log``
   是诱饵（记录的是打开模板时的状态）。

判据的期望值全部由 :mod:`backend.agents.pcb.derive` 算出，不是抄来的常数 ——
改一个焊盘尺寸，断言会自动跟着走。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from backend.agents.pcb.derive import DerivedPackage
from backend.agents.pcb.parser import (
    BuildLog,
    PropsLog,
    VerifyLog,
    has_drc_warning,
)
from backend.agents.pcb.spec import PackageSpec

#: 日志里的坐标保留 3 位小数，比较留一点余量。
TOLERANCE = 1e-3

PLACE_BOUND_LAYER = "PACKAGE GEOMETRY/PLACE_BOUND_TOP"

#: 正常构建必须出现的层（``_scratch/probe_layers2.log`` 实测的合法层名子集）。
REQUIRED_LAYERS = {
    "PACKAGE GEOMETRY/ASSEMBLY_TOP",
    "PACKAGE GEOMETRY/PLACE_BOUND_TOP",
    "PACKAGE GEOMETRY/SILKSCREEN_TOP",
    "REF DES/ASSEMBLY_TOP",
    "REF DES/SILKSCREEN_TOP",
}

#: 允许在最终图纸里出现的对象类型（多出 ``drc`` 即失败）。
ALLOWED_OBJECT_TYPES = {"polygon", "shape", "path", "pin", "text"}


@dataclass(frozen=True)
class Assertion:
    """一条判据。"""

    name: str
    passed: bool
    expected: str = ""
    actual: str = ""
    detail: str = ""


@dataclass(frozen=True)
class Verdict:
    """一次生成的全部判据结果。"""

    spec_name: str
    assertions: list[Assertion]

    @property
    def passed(self) -> bool:
        return bool(self.assertions) and all(a.passed for a in self.assertions)

    @property
    def failures(self) -> list[Assertion]:
        return [a for a in self.assertions if not a.passed]

    def to_dict(self) -> dict:
        return {
            "spec_name": self.spec_name,
            "passed": self.passed,
            "assertions": [asdict(a) for a in self.assertions],
        }

    def to_text(self) -> str:
        head = "PASS" if self.passed else f"FAIL（{len(self.failures)} 条未过）"
        lines = [f"[{head}] {self.spec_name}"]
        for item in self.assertions:
            mark = "ok  " if item.passed else "FAIL"
            line = f"  {mark} {item.name}"
            if not item.passed:
                line += f"：期望 {item.expected}，实际 {item.actual}"
            elif item.detail:
                line += f"（{item.detail}）"
            lines.append(line)
        return "\n".join(lines)


def _close(left: float, right: float) -> bool:
    return abs(left - right) <= TOLERANCE


def expected_pin_total(spec: PackageSpec) -> int:
    """成品里的引脚数（信号脚 + 散热盘）。"""
    return spec.pin_count + (1 if spec.ep_present else 0)


def expected_object_count(spec: PackageSpec, derived: DerivedPackage) -> int:
    """成品里的对象总数。

    1 装配框 + 1 PlaceBound + 2 丝印横杠 + 一脚标记 + 全部引脚 + 2 位号文字。
    WSON8_3X3 算出 16，与 ``verify.log`` 的 ``object_count=16`` 吻合；
    v1 是 30 —— 多出的 14 个正是 DRC 标记对象。
    """
    assembly = 1  # PACKAGE GEOMETRY/ASSEMBLY_TOP 的 polygon
    place_bound = 1  # PACKAGE GEOMETRY/PLACE_BOUND_TOP 的 shape
    silk_bars = 2  # 上下两条丝印横杠
    pin1_marker = 1 if derived.pin1_marker is not None else 0
    pins = expected_pin_total(spec)
    texts = 2  # REF DES 在装配层与丝印层各一个
    return assembly + place_bound + silk_bars + pin1_marker + pins + texts


def judge_verify(
    spec: PackageSpec, derived: DerivedPackage, verify: VerifyLog
) -> list[Assertion]:
    """对 ``verify.log`` 的判据。"""
    checks: list[Assertion] = []

    checks.append(Assertion("verify 收尾完整", verify.complete))
    checks.append(
        Assertion(
            "designType 是 PACKAGE",
            verify.design_type == "PACKAGE",
            expected="PACKAGE",
            actual=str(verify.design_type),
        )
    )
    checks.append(
        Assertion(
            "单位与精度",
            verify.units == spec.units and verify.accuracy == spec.accuracy,
            expected=f"{spec.units}/{spec.accuracy}",
            actual=f"{verify.units}/{verify.accuracy}",
        )
    )
    if verify.extents is None:
        checks.append(Assertion("图纸范围", False, expected=str(spec.extents), actual="未读到"))
    else:
        actual_extents = (
            verify.extents.x0,
            verify.extents.y0,
            verify.extents.x1,
            verify.extents.y1,
        )
        checks.append(
            Assertion(
                "图纸范围",
                all(_close(a, b) for a, b in zip(actual_extents, spec.extents)),
                expected=str(spec.extents),
                actual=str(actual_extents),
            )
        )

    total = expected_pin_total(spec)
    checks.append(
        Assertion(
            "引脚总数", verify.pin_count == total, expected=str(total), actual=str(verify.pin_count)
        )
    )

    wanted = set(spec.pin_numbering) | ({spec.ep_pin_number} if spec.ep_present else set())
    got = {p.number for p in verify.pins}
    checks.append(
        Assertion(
            "引脚号集合",
            got == wanted,
            expected=",".join(sorted(wanted)),
            actual=",".join(sorted(got)),
        )
    )

    drc = verify.drc_objects
    checks.append(
        Assertion(
            "无 DRC 标记对象",
            not drc,
            expected="0 个",
            actual=f"{len(drc)} 个",
            detail="铜箔间距不足时 Allegro 会插入 layer=(DRC ERROR CLASS/TOP) 的对象",
        )
    )

    unexpected = {o.obj_type for o in verify.objects} - ALLOWED_OBJECT_TYPES
    checks.append(
        Assertion(
            "对象类型均在白名单内",
            not unexpected,
            expected=",".join(sorted(ALLOWED_OBJECT_TYPES)),
            actual=",".join(sorted(unexpected)) or "—",
        )
    )

    expected_count = expected_object_count(spec, derived)
    checks.append(
        Assertion(
            "对象总数",
            verify.object_count == expected_count,
            expected=str(expected_count),
            actual=str(verify.object_count),
        )
    )

    missing_layers = sorted(REQUIRED_LAYERS - verify.layers)
    checks.append(
        Assertion(
            "各层对象齐全",
            not missing_layers,
            expected="全部存在",
            actual="缺少 " + ",".join(missing_layers) if missing_layers else "全部存在",
        )
    )

    checks.extend(_judge_pin_geometry(spec, derived, verify))
    return checks


def _judge_pin_geometry(
    spec: PackageSpec, derived: DerivedPackage, verify: VerifyLog
) -> list[Assertion]:
    """逐个引脚比对 bbox：尺寸应等于阻焊尺寸，中心应等于派生坐标。

    ``verify.log`` 的引脚 bbox 量的是**阻焊矩形**（不是铜箔），所以它可以
    反过来验证 padstack 尺寸 —— 这是纯文本唯一能校验焊盘几何的途径。
    """
    checks: list[Assertion] = []
    by_number = {p.number: p for p in verify.pins}

    for pin in derived.pins:
        record = by_number.get(pin.number)
        if record is None or record.bbox is None:
            checks.append(
                Assertion(f"引脚 {pin.number} 的 bbox", False, expected="有", actual="缺失")
            )
            continue
        center = record.bbox.center
        size_ok = _close(record.bbox.width, derived.land_pad.mask[0]) and _close(
            record.bbox.height, derived.land_pad.mask[1]
        )
        center_ok = _close(center[0], pin.x) and _close(center[1], pin.y)
        checks.append(
            Assertion(
                f"引脚 {pin.number} 的 bbox",
                size_ok and center_ok,
                expected=f"{derived.land_pad.mask[0]}×{derived.land_pad.mask[1]} @ ({pin.x}, {pin.y})",
                actual=f"{record.bbox.width}×{record.bbox.height} @ {center}",
            )
        )

    if spec.ep_present and derived.ep_pad is not None:
        record = by_number.get(spec.ep_pin_number)
        if record is None or record.bbox is None:
            checks.append(
                Assertion("散热盘的 bbox", False, expected="有", actual="缺失")
            )
        else:
            size_ok = _close(record.bbox.width, derived.ep_pad.mask[0]) and _close(
                record.bbox.height, derived.ep_pad.mask[1]
            )
            checks.append(
                Assertion(
                    "散热盘的 bbox",
                    size_ok
                    and _close(record.bbox.center[0], 0.0)
                    and _close(record.bbox.center[1], 0.0),
                    expected=f"{derived.ep_pad.mask[0]}×{derived.ep_pad.mask[1]} @ (0, 0)",
                    actual=f"{record.bbox.width}×{record.bbox.height} @ {record.bbox.center}",
                )
            )
    return checks


def judge_props(spec: PackageSpec, props: PropsLog) -> list[Assertion]:
    """对 ``props.log`` 的判据：``PACKAGE_HEIGHT_MAX`` 是否熬过存盘-重开。"""
    checks: list[Assertion] = [Assertion("props 收尾完整", props.complete)]

    entries = {name: value for record in props.properties for name, value in record.entries.items()}
    upper = {name.upper(): value.upper() for name, value in entries.items()}
    expected_value = spec.height_property_value.upper()

    checks.append(
        Assertion(
            "PACKAGE_HEIGHT_MAX 存活",
            upper.get("PACKAGE_HEIGHT_MAX") == expected_value,
            expected=expected_value,
            actual=upper.get("PACKAGE_HEIGHT_MAX", "缺失"),
            detail="值会被 Allegro 大写归一，比较前须统一",
        )
    )

    owners = props.with_properties
    checks.append(
        Assertion(
            "带属性的对象恰好一个",
            len(owners) == 1,
            expected="1 个",
            actual=f"{len(owners)} 个",
        )
    )
    if len(owners) == 1:
        checks.append(
            Assertion(
                "属性挂在 Place Bound 上",
                owners[0].layer == PLACE_BOUND_LAYER,
                expected=PLACE_BOUND_LAYER,
                actual=str(owners[0].layer),
            )
        )
    return checks


def judge_build(
    spec: PackageSpec,
    derived: DerivedPackage,
    build: BuildLog,
    symbol_log: str = "",
) -> list[Assertion]:
    """对 ``build.log`` 与 ``<符号名>.log`` 的判据。"""
    checks: list[Assertion] = [Assertion("build 收尾完整", build.complete)]

    checks.append(
        Assertion(
            "设计名",
            build.design_name == spec.name,
            expected=spec.name,
            actual=build.design_name,
        )
    )

    # 只信副作用：不看函数的返回值，看 toDisk 是否真的执行了。
    for padstack in (spec.land_padstack_name, spec.ep_padstack_name):
        if padstack == spec.ep_padstack_name and not spec.ep_present:
            continue
        record = build.padstacks.get(padstack, {})
        checks.append(
            Assertion(
                f"焊盘 {padstack} 已落盘",
                record.get("toDisk") == "(t)",
                expected="toDisk=(t)",
                actual=record.get("toDisk", "缺失"),
                detail="bMakePadstack 的返回值恒为 nil，不可作为判据",
            )
        )

    expected_pins = expected_pin_total(spec)
    checks.append(
        Assertion(
            "实际摆放的引脚数",
            len(build.placed_pins) == expected_pins,
            expected=str(expected_pins),
            actual=str(len(build.placed_pins)),
        )
    )

    checks.append(
        Assertion(
            "已保存 .dra",
            build.saved_dra == spec.name,
            expected=spec.name,
            actual=str(build.saved_dra),
        )
    )

    # Allegro 会把 .psm 的文件名转小写。
    compiled = (build.compiled_psm or "").lower()
    checks.append(
        Assertion(
            "已编译 .psm",
            compiled.endswith(".psm") and spec.name.lower() in compiled,
            expected=f"{spec.name.lower()}.psm",
            actual=str(build.compiled_psm),
        )
    )

    build_drc = build.drc_objects
    checks.append(
        Assertion(
            "构建期无 DRC 标记",
            not build_drc,
            expected="0 个",
            actual=f"{len(build_drc)} 个",
        )
    )

    checks.append(
        Assertion(
            "编译报告无 DRC 警告",
            not has_drc_warning(symbol_log),
            expected="无 SPMHA1-301",
            actual="有 SPMHA1-301" if has_drc_warning(symbol_log) else "无",
            detail=f"警告写在 <{spec.name.lower()}>.log，不在 build.log",
        )
    )
    return checks


def judge(
    spec: PackageSpec,
    derived: DerivedPackage,
    *,
    build: BuildLog | None = None,
    verify: VerifyLog | None = None,
    props: PropsLog | None = None,
    symbol_log: str = "",
) -> Verdict:
    """汇总全部判据。

    缺哪一份日志就跳过哪一组 —— 这样离线用现成日志回归时，可以只喂
    ``verify.log`` 而不必凑齐四份。
    """
    checks: list[Assertion] = []
    if build is not None:
        checks.extend(judge_build(spec, derived, build, symbol_log))
    if verify is not None:
        checks.extend(judge_verify(spec, derived, verify))
    if props is not None:
        checks.extend(judge_props(spec, props))
    return Verdict(spec_name=spec.name, assertions=checks)
