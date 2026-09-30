"""PCB 封装工作流的主节点与路由。

链路（全确定性，不调用模型）：

    load_spec → validate_spec → [confirm_spec] → emit_skills
      → run_build → run_verify → run_props → judge → [review_result]

每个节点只返回自己写出的增量；错误不抛给框架，而是转成
``{"errors": [...], "status": "failed"}`` 交给路由。

三种终点泾渭分明，绝不互相冒充：

* ``failed`` —— 技术失败（读写异常、子进程崩了）；
* ``stopped_invalid_spec`` —— 参数不合规（铜箔间距不足等），**不下发到 Allegro**；
* ``stopped_no_toolchain`` —— 本机没有 ``allegro.exe``，SKILL 已生成好但没执行。

后两者是**预期内的安全终点**，不是失败：SKILL 文件已经留在 ``work_dir``，
换一台有 Cadence 的机器就能接着跑。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from backend.agents.pcb.assertions import judge
from backend.agents.pcb.derive import check_clearances, derive_package, summarize
from backend.agents.pcb.parser import (
    parse_build_log,
    parse_props_log,
    parse_verify_log,
    read_logs,
)
from backend.agents.pcb.runner import AllegroRun, run_stage
from backend.agents.pcb.skill_emitter import check_layers, write_skill_package
from backend.agents.pcb.spec import PackageSpec
from backend.agents.pcb.workers import cad_node
from backend.core.logger import get_logger

logger = get_logger(__name__)

#: 单次 Allegro 执行的超时。无头模式卡住时最常见的成因是 SKILL 侧漏了
#: ``?noConfirm t``（会弹保存确认框）。
DEFAULT_TIMEOUT = 600.0


def _errors(state: dict[str, Any], message: str) -> list[str]:
    """追加一条节点错误。"""
    return [*state.get("errors", []), message]


def _spec_of(state: dict[str, Any]) -> PackageSpec:
    return PackageSpec.model_validate(state["spec"])


def _work_dir_of(state: dict[str, Any]) -> Path:
    return Path(state["work_dir"])


# ── 加载与校验 ────────────────────────────────────────────────────


async def load_spec_node(state: dict[str, Any]) -> dict[str, Any]:
    """把输入 dict 构造并校验成 :class:`PackageSpec`，并派生几何。

    唯一必填输入是 ``spec``。``work_dir`` 未给则在系统临时目录开一个 ——
    源程序的 ``OUTPUT_DIR`` 是用户正式库，agent 绝不自动写进去。
    """
    try:
        raw = state.get("spec") or {}
        if not isinstance(raw, dict) or not raw:
            raise ValueError(
                "缺少 spec：至少需要 name、body_x/body_y、pin_* 与 land_* 等字段"
            )
        spec = PackageSpec.model_validate(raw)
        derived = derive_package(spec)

        work_dir = Path(state.get("work_dir") or tempfile.mkdtemp(prefix="pcb-package-"))
        work_dir.mkdir(parents=True, exist_ok=True)

        return {
            "spec": spec.model_dump(),
            "work_dir": str(work_dir),
            "derived_summary": summarize(spec, derived),
            "warnings": list(derived.warnings),
            "status": "spec_loaded",
        }
    except Exception as exc:
        logger.warning("pcb.load_spec.failed", error=str(exc))
        return {"errors": _errors(state, f"参数加载失败：{exc}"), "status": "failed"}


async def validate_spec_node(state: dict[str, Any]) -> dict[str, Any]:
    """生成前的硬校验：铜箔间距与层名白名单。

    间距不足时 **不下发**给 Allegro —— 让它跑到编译期才报 ``SPMHA1-301``
    的代价高得多（要起进程、开图纸、编译，还要人去翻 ``<符号名>.log``）。
    """
    try:
        spec = _spec_of(state)
        issues = check_clearances(spec)
        layer_issues = check_layers()
        if layer_issues:
            issues.extend(f"使用了白名单外的层名：{name}" for name in layer_issues)
        return {
            "clearance_issues": issues,
            "status": "spec_invalid" if issues else "spec_valid",
        }
    except Exception as exc:
        logger.warning("pcb.validate_spec.failed", error=str(exc))
        return {"errors": _errors(state, f"参数校验失败：{exc}"), "status": "failed"}


# ── 生成装置 ──────────────────────────────────────────────────────


async def emit_skills_node(state: dict[str, Any]) -> dict[str, Any]:
    """渲染 SKILL 脚本、``.scr`` 剧本与 ``allegro.ilinit``。

    ``allegro.ilinit`` 与 ``*.scr`` 必须成套写 —— Allegro 只加载启动 cwd 里的
    ilinit，两者不同步就白跑（源程序靠手工替换，这里一次写齐）。
    """
    try:
        spec = _spec_of(state)
        derived = derive_package(spec)
        written = write_skill_package(spec, derived, _work_dir_of(state))
        logger.info(
            "pcb.skills_emitted",
            spec_name=spec.name,
            work_dir=str(_work_dir_of(state)),
            files=len(written),
        )
        return {
            "generated_files": {key: str(path) for key, path in written.items()},
            "status": "skills_emitted",
        }
    except Exception as exc:
        logger.error("pcb.emit_skills.failed", exc_info=True)
        return {"errors": _errors(state, f"SKILL 生成失败：{exc}"), "status": "failed"}


# ── 三阶段执行（原生子进程，走专属线程）──────────────────────────


@cad_node
def _execute_stage(
    work_dir: str, spec_name: str, stage: str, exe: str | None, timeout: float
) -> AllegroRun:
    return run_stage(Path(work_dir), spec_name, stage=stage, exe=exe, timeout=timeout)


def _run_to_dict(run: AllegroRun) -> dict[str, Any]:
    return {
        "stage": run.stage,
        "returncode": run.returncode,
        "ok": run.ok,
        "log_path": str(run.log_path),
        "stdout_tail": run.stdout[-2000:],
    }


async def _execute_and_capture(
    state: dict[str, Any], stage: str
) -> tuple[dict[str, Any], str]:
    """跑一个阶段，返回（增量 dict, 路由状态）。

    只有 ``stage`` 不同，三个阶段共用这一套异常映射：

    * 找不到 ``allegro.exe`` → ``stopped_no_toolchain``（环境缺失，非任务失败）
    * 超时 → ``failed``（多半是 SKILL 侧漏了 ``?noConfirm t``）
    * 其他异常 → ``failed``
    """
    spec_name = state["spec"]["name"]
    try:
        run = await _execute_stage(
            state["work_dir"],
            spec_name,
            stage,
            state.get("allegro_exe"),
            float(state.get("allegro_timeout") or DEFAULT_TIMEOUT),
        )
    except FileNotFoundError as exc:
        logger.warning("pcb.toolchain_missing", stage=stage, error=str(exc))
        return (
            {
                "errors": _errors(state, str(exc)),
                "status": "stopped_no_toolchain",
                "result": {
                    "status": "stopped_no_toolchain",
                    "stage": stage,
                    "reason": str(exc),
                    "generated_files": state.get("generated_files", {}),
                },
            },
            "stopped_no_toolchain",
        )
    except TimeoutError as exc:
        return (
            {"errors": _errors(state, f"{stage} 超时：{exc}"), "status": "failed"},
            "failed",
        )
    except Exception as exc:
        logger.error("pcb.stage_exception", stage=stage, exc_info=True)
        return (
            {"errors": _errors(state, f"{stage} 执行异常：{exc}"), "status": "failed"},
            "failed",
        )

    key = {"build": "build_run", "verify": "verify_run", "props": "props_run"}[stage]
    if not run.ok:
        return (
            {
                key: _run_to_dict(run),
                "errors": _errors(state, f"{stage} 退出码 {run.returncode}"),
                "status": "failed",
            },
            "failed",
        )
    return (
        {key: _run_to_dict(run), "status": f"{stage}_done"},
        "continue",
    )


async def run_build_node(state: dict[str, Any]) -> dict[str, Any]:
    """阶段一：建焊盘、摆引脚、画各层、存 ``.dra``、编译 ``.psm``。"""
    payload, _ = await _execute_and_capture(state, "build")
    return payload


async def run_verify_node(state: dict[str, Any]) -> dict[str, Any]:
    """阶段二：重新打开成品回读，产出 ``verify.log``（几何的权威来源）。"""
    payload, _ = await _execute_and_capture(state, "verify")
    return payload


async def run_props_node(state: dict[str, Any]) -> dict[str, Any]:
    """阶段三：确认 ``PACKAGE_HEIGHT_MAX`` 熬过存盘-重开。"""
    payload, _ = await _execute_and_capture(state, "props")
    return payload


# ── 判据 ─────────────────────────────────────────────────────────


async def judge_node(state: dict[str, Any]) -> dict[str, Any]:
    """读取四份日志，逐条比对。

    缺哪一份日志就跳过哪一组判据 —— 这样离线用现成日志回归时不必凑齐。
    """
    try:
        spec = _spec_of(state)
        derived = derive_package(spec)
        work_dir = _work_dir_of(state)
        logs = read_logs(work_dir, spec.name)

        verdict = judge(
            spec,
            derived,
            build=parse_build_log(logs["build"]) if state.get("build_run") else None,
            verify=parse_verify_log(logs["verify"]) if state.get("verify_run") else None,
            props=parse_props_log(logs["props"]) if state.get("props_run") else None,
            symbol_log=logs["symbol"],
        )
        logger.info(
            "pcb.judged",
            spec_name=spec.name,
            passed=verdict.passed,
            failures=len(verdict.failures),
        )
        return {
            "verdict": verdict.to_dict(),
            "status": "judged" if verdict.passed else "verdict_failed",
        }
    except Exception as exc:
        logger.error("pcb.judge.failed", exc_info=True)
        return {"errors": _errors(state, f"判据执行失败：{exc}"), "status": "failed"}


# ── 终点 ─────────────────────────────────────────────────────────


def _collect_artifacts(state: dict[str, Any]) -> dict[str, str]:
    """收拢产物路径：SKILL 装置永远有，Allegro 产物只在跑过执行时才有。"""
    artifacts = dict(state.get("generated_files", {}))
    work_dir = state.get("work_dir")
    spec_name = (state.get("spec") or {}).get("name", "")
    if work_dir and spec_name:
        root = Path(work_dir)
        for label, path in (
            ("dra", root / f"{spec_name}.dra"),
            ("psm", root / f"{spec_name.lower()}.psm"),
            ("land_pad", root / f"{spec_name.lower()}_land.pad"),
            ("ep_pad", root / f"{spec_name.lower()}_ep.pad"),
        ):
            if path.is_file():
                artifacts[label] = str(path)
    return artifacts


async def emit_only_done_node(state: dict[str, Any]) -> dict[str, Any]:
    """``emit_only`` 模式的终点：只产装置，不下发 Allegro。"""
    return {
        "needs_review": True,
        "artifact_paths": _collect_artifacts(state),
        "status": "skills_ready",
        "result": {
            "status": "skills_ready",
            "spec_name": state["spec"]["name"],
            "summary": state.get("derived_summary", ""),
            "generated_files": state.get("generated_files", {}),
            "note": "仅生成 SKILL 与运行装置，未执行 Allegro（emit_only 模式）",
        },
    }


async def finalize_node(state: dict[str, Any]) -> dict[str, Any]:
    """人工审核之后的收尾：补齐产物清单。

    复核节点已经写过 ``result``（含操作员的意见），这里**合并**而不是覆盖 ——
    否则审核意见会在最后一步悄悄丢掉。
    """
    artifacts = _collect_artifacts(state)
    status = state.get("status") or "completed"
    result = dict(state.get("result") or {})
    result.update(
        {
            "status": status,
            "spec_name": state["spec"]["name"],
            "summary": state.get("derived_summary", ""),
            "verdict": state.get("verdict", {}),
            "artifacts": artifacts,
        }
    )
    return {"artifact_paths": artifacts, "status": status, "result": result}


async def stopped_invalid_spec_node(state: dict[str, Any]) -> dict[str, Any]:
    """参数不合规的安全终点。SKILL 不生成，问题原样交出。"""
    issues = list(state.get("clearance_issues", []))
    logger.warning("pcb.stopped_invalid_spec", spec_name=state["spec"]["name"], issues=issues)
    return {
        "needs_review": True,
        "status": "stopped_invalid_spec",
        "result": {
            "status": "stopped_invalid_spec",
            "spec_name": state["spec"]["name"],
            "issues": issues,
            "summary": state.get("derived_summary", ""),
        },
    }


async def stopped_no_toolchain_node(state: dict[str, Any]) -> dict[str, Any]:
    """没有 ``allegro.exe`` 时的安全终点：装置已备好，等有环境的机器。"""
    return {
        "needs_review": True,
        "artifact_paths": _collect_artifacts(state),
        "status": "stopped_no_toolchain",
        "result": {
            "status": "stopped_no_toolchain",
            "spec_name": state["spec"]["name"],
            "generated_files": state.get("generated_files", {}),
            "note": "SKILL 已生成但未执行：本机没有 allegro.exe，请在有 Cadence 的机器上运行",
        },
    }


async def failed_node(state: dict[str, Any]) -> dict[str, Any]:
    """技术失败的收敛点。"""
    logger.error("pcb.failed", errors=state.get("errors", []))
    return {
        "needs_review": False,
        "status": "failed",
        "result": {
            "status": "failed",
            "spec_name": (state.get("spec") or {}).get("name", ""),
            "errors": list(state.get("errors", [])),
        },
    }


# ── 路由 ─────────────────────────────────────────────────────────


def route_after_load(state: dict[str, Any]) -> str:
    return "failed" if state.get("status") == "failed" else "continue"


def route_after_validate(state: dict[str, Any]) -> str:
    status = state.get("status")
    if status == "failed":
        return "failed"
    return "invalid" if status == "spec_invalid" else "continue"


def route_after_human_input(state: dict[str, Any]) -> str:
    return "cancelled" if state.get("status") == "cancelled" else "continue"


def route_after_emit(state: dict[str, Any]) -> str:
    """``emit_only`` 模式停在装置层，不触碰 Allegro。"""
    if state.get("status") == "failed":
        return "failed"
    return "emit_only" if state.get("mode") == "emit_only" else "build"


def route_continue_or_failed(state: dict[str, Any]) -> str:
    """普通节点路由：技术失败进 failed，环境缺失进 stopped。"""
    status = state.get("status")
    if status == "stopped_no_toolchain":
        return "stopped"
    return "failed" if status == "failed" else "continue"


def route_after_judge(state: dict[str, Any]) -> str:
    return "failed" if state.get("status") == "failed" else "continue"
