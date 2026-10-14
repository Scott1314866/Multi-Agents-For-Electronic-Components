"""Use the same Capture Tcl template on a configured Windows SSH host."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

from backend.agents.symbol.contracts.layout import SymbolLayout
from .capture import CaptureResult, CaptureToolchainUnavailable, PASS_MARKER, OLE_HEADER, _validate_part_name, render_tcl, _decode


def _ps_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class WindowsSSH:
    def __init__(self, settings):
        host, user = settings.symbol_ssh_host, settings.symbol_ssh_user
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host or ""):
            raise ValueError("SYMBOL_SSH_HOST 必须是主机名或 IPv4 地址")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", user or ""):
            raise ValueError("请配置有效的 SYMBOL_SSH_USER")
        key = Path(settings.symbol_ssh_private_key).expanduser()
        if not settings.symbol_ssh_private_key or not key.is_file():
            raise FileNotFoundError("请配置存在的 SYMBOL_SSH_PRIVATE_KEY 私钥文件")
        self.ssh, self.scp = shutil.which("ssh"), shutil.which("scp")
        if not self.ssh or not self.scp:
            raise FileNotFoundError("SSH 模式需要 OpenSSH 客户端 ssh 和 scp")
        self.target = f"{user}@{host}"
        # Ignore user SSH config; never fall back to passwords or accept unknown hosts.
        self.options = ["-F", os.devnull, "-i", str(key), "-o", "IdentitiesOnly=yes",
                        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        "-o", "StrictHostKeyChecking=yes"]
        if settings.symbol_ssh_known_hosts:
            known = Path(settings.symbol_ssh_known_hosts).expanduser()
            if not known.is_file():
                raise FileNotFoundError("SYMBOL_SSH_KNOWN_HOSTS 文件不存在")
            self.options += ["-o", f"UserKnownHostsFile={known}"]
        self.port = str(settings.symbol_ssh_port)

    def _run(self, command: list[str], timeout: float):
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False,
                                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"SSH 设备操作超过 {timeout:g} 秒，请检查设备连接和远端服务") from None
        if result.returncode:
            raise RuntimeError(f"SSH/scp 失败（{result.returncode}）：{_decode(result.stderr)[-2000:]}")
        return result

    def powershell(self, script: str, timeout: float = 30):
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = self._run([self.ssh, *self.options, "-p", self.port, self.target,
                            "powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], timeout)
        return json.loads(_decode(result.stdout).strip().lstrip("\ufeff"))

    def upload(self, local: Path, remote: str):
        self._run([self.scp, *self.options, "-P", self.port, str(local), f"{self.target}:{remote}"], 60)

    def download(self, remote: str, local: Path):
        self._run([self.scp, *self.options, "-P", self.port, f"{self.target}:{remote}", str(local)], 60)


def generate_remote(layout: SymbolLayout, *, out_dir: Path, settings) -> CaptureResult:
    _validate_part_name(layout.part_name)
    # Also safe with older scp clients that construct a remote shell command.
    if not re.fullmatch(r"[A-Za-z0-9_. -]+", layout.part_name):
        raise ValueError("SSH 模式的型号仅支持字母、数字、空格、点、下划线和连字符")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    script = out_dir / f"{layout.part_name}_gen.tcl"
    script.write_text(render_tcl(layout, out_dir, dbo_dll=settings.symbol_ssh_dbo_dll), encoding="utf-8")
    log_path = out_dir / "capture.log"
    execution = {"mode": "ssh"}
    passed = False
    log = ""
    try:
        transport = WindowsSSH(settings)
    except (ValueError, FileNotFoundError) as exc:
        raise CaptureToolchainUnavailable(str(exc), script) from exc
    stage = Path(tempfile.mkdtemp(prefix="capture-ssh-", dir=out_dir))
    try:
        bootstrap = transport.powershell(f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tclsh = {_ps_string(settings.symbol_ssh_tclsh)}
$dll = {_ps_string(settings.symbol_ssh_dbo_dll)}
if (-not (Test-Path -LiteralPath $tclsh -PathType Leaf) -or -not (Test-Path -LiteralPath $dll -PathType Leaf)) {{
    Write-Output '{{"error":"远端缺少 Cadence tclsh.exe 或 DBO DLL"}}'; exit 0
}}
$root = {_ps_string(settings.symbol_ssh_remote_root)}
if (-not $root) {{ $root = Join-Path $env:LOCALAPPDATA 'symbol-agent/runs' }}
if ($root -notmatch '^[A-Za-z]:[\\\\/]') {{ throw 'SYMBOL_SSH_REMOTE_ROOT must be an absolute Windows path' }}
$run = Join-Path $root {_ps_string(uuid.uuid4().hex)}
[void](New-Item -ItemType Directory -Path $run -ErrorAction Stop)
ConvertTo-Json -InputObject @{{run_dir = $run.Replace('\\', '/')}} -Compress
""")
        if bootstrap.get("error"):
            raise CaptureToolchainUnavailable(bootstrap["error"], script)
        remote_dir = bootstrap["run_dir"]
        # Safe SFTP/PowerShell argument paths even if host returns unexpected data.
        if not re.fullmatch(r"[A-Za-z]:/[A-Za-z0-9_ ./-]+", remote_dir) or ".." in remote_dir.split("/"):
            raise ValueError("远端工作目录无效")
        execution["remote_dir"] = remote_dir
        upload_script = stage / script.name
        upload_script.write_text(render_tcl(layout, remote_dir, dbo_dll=settings.symbol_ssh_dbo_dll), encoding="utf-8")
        remote_script = f"{remote_dir}/{script.name}"
        runner = Path(__file__).with_name("capture_runner.ps1")
        transport.upload(upload_script, remote_script)
        transport.upload(runner, f"{remote_dir}/capture_runner.ps1")
        expected = hashlib.sha256(upload_script.read_bytes()).hexdigest()
        result = transport.powershell(f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ((Get-FileHash -LiteralPath {_ps_string(remote_script)} -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{expected}') {{ throw 'Tcl upload checksum mismatch' }}
& {_ps_string(remote_dir + '/capture_runner.ps1')} -Tclsh {_ps_string(settings.symbol_ssh_tclsh)} -Script {_ps_string(remote_script)} -Part {_ps_string(layout.part_name)} -TimeoutSeconds {math.ceil(settings.symbol_capture_timeout)}
""", settings.symbol_capture_timeout + 45)
        execution.update({"exit_code": result.get("exit_code"), "timed_out": result.get("timed_out", False)})
        transport.download(f"{remote_dir}/capture.log", stage / "capture.log")
        log = (stage / "capture.log").read_text(encoding="utf-8", errors="replace")
        if (result.get("passed") is True and result.get("exit_code") == 0
                and not result.get("timed_out") and PASS_MARKER in log):
            for extension in ("OLB", "DSN"):
                name = f"{layout.part_name}.{extension}"
                local = stage / name
                transport.download(f"{remote_dir}/{name}", local)
                data = local.read_bytes()
                expected_file = result["files"][extension]
                if (len(data) != expected_file["size"] or not data or
                        hashlib.sha256(data).hexdigest() != expected_file["sha256"].lower() or
                        not data.startswith(OLE_HEADER)):
                    raise ValueError(f"{name} 下载校验失败")
            # Publish only after both native documents have been verified.
            for extension in ("OLB", "DSN"):
                name = f"{layout.part_name}.{extension}"
                os.replace(stage / name, out_dir / name)
            passed = True
        if not passed:
            log += "\nCapture 未生成通过验证的 OLB/DSN。请检查 Cadence 许可和日志。"
    except CaptureToolchainUnavailable:
        raise
    except Exception as exc:
        execution["error"] = type(exc).__name__
        log += f"\n{exc}"
    if "ORDBDLL-1017" in log or "License not found" in log:
        execution["error"] = "license_unavailable"
    log_path.write_text(log, encoding="utf-8")
    return CaptureResult(log, passed, out_dir / f"{layout.part_name}.OLB",
                         out_dir / f"{layout.part_name}.DSN", script, log_path, execution)
