"""Dispatch Allegro to the local machine or Symbol's configured Windows SSH host.

Only public execution metadata enters checkpoints. Keys and connection credentials
remain in server settings. Each build gets an isolated remote workspace; subsequent
stages reuse it and reject a changed connection identity.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
import tempfile
import uuid
import zipfile

from backend.agents.pcb.derive import derive_package
from backend.agents.pcb.runner import AllegroRun, DEFAULT_ALLEGRO_EXE, STAGES, run_stage
from backend.agents.pcb.skill_emitter import write_skill_package
from backend.agents.pcb.spec import PackageSpec
from backend.config import get_settings


def execution_options(state: dict, settings) -> dict:
    """Pin mode/toolchain for all three stages without persisting credentials."""
    previous = state.get("execution") or {}
    if previous:
        return dict(previous)
    mode = state.get("execution_mode") or settings.pcb_execution_mode or settings.symbol_capture_mode
    if mode not in {"local", "ssh"}:
        raise ValueError("execution_mode 必须是 local 或 ssh")
    timeout = float(settings.pcb_allegro_timeout if state.get("allegro_timeout") is None else state["allegro_timeout"])
    if not math.isfinite(timeout) or not 0 < timeout <= 1800:
        raise ValueError("Allegro 单阶段超时必须在 0～1800 秒之间")
    exe = state.get("allegro_exe") or settings.pcb_allegro_exe or None
    if mode == "local" and not exe and settings.cadence_install_dir:
        exe = str(Path(settings.cadence_install_dir) / "tools/bin/allegro.exe")
    if mode == "ssh":
        # Symbol already specifies the remote Cadence tools directory.
        exe = exe or str(Path(settings.symbol_ssh_tclsh).with_name("allegro.exe"))
    return {"mode": mode, "exe": exe, "product": settings.pcb_allegro_product,
            "timeout": timeout}


def _remote_path(value: str) -> str:
    value = value.replace("\\", "/")
    if (not re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9_ ./-]+", value)
            or any(part in {".", "..", ""} for part in value[3:].split("/"))):
        raise ValueError("远端工作目录无效")
    return value


def _collect_archive(archive: Path, result: dict, destination: Path) -> None:
    """Verify the entire flat archive before replacing any job artifact."""
    if hashlib.sha256(archive.read_bytes()).hexdigest() != result["archive_sha256"]:
        raise ValueError("远端归档 SHA256 校验失败")
    records = result["files"]
    expected = {record["name"]: record for record in records}
    if len(expected) != len(records):
        raise ValueError("远端产物清单含重复名称")
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (len(entries) > 256 or sum(e.file_size for e in entries) > 128 * 1024 * 1024
                or len({e.filename for e in entries}) != len(entries)
                or {e.filename for e in entries} != set(expected)):
            raise ValueError("远端产物清单与归档不一致或超出限制")
        for entry in entries:
            if (not re.fullmatch(r"[A-Za-z0-9_.-]+", entry.filename)
                    or Path(entry.filename).suffix.lower() not in {".dra", ".psm", ".pad", ".log", ".txt"}):
                raise ValueError("远端归档包含无效文件名")
            content = bundle.read(entry)
            record = expected[entry.filename]
            if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
                raise ValueError("远端产物 SHA256 校验失败：" + entry.filename)
        for entry in entries:
            (destination / entry.filename).write_bytes(bundle.read(entry))


def run_remote_stage(state: dict, stage: str, options: dict, settings) -> AllegroRun:
    # Use precisely the same connection, strict known-host checks and transport
    # as Symbol; PCB has a separate workspace and Allegro process runner.
    from backend.agents.symbol.tools.capture_remote import WindowsSSH, _ps_string
    try:
        transport = WindowsSSH(settings)
    except ValueError as exc:
        raise FileNotFoundError(f"PCB SSH 设备配置不可用：{exc}") from exc
    spec = PackageSpec.model_validate(state["spec"])
    work_dir = Path(state["work_dir"])
    identity = f"{transport.target}:{transport.port}"
    options = dict(options)
    if options.get("target") and options["target"] != identity:
        raise ValueError("SSH 设备配置已改变，不能在另一设备继续当前 PCB 任务")
    options["target"] = identity
    with tempfile.TemporaryDirectory(prefix="allegro-ssh-", dir=work_dir) as temporary:
        staging = Path(temporary)
        if stage == "build":
            bootstrap = transport.powershell(f"""
