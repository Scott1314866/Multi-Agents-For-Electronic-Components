"""驱动 Allegro 无头执行 SKILL 脚本。

SKILL 不能独立运行 —— 必须由 ``allegro.exe`` 以 ``-s <script>.scr`` 拉起。
三条运行约束，全部有源程序的证据：

1. **cwd 决定加载哪个 `.il`** —— Allegro 只读启动目录下的 ``allegro.ilinit``，
   所以每次跑必须固定 cwd 并把 ilinit 指向本次要用的脚本；
2. **无头模式必须 ``-sq``**，且 SKILL 侧要 ``?noConfirm t``，否则卡在保存确认框
   （GUI 会话日志 ``allegro.jrl`` 里留下了那条 ``Do you want to save…`` 的反面证据）；
3. **必须 ``skill (axlOSExit 0)``** 收尾，否则进程挂着不退出。

本机没有 Cadence（``C:\\Cadence`` 不存在、无 ``CDS*`` 环境变量），所以可执行
文件是**参数**：有环境的机器传入即可，没有则明确抛 ``FileNotFoundError``，
而不是让子进程去静默失败。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend.agents.pcb.skill_emitter import emit_allegro_ilinit

#: ``allegro.exe`` 的常规安装位置（SPB 17.2）。仅作兜底探测，不保证存在。
DEFAULT_ALLEGRO_EXE = Path(r"C:\Cadence\SPB_17.2\tools\bin\allegro.exe")

#: 产品开关。源程序 journal 里记的是 ``Allegro_Enterprise_PCB_Designe``
#: （末位疑似被 Allegro 自己的日志截断），这里补全。
DEFAULT_PRODUCT = "Allegro_Enterprise_PCB_Designer"

#: 环境变量名，供部署时覆盖可执行文件位置。
EXE_ENV_VAR = "ALLEGRO_EXE"

#: 各阶段对应的脚本与日志。
STAGES: dict[str, tuple[str, str, str]] = {
    # stage: (要加载的 .il 前缀, 剧本名, 日志名)
    "build": ("build", "build.scr", "build.log"),
    "verify": ("verify", "verify.scr", "verify.log"),
    "props": ("props", "props.scr", "props.log"),
}


@dataclass(frozen=True)
class AllegroRun:
    """一次 Allegro 执行的完整结果。"""

    stage: str
    returncode: int
    stdout: str
    log_path: Path
    log_text: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def resolve_allegro_exe(exe: str | Path | None = None) -> Path:
    """定位 ``allegro.exe``。

    优先级：显式传入 → 环境变量 ``ALLEGRO_EXE`` → 常规安装路径。

    Raises:
        FileNotFoundError: 三处都找不到。这是**环境缺失**而不是任务失败，
            调用方应据此把任务标记为"等待环境"而非"生成失败"。
    """
    candidates: list[Path] = []
    if exe is not None:
        candidates.append(Path(exe))
    env = os.environ.get(EXE_ENV_VAR)
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_ALLEGRO_EXE)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "找不到 allegro.exe。请在有 Cadence 的机器上运行，或设置环境变量 "
        f"{EXE_ENV_VAR} 指向可执行文件。已尝试：{', '.join(str(c) for c in candidates)}"
    )


def reset_log(work_dir: Path, log_name: str) -> None:
    """删除上一轮的同名日志。

    SKILL 侧用 ``outfile(..., "a")`` 追加写（这是刻意的：硬崩也不丢证据），
    所以重跑前必须清掉旧日志，否则新旧证据混在一起，判据会读到上一轮的数据。
    """
    path = Path(work_dir) / log_name
    if path.exists():
        path.unlink()


def point_ilinit(work_dir: Path, il_path: Path) -> None:
    """把 ``allegro.ilinit`` 指向本轮要用的 SKILL 文件。"""
    (Path(work_dir) / "allegro.ilinit").write_text(
        emit_allegro_ilinit(il_path), encoding="utf-8"
    )


def run_stage(
    work_dir: Path,
    spec_name: str,
    *,
    stage: str,
    exe: str | Path | None = None,
    product: str = DEFAULT_PRODUCT,
    design: str | None = None,
    timeout: float = 600.0,
) -> AllegroRun:
    """执行一个阶段（build / verify / props）。

    Args:
        work_dir: 工作目录；同时是 cwd、``padpath``/``psmpath`` 与 ilinit 所在处。
        spec_name: 封装名，用于拼 ``<name>.dra`` 与 ``<name.lower()>.il``。
        stage: ``build`` / ``verify`` / ``props``。
        exe: ``allegro.exe`` 路径；留空则自动探测。
        product: ``-product`` 开关的值。
        design: 要打开的 ``.dra``；默认按阶段取（build 用模板，其余用成品）。
        timeout: 子进程超时（秒）。

    Returns:
        含 returncode、stdout 与日志全文的结果。

    Raises:
        ValueError: 未知的 ``stage``。
        FileNotFoundError: 找不到 ``allegro.exe``。
        subprocess.TimeoutExpired: 超时（Allegro 无头卡住时最常见的原因
            是 SKILL 侧漏了 ``?noConfirm t``）。
    """
    if stage not in STAGES:
        raise ValueError(f"未知阶段：{stage}（可用：{', '.join(STAGES)}）")

    il_prefix, script_name, log_name = STAGES[stage]
    work_dir = Path(work_dir)
    exe_path = resolve_allegro_exe(exe)

    il_path = work_dir / f"{il_prefix}_{spec_name.lower()}.il"
    if not il_path.is_file():
        raise FileNotFoundError(f"缺少 {stage} 阶段的 SKILL 脚本：{il_path}")

    reset_log(work_dir, log_name)
    point_ilinit(work_dir, il_path)

    design_name = design or f"{spec_name}.dra"
    command = [
        str(exe_path),
        "-sq",
        "-product",
        product,
        "-s",
        script_name,
        design_name,
    ]

    completed = subprocess.run(
        command,
        cwd=str(work_dir),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    log_path = work_dir / log_name
    log_text = (
        log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    )

    return AllegroRun(
        stage=stage,
        returncode=completed.returncode,
        stdout=stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else ""),
        log_path=log_path,
        log_text=log_text,
    )


def build_command(
    exe: str | Path,
    work_dir: Path,
    spec_name: str,
    *,
    stage: str,
    product: str = DEFAULT_PRODUCT,
) -> list[str]:
    """只拼命令行，不执行。

    本机没有 Cadence 时，这是唯一能验证的部分 —— 也是让部署方核对
    ``-product`` 取值是否正确的方式。
    """
    if stage not in STAGES:
        raise ValueError(f"未知阶段：{stage}")
    _, script_name, _ = STAGES[stage]
    return [
        str(exe),
        "-sq",
        "-product",
        product,
        "-s",
        script_name,
        f"{spec_name}.dra",
    ]
