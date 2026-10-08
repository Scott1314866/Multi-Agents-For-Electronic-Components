"""布局 → OrCAD Capture 的 TCL 脚本，并驱动 tclsh 执行。

对应源程序的 ``app/capture.py``（密文）。TCL 结构由
``spike/c_out4/ADS1115_gen.tcl``（181 行）与
``spike/s_stm32_web/702bb3679978/STM32F105VCT6_gen.tcl``（990 行）**逐段还原** ——
五份生成物完全同构，差别只在坐标与输出目录，证明模板是纯字符串填充。

三条反直觉的坑，都有实证（Module1-Spec §10.1/§10.2）：

1. **矩形体不能用 ``NewBox``** —— 它在目标环境恒返回 ``statusFailed=1``，
   必须用 4 条 ``NewLine`` 拼（与 Cadence 自家 ``PwlANDSymbolCreate.tcl`` 同法）。
2. **``NewPlacedInst`` 必须传 ``DboDevice``** —— 只传 ``DboLibPart`` 会一直报
   ``No matching function for overloaded``。名字里的 ``$cName`` 被复用作
   包名，这是源程序的写法。
3. **``SaveDesignAs`` 是五参数形式** —— ``self design <int> <int> <CString 路径> <int>``，
   不是看起来的三参数。

引脚类型恒为 ``4``（Passive），且 ``Shape=Short`` **不是**靠 ``SetIsLong``
表达的，而是靠「引脚长 100 mil + ``SetIsLong 0``」—— 这条最反直觉，是踩坑换来的。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import re

from backend.agents.symbol.config import DBO_DLL, resolve_tclsh
from backend.agents.symbol.contracts.layout import SymbolLayout

#: 引脚类型码：4 = Passive（五份生成物跨越 IN/OUT/IO/PAS 四类，一律是 4）。
PIN_TYPE_PASSIVE = 4

#: 成功判据：stdout 里出现这一行。
PASS_MARKER = "===== CAPTURE RESULT: PASS ====="

#: tclsh 缺省超时（秒）。
DEFAULT_TIMEOUT = 300.0

_HEADER = """# 由 backend/agents/symbol/tools/capture.py 生成 —— 请勿手改
proc step {msg} { puts ""; puts "\\[STEP\\] $msg"; flush stdout }
proc ok   {msg} { puts "   \\[OK\\] $msg"; flush stdout }
proc die  {msg} { puts "   \\[FAIL\\] $msg"; flush stdout; exit 1 }
"""


@dataclass
class CaptureResult:
    """一次 Capture 生成的结果。"""

    #: tclsh 的完整输出（失败时是唯一的排查线索）。
    log: str
    #: stdout 里是否出现 PASS 标记且退出码为 0。
    passed: bool
    olb: Path
    dsn: Path
    #: 生成的 ``<part>_gen.tcl``。
    script: Path


class CaptureToolchainUnavailable(FileNotFoundError):
    """Execution is unavailable, but a portable script has been written."""

    def __init__(self, message: str, script: Path):
        super().__init__(message)
        self.script = script


def _tcl_string(value: str) -> str:
    """Escape literal text embedded in a Tcl double-quoted argument."""
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("$", "\\$").replace("[", "\\[").replace("]", "\\]")
            .replace("{", "\\{").replace("}", "\\}")
            .replace("\n", "\\n").replace("\r", "\\r"))


def _validate_part_name(part: str) -> None:
    if (not part or part in {".", ".."} or part.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', part)
            or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
        raise ValueError("型号不能用作产物文件名：请去除路径分隔符或非法文件名字符")


def render_tcl(layout: SymbolLayout, out_dir: Path) -> str:
    """把布局渲染成 TCL 脚本文本（纯函数，可对拍）。"""
    _validate_part_name(layout.part_name)
    part = _tcl_string(layout.part_name)
    half_w = layout.body_width // 2
    half_h = layout.half_height
    posix_dir = _tcl_string(Path(out_dir).as_posix())

    lines: list[str] = [_HEADER]
    lines.append(f'set outDir  "{posix_dir}"')
    lines.append(f'set libPath "$outDir/{part}.OLB"')
    lines.append(f'set dsnPath "$outDir/{part}.DSN"')
    lines.append("file mkdir $outDir")
    lines.append("")
    lines.append('step "load DBO DLL"')
    lines.append(
        f'if {{[catch {{load {DBO_DLL} DboTclWriteBasic}} err]}} {{ die "load: $err" }}'
    )
    lines.append("set mSession [DboTclHelper_sCreateSession]")
    lines.append("")
    lines.append('step "create .olb + symbol"')
    lines.append("set st [DboState]")
    lines.append("set mLib [$mSession CreateLib [DboTclHelper_sMakeCString $libPath] $st]")
    lines.append('if {[$st Failed]} { die "CreateLib" }')
    lines.append("")
    lines.append(f'set cName [DboTclHelper_sMakeCString "{part}"]')
    lines.append("set mPkg  [$mLib NewPackage $cName $st]")
    lines.append("set mCell [$mLib NewCell    $cName $st]")
    lines.append(f'set mPart [$mLib NewPart    [DboTclHelper_sMakeCString "{part}.Normal"] $st]')
    lines.append('if {[$st Failed]} { die "NewPackage/NewCell/NewPart" }')
    lines.append("$mCell AddPart $mPart")
    lines.append('$mPkg SetReferenceTemplate [DboTclHelper_sMakeCString "U"]')
    lines.append(
        f"$mPart SetBoundingBox [DboTclHelper_sMakeCRect "
        f"{-half_w} {half_h} {half_w} {-half_h}]"
    )
    lines.append("")

    # 矩形体：必须 4 条 NewLine（NewBox 在此环境恒失败）
    lines.append('step "draw body"')
    lines.append("foreach c {")
    corners = [
        (-half_w, half_h, half_w, half_h),
        (half_w, half_h, half_w, -half_h),
        (half_w, -half_h, -half_w, -half_h),
        (-half_w, -half_h, -half_w, half_h),
    ]
    for x1, y1, x2, y2 in corners:
        lines.append(f"    {{{x1} {y1} {x2} {y2}}}")
    lines.append("} {")
    lines.append("    lassign $c x1 y1 x2 y2")
    lines.append("    set s [DboState]")
    lines.append(
        "    $mPart NewLine $s [DboTclHelper_sMakeCPoint $x1 $y1] "
        "[DboTclHelper_sMakeCPoint $x2 $y2] 0 0"
    )
    lines.append('    if {[$s Failed]} { die "NewLine" }')
    lines.append("}")
    lines.append("")
    lines.append(
        'set mDevice [$mPkg NewDevice [DboTclHelper_sMakeCString "U"] 0 $mCell $st]'
    )
    lines.append('if {[$st Failed]} { die "NewDevice" }')
    lines.append("")

    lines.append(f'step "place {len(layout.pins)} pins"')
    for index, pin in enumerate(layout.pins):
        lines.append("set s [DboState]")
        lines.append(
            f'set mPin [$mPart NewSymbolPinScalar $s [DboTclHelper_sMakeCString "{_tcl_string(pin.name)}"] \\'
        )
        lines.append(f"              {PIN_TYPE_PASSIVE} \\")
        lines.append(
            f"              [DboTclHelper_sMakeCPoint {pin.x} {pin.y}] \\"
        )
        lines.append(
            f"              [DboTclHelper_sMakeCPoint {pin.hx} {pin.hy}] 1 {index}]"
        )
        lines.append(f'if {{[$s Failed]}} {{ die "NewSymbolPinScalar({_tcl_string(pin.number)})" }}')
        lines.append("$mPin SetIsLong 0")
        lines.append("$mPin SetIsNumberVisible 1")
        lines.append(
            f'$mDevice NewPinNumber [DboTclHelper_sMakeCString "{_tcl_string(pin.number)}"] '
            f"[DboTclHelper_sMakeInt {index}]"
        )
    lines.append("$mLib SavePackageAll $mPkg")
    lines.append("$mSession SaveLib $mLib")
    lines.append(f'ok "{len(layout.pins)} pins, olb saved ($libPath)"')
    lines.append("")

    # .dsn：建原理图并落一个实例
    lines.append('step "create .dsn and place the symbol"')
    lines.append("set st [DboState]")
    lines.append("set mDesign [$mSession CreateDesign $st \\")
    lines.append(f'    [DboTclHelper_sMakeCString "{part}"] [DboTclHelper_sMakeCString "{part}"]]')
    lines.append('if {[$st Failed]} { die "CreateDesign" }')
    lines.append("")
    lines.append("set st [DboState]")
    lines.append("set mRoot [$mDesign GetRootSchematic $st]")
    lines.append("set st [DboState]")
    lines.append('set mPage [$mRoot NewPage $st [DboTclHelper_sMakeCString "PAGE1"] 1]')
    lines.append('if {[$st Failed]} { die "NewPage" }')
    lines.append("")
    lines.append("set st [DboState]")
    lines.append("set pIt [$mLib NewPartsIter $st]")
    lines.append("set st [DboState]")
    lines.append("set mPartRef [$pIt NextPart $st]")
    lines.append("set st [DboState]")
    lines.append(f'set mPkgRef [$mLib GetPackage [DboTclHelper_sMakeCString "{part}"] $st]')
    lines.append("set st [DboState]")
    lines.append("set dIt [$mPkgRef NewDevicesIter $st]")
    lines.append("set st [DboState]")
    lines.append("set mDevRef [$dIt NextDevice $st]")
    lines.append(
        'if {$mPartRef eq "NULL" || $mDevRef eq "NULL"} '
        '{ die "cannot fetch part/device from lib" }'
    )
    lines.append("")
    lines.append("set s [DboState]")
    lines.append("set r [$mPage NewPlacedInst $s \\")
    lines.append('    [DboTclHelper_sMakeCString "U1"] $cName $mPartRef $mDevRef \\')
    lines.append("    [DboTclHelper_sMakeCPoint 300 400]]")
    lines.append('if {[$s Failed]} { die "NewPlacedInst" }')
    lines.append("")
    lines.append('step "save .dsn"')
    lines.append("set st [DboState]")
    lines.append("$mSession SaveDesignAs $mDesign 2 0 [DboTclHelper_sMakeCString $dsnPath] 1")
    lines.append('if {[$st Failed]} { die "SaveDesignAs" }')
    lines.append("if {![file exists $dsnPath]} { die \"dsn not created\" }")
    lines.append("")
    lines.append('puts ""')
    lines.append(f'puts "{PASS_MARKER}"')
    lines.append('puts "  olb : $libPath"')
    lines.append('puts "  dsn : $dsnPath"')
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def generate(
    layout: SymbolLayout,
    *,
    out_dir: Path,
    tclsh: str | Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CaptureResult:
    """渲染 TCL 并执行，产出 ``.OLB`` 与 ``.DSN``。

    Args:
        layout: 已算好的符号布局。
        out_dir: 输出目录；同时是 TCL 里的 ``$outDir``。
        tclsh: ``tclsh.exe`` 路径；留空则自动探测。
        timeout: 子进程超时（秒）。

    Returns:
        ``CaptureResult``。``passed`` 要求 stdout 出现 PASS 标记 **且** 退出码为 0。

    Raises:
        FileNotFoundError: 找不到 ``tclsh.exe``（环境缺失，不是生成失败）。
        subprocess.TimeoutExpired: 超时。源程序的 ``web/service.py`` 把这两种
            异常单独接住并降级成"生成失败"，调用方应保持同样的区分。
    """
    _validate_part_name(layout.part_name)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    script = out_dir / f"{layout.part_name}_gen.tcl"
    script.write_text(render_tcl(layout, out_dir), encoding="utf-8")
    try:
        exe = resolve_tclsh(tclsh)
    except FileNotFoundError as exc:
        raise CaptureToolchainUnavailable(str(exc), script) from exc

    completed = subprocess.run(
        [str(exe), str(script)],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    log = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    if stderr.strip():
        log = f"{log}\n[stderr]\n{stderr}"

    olb = out_dir / f"{layout.part_name}.OLB"
    dsn = out_dir / f"{layout.part_name}.DSN"
    passed = (
        completed.returncode == 0 and PASS_MARKER in log
        and all(path.is_file() and path.stat().st_size > 0 for path in (olb, dsn))
    )

    return CaptureResult(log=log, passed=passed, olb=olb, dsn=dsn, script=script)
