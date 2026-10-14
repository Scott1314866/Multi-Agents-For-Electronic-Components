"""Capture execution regressions; transport is isolated from real SSH/Cadence."""
import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from backend.agents.symbol import config, nodes
from backend.agents.symbol.contracts.layout import build_layout
from backend.agents.symbol.contracts.models import Pin
from backend.agents.symbol.tools import capture, capture_remote


@pytest.fixture
def layout():
    return build_layout([Pin("1", "IN", "PAS", "left"), Pin("2", "OUT", "PAS", "right")], "ADS1115")


@pytest.fixture
def local_toolchain(monkeypatch, tmp_path):
    exe, dll = tmp_path / "tclsh.exe", tmp_path / "orDb_Dll_Tcl64.dll"
    exe.touch()
    dll.touch()
    monkeypatch.setattr(capture, "resolve_tclsh", lambda value: exe)
    monkeypatch.setattr(capture, "resolve_dbo_dll", lambda value: dll)


def test_local_stale_outputs_cannot_pass(monkeypatch, tmp_path, layout, local_toolchain):
    for extension in ("OLB", "DSN"):
        (tmp_path / f"ADS1115.{extension}").write_bytes(b"previous run")
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=capture.PASS_MARKER.encode(), stderr=b""))
    result = capture.generate(layout, out_dir=tmp_path)
    assert not result.passed and result.log_path.is_file()
    assert result.olb.read_bytes() == b"previous run"


def test_local_timeout_keeps_script_and_log(monkeypatch, tmp_path, layout, local_toolchain):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"partial")
    monkeypatch.setattr(capture.subprocess, "run", timeout)
    result = capture.generate(layout, out_dir=tmp_path, timeout=2)
    assert not result.passed and result.script.is_file()
    assert "partial" in result.log_path.read_text(encoding="utf-8")
    assert result.execution["timed_out"] is True


def test_configured_path_and_matching_dll(monkeypatch, tmp_path):
    exe, dll = tmp_path / "tclsh.exe", tmp_path / "orDb_Dll_Tcl64.dll"
    exe.touch()
    dll.touch()
    monkeypatch.delenv("CADENCE_TCLSH", raising=False)
    monkeypatch.delenv("CADENCE_DBO_DLL", raising=False)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(cadence_tclsh=str(exe), cadence_dbo_dll=""))
    assert config.resolve_tclsh() == exe
    assert config.resolve_dbo_dll(exe) == dll
    exe.unlink()
    with pytest.raises(FileNotFoundError, match="配置"):
        config.resolve_tclsh()


def test_dll_path_with_spaces_and_tcl_metacharacters_is_literal(layout):
    import tkinter
    dll = 'C:/Cadence path/$dll[1]/orDb_Dll_Tcl64.dll'
    script = capture.render_tcl(layout, Path("output"), dbo_dll=dll)
    load_line = next(line for line in script.splitlines() if "catch {load " in line)
    interpreter = tkinter.Tcl()
    interpreter.eval('proc load {filename package} { set ::loaded $filename }')
    interpreter.eval(load_line)
    assert interpreter.getvar("loaded") == dll


@pytest.fixture
def remote_settings(tmp_path):
    key = tmp_path / "private_key"
    key.touch()
    return SimpleNamespace(symbol_ssh_host="172.16.10.22", symbol_ssh_user="scott",
        symbol_ssh_port=22, symbol_ssh_private_key=str(key), symbol_ssh_known_hosts="",
        symbol_ssh_remote_root="", symbol_ssh_tclsh="C:/Cadence/tools/bin/tclsh.exe",
        symbol_ssh_dbo_dll="C:/Cadence/tools/bin/orDb_Dll_Tcl64.dll", symbol_capture_timeout=10)


@pytest.fixture
def remote_transport(monkeypatch):
    binary = bytes.fromhex("D0CF11E0A1B11AE1") + b"native document"
    transport = SimpleNamespace(calls=[], passed=True, corrupt=False, missing=False, fail=None, timed_out=False)

    class FakeSSH:
        def __init__(self, settings):
            pass

        def powershell(self, script, timeout=30):
            transport.calls.append(("powershell", script, timeout))
            if transport.fail:
                raise transport.fail
            if "New-Item" in script:
                return {"error": "missing Cadence"} if transport.missing else {"run_dir": "C:/Users/scott/runs/new-job"}
            return {"passed": transport.passed, "timed_out": transport.timed_out,
                    "exit_code": 0 if transport.passed else 1,
                    "files": {ext: {"size": len(binary), "sha256": hashlib.sha256(binary).hexdigest()}
                              for ext in ("OLB", "DSN")}}

        def upload(self, local, remote):
            transport.calls.append(("upload", local, remote))

        def download(self, remote, local):
            transport.calls.append(("download", remote, local))
            if remote.endswith("capture.log"):
                local.write_text(capture.PASS_MARKER if transport.passed else "ORDBDLL-1017: License not found", encoding="utf-8")
            else:
                local.write_bytes(b"corrupt" if transport.corrupt else binary)

    monkeypatch.setattr(capture_remote, "WindowsSSH", FakeSSH)
    return transport


