"""PCB 封装生成 Agent 的状态定义。

与其他 Agent 一致的约定：``TypedDict, total=False``，每个节点只返回自己
写出的那一小块增量，未涉及的键不出现；列表字段靠节点里显式拼接，
不用 reducer。

唯一必填输入是 ``spec`` —— 一个 :class:`~backend.agents.pcb.spec.PackageSpec`
的 dict 形式。其余全部由工作流产出。
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class PcbPackageState(TypedDict, total=False):
    """参数化 PCB 封装生成工作流的状态。"""

    task: str
    mode: str
    # 唯一必填输入
    spec: dict[str, Any]
    # 输出目录；留空则用临时目录（源程序的 OUTPUT_DIR 是用户正式库，绝不自动写）
    output_dir: str

    # ── 人工回答（interrupt/resume 收下，与自动派生结果分开存）────
    human_spec_confirmation: dict[str, Any]
    human_review: dict[str, Any]
    human_history: list[dict[str, Any]]
    needs_review: bool

    # ── 校验与派生 ──────────────────────────────────────────────
    #: 铜箔间距等硬校验的问题清单；非空即停在 ask_spec_confirmation
    clearance_issues: list[str]
    warnings: list[str]
    derived_summary: str

    # ── 生成装置 ────────────────────────────────────────────────
    work_dir: str
    generated_files: dict[str, str]

    # ── Allegro 运行参数（可选覆盖；留空则自动探测）──────────────
    #: allegro.exe 的路径；本机通常没有，由有 Cadence 的机器传入或设
    #: 环境变量 ALLEGRO_EXE。
    allegro_exe: str
    allegro_timeout: float

    # ── 三阶段执行结果（只有执行模式下才有）────────────────────
    build_run: dict[str, Any]
    verify_run: dict[str, Any]
    props_run: dict[str, Any]

    # ── 判据与产物 ──────────────────────────────────────────────
    verdict: dict[str, Any]
    artifact_paths: dict[str, str]

    # ── 内部输出设置，不属于必填接口 ─────────────────────────────
    result: dict[str, Any]
    status: str
    errors: list[str]