$ErrorActionPreference = 'Stop'
$exe = {_ps_string(options['exe'] or str(DEFAULT_ALLEGRO_EXE))}
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {{
    ConvertTo-Json -InputObject @{{error='远端缺少 allegro.exe'}} -Compress; exit 0
}}
$root = {_ps_string(settings.pcb_ssh_remote_root)}
if (-not $root) {{ $root = Join-Path $env:LOCALAPPDATA 'pcb-agent/runs' }}
if ($root -notmatch '^[A-Za-z]:[\\\\/]' -or $root -match '(^|[\\\\/])\\.\\.([\\\\/]|$)') {{ throw 'Invalid PCB remote root' }}
$run = Join-Path $root '{uuid.uuid4().hex}'
[void](New-Item -ItemType Directory -Path $run -ErrorAction Stop)
ConvertTo-Json -InputObject @{{run_dir=$run.Replace('\\','/')}} -Compress
""")
            if bootstrap.get("error"):
                raise FileNotFoundError(bootstrap["error"])
            remote = _remote_path(bootstrap["run_dir"])
            options["remote_dir"] = remote
            files = write_skill_package(spec, derive_package(spec), staging, runtime_dir=Path(remote))
            files["runner"] = Path(__file__).with_name("allegro_runner.ps1")
            checks = []
            for path in files.values():
                target = remote + "/" + path.name
                transport.upload(path, target)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                checks.append(f"if ((Get-FileHash -LiteralPath {_ps_string(target)} -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{digest}') {{ throw 'SKILL upload checksum mismatch' }}")
            transport.powershell("$ErrorActionPreference='Stop'\n" + "\n".join(checks) + "\n'{}'")
        else:
            if not options.get("remote_dir"):
                raise ValueError("缺少已完成 build 的远端工作目录，不能继续回读")
            remote = _remote_path(options["remote_dir"])
        result = transport.powershell(
            f"$ErrorActionPreference='Stop'\n& {_ps_string(remote + '/allegro_runner.ps1')}"
            f" -WorkDir {_ps_string(remote)} -Name {_ps_string(spec.name)} -Stage {_ps_string(stage)}"
            f" -Exe {_ps_string(options['exe'])} -Product {_ps_string(options['product'])}"
            f" -TimeoutSeconds {math.ceil(options['timeout'])}", options["timeout"] + 45)
        if result.get("stage") != stage:
            raise ValueError("远端返回了错误阶段的执行结果")
        archive = staging / "results.zip"
        transport.download(remote + f"/{stage}_results.zip", archive)
        _collect_archive(archive, result, work_dir)
        if result.get("timed_out"):
            raise TimeoutError(f"远端 Allegro {stage} 超过 {options['timeout']} 秒；已终止本次进程并回传日志")
        log_path = work_dir / STAGES[stage][2]
        if result["returncode"] == 0 and log_path.name not in {r["name"] for r in result["files"]}:
            raise RuntimeError(f"远端 {stage} 退出码为0但没有阶段日志")
        stdout = (work_dir / f"{stage}_stdout.txt").read_text(encoding="utf-8", errors="replace")
        stderr = (work_dir / f"{stage}_stderr.txt").read_text(encoding="utf-8", errors="replace")
        return AllegroRun(stage, result["returncode"], stdout + "\n" + stderr, log_path,
                         log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else "", options)


def execute_stage(state: dict, stage: str) -> AllegroRun:
    if stage not in STAGES:
        raise ValueError("未知 Allegro 阶段：" + stage)
    settings = get_settings()
    options = execution_options(state, settings)
    if options["mode"] == "ssh":
        return run_remote_stage(state, stage, options, settings)
    result = run_stage(Path(state["work_dir"]), state["spec"]["name"], stage=stage,
                       exe=options["exe"], product=options["product"], timeout=options["timeout"])
    return AllegroRun(result.stage, result.returncode, result.stdout, result.log_path, result.log_text, options)