def test_remote_success_verifies_transfers_and_keeps_log(tmp_path, layout, remote_settings, remote_transport):
    result = capture_remote.generate_remote(layout, out_dir=tmp_path, settings=remote_settings)
    assert result.passed and result.olb.is_file() and result.dsn.is_file()
    assert result.execution["mode"] == "ssh" and result.execution["exit_code"] == 0
    uploaded = next(call[1] for call in remote_transport.calls if call[0] == "upload")
    assert 'set outDir  "C:/Users/scott/runs/new-job"' in uploaded.read_text(encoding="utf-8")
    assert result.log_path.read_text(encoding="utf-8") == capture.PASS_MARKER


@pytest.mark.parametrize("failure", ["license", "timeout", "corrupt", "authentication"])
def test_remote_failure_does_not_publish_old_binaries(tmp_path, layout, remote_settings, remote_transport, failure):
    for extension in ("OLB", "DSN"):
        (tmp_path / f"ADS1115.{extension}").write_bytes(b"old")
    if failure in {"license", "timeout"}:
        remote_transport.passed = False
        remote_transport.timed_out = failure == "timeout"
    elif failure == "corrupt":
        remote_transport.corrupt = True
    else:
        remote_transport.fail = RuntimeError("Permission denied (publickey)")
    result = capture_remote.generate_remote(layout, out_dir=tmp_path, settings=remote_settings)
    assert not result.passed and result.script.is_file() and result.log_path.is_file()
    assert result.olb.read_bytes() == b"old" and result.dsn.read_bytes() == b"old"
    if failure == "license":
        assert result.execution["error"] == "license_unavailable"
    if failure == "authentication":
        assert len(remote_transport.calls) == 1  # No automatic rerun or upload after auth fails.


def test_remote_missing_cadence_retains_portable_script(tmp_path, layout, remote_settings, remote_transport):
    remote_transport.missing = True
    with pytest.raises(capture.CaptureToolchainUnavailable) as caught:
        capture_remote.generate_remote(layout, out_dir=tmp_path, settings=remote_settings)
    assert caught.value.script.is_file()


def test_ssh_enforces_noninteractive_trusted_hosts(monkeypatch, remote_settings):
    monkeypatch.setattr(capture_remote.shutil, "which", lambda command: command)
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b"")
    monkeypatch.setattr(capture_remote.subprocess, "run", run)
    transport = capture_remote.WindowsSSH(remote_settings)
    assert transport.powershell("Write-Output '{}'")["ok"]
    command = commands[0]
    assert "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
    assert "IdentitiesOnly=yes" in command and "-EncodedCommand" in command
    assert "-F" in command


@pytest.mark.parametrize("field,value", [("symbol_ssh_host", "-oProxyCommand=evil"),
    ("symbol_ssh_host", "host;evil"), ("symbol_ssh_user", "scott&whoami")])
def test_ssh_rejects_argument_injection(remote_settings, field, value):
    setattr(remote_settings, field, value)
    with pytest.raises(ValueError):
        capture_remote.WindowsSSH(remote_settings)


def test_failed_capture_node_registers_only_script_and_log(monkeypatch, tmp_path):
    script, log_path = tmp_path / "test.tcl", tmp_path / "capture.log"
    script.touch()
    log_path.write_text("License not found")
    async def failed(*args):
        return capture.CaptureResult("License not found", False, tmp_path / "old.OLB",
                                     tmp_path / "old.DSN", script, log_path, {"mode": "local"})
    monkeypatch.setattr(nodes, "_run_capture", failed)
    state = {"layout": {"part_name": "test"}, "work_dir": str(tmp_path)}
    result = asyncio.run(nodes.generate_capture_node(state))
    assert result["status"] == "capture_failed"
    assert set(result["artifact_paths"]) == {"tcl", "capture_log"}
    assert "License not found" in result["errors"][0]


def test_agent_entry_routes_ssh(monkeypatch, tmp_path, layout):
    from backend import config as app_config
    settings = SimpleNamespace(symbol_capture_mode="ssh")
    monkeypatch.setattr(app_config, "get_settings", lambda: settings)
    marker = object()
    def generate_remote(value, *, out_dir, settings):
        assert value is layout and out_dir == tmp_path
        return marker
    monkeypatch.setattr(capture_remote, "generate_remote", generate_remote)
    assert capture.generate_configured(layout, out_dir=tmp_path) is marker
