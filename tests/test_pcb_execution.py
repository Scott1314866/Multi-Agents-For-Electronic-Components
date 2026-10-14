"""Local/SSH dispatch, checkpoint continuity and artifact integrity."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from backend.agents.pcb import execution as E
from backend.agents.pcb import nodes
from backend.agents.pcb.runner import AllegroRun, build_command
from backend.agents.pcb.spec import wson8_3x3


def settings(**overrides):
    return SimpleNamespace(**{
        'pcb_execution_mode': None, 'symbol_capture_mode': 'ssh',
        'pcb_allegro_timeout': 600, 'pcb_allegro_exe': '',
        'cadence_install_dir': '', 'symbol_ssh_tclsh': 'C:/Cadence/tools/bin/tclsh.exe',
        'pcb_allegro_product': 'Allegro_Enterprise_PCB_Designer',
        'pcb_ssh_remote_root': '', **overrides,
    })


def state(tmp_path):
    return {'spec': wson8_3x3().model_dump(), 'work_dir': str(tmp_path), 'errors': []}


def test_mode_inherits_symbol_but_request_and_pcb_setting_can_override():
    assert E.execution_options({}, settings())['mode'] == 'ssh'
    assert E.execution_options({}, settings(pcb_execution_mode='local'))['mode'] == 'local'
    assert E.execution_options({'execution_mode': 'local'}, settings())['mode'] == 'local'
    assert E.execution_options({}, settings())['exe'] == str(Path('C:/Cadence/tools/bin/allegro.exe'))
    opts = E.execution_options({}, settings(cadence_install_dir='C:/Cadence/SPB', symbol_capture_mode='local'))
    assert opts['exe'] == str(Path('C:/Cadence/SPB/tools/bin/allegro.exe'))


@pytest.mark.parametrize('timeout', [0, -1, 1801, float('nan'), float('inf')])
def test_invalid_timeouts_never_reach_a_process(timeout):
    with pytest.raises(ValueError):
        E.execution_options({'allegro_timeout': timeout}, settings())


def test_checkpoint_pins_options_without_credentials():
    opts = E.execution_options({}, settings())
    opts['remote_dir'] = 'C:/runs/abc'
    assert E.execution_options({'execution': opts}, settings(pcb_execution_mode='local')) == opts
    assert not any('key' in key or 'password' in key for key in opts)


def test_local_dispatch_uses_selected_toolchain_and_preserves_result(monkeypatch, tmp_path):
    monkeypatch.setattr(E, 'get_settings', lambda: settings(symbol_capture_mode='local'))
    seen = []
    def run(root, name, **kwargs):
        seen.append((root, name, kwargs))
        return AllegroRun('build', 0, '', root / 'build.log', 'test')
    monkeypatch.setattr(E, 'run_stage', run)
    result = E.execute_stage(state(tmp_path), 'build')
    assert result.ok and result.execution['mode'] == 'local'
    assert seen[0][2]['timeout'] == 600 and seen[0][2]['stage'] == 'build'


def archive(tmp_path, contents):
    path = tmp_path / 'test.zip'
    with zipfile.ZipFile(path, 'w') as z:
        for name, content in contents.items():
            z.writestr(name, content)
    return path, {'archive_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'files': [{'name': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                            for name, content in contents.items()]}


def test_bad_archive_cannot_overwrite_existing_artifacts(tmp_path):
    target = tmp_path / 'out'
    target.mkdir()
    (target / 'build.log').write_bytes(b'prior')
    path, result = archive(tmp_path, {'build.log': b'new', 'verify.log': b'other'})
    result['files'][1]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='SHA256'):
        E._collect_archive(path, result, target)
    assert (target / 'build.log').read_bytes() == b'prior'


@pytest.mark.parametrize('name', ['../escape.log', 'folder/pad.pad', 'evil.ps1'])
def test_archive_rejects_paths_and_executables(tmp_path, name):
    path, result = archive(tmp_path, {name: b'test'})
    with pytest.raises(ValueError, match='无效文件名'):
        E._collect_archive(path, result, tmp_path)


def test_remote_stages_reuse_checkpoint_directory_and_remap_skill_paths(monkeypatch, tmp_path):
    from backend.agents.symbol.tools import capture_remote
    bootstraps, uploaded, stages = [], {}, []
    result_source = {}
    class SSH:
        target, port = 'test@device', '22'
        def __init__(self, _):
            pass
        def powershell(self, script, timeout=30):
            if 'New-Item' in script:
                bootstraps.append(script)
                return {'run_dir': 'C:/pcb/runs/abc'}
            if ' -Stage ' in script:
                stage = 'build' if "-Stage 'build'" in script else 'verify'
                stages.append(stage)
                path, report = archive(tmp_path, {stage + '.log': b'fresh', stage + '_stdout.txt': b'', stage + '_stderr.txt': b''})
                result_source['path'] = path
                return {**report, 'stage': stage, 'returncode': 0, 'timed_out': False}
            return {}
        def upload(self, local, remote):
            uploaded[remote] = local.read_text(encoding='utf-8')
        def download(self, remote, local):
            local.write_bytes(result_source['path'].read_bytes())
    monkeypatch.setattr(capture_remote, 'WindowsSSH', SSH)
    monkeypatch.setattr(E, 'get_settings', settings)
    first = E.execute_stage(state(tmp_path), 'build')
    checkpoint = {**state(tmp_path), 'execution': first.execution, 'build_run': {'ok': True}}
    second = E.execute_stage(checkpoint, 'verify')
    assert second.ok and len(bootstraps) == 1 and stages == ['build', 'verify']
    assert first.execution['remote_dir'] == second.execution['remote_dir']
    skill = uploaded['C:/pcb/runs/abc/build_' + checkpoint['spec']['name'].lower() + '.il']
    assert 'C:/pcb/runs/abc' in skill and str(tmp_path).replace('\\', '/') not in skill
    assert (tmp_path / 'verify.log').read_text() == 'fresh'
    checkpoint['execution'] = {**first.execution, 'target': 'test@another:22'}
    with pytest.raises(ValueError, match='设备配置已改变'):
        E.execute_stage(checkpoint, 'verify')


def test_missing_toolchain_is_stopped_and_nonzero_return_is_failed(monkeypatch, tmp_path):
    async def missing(*_):
        raise FileNotFoundError('Missing remote Allegro')
    monkeypatch.setattr(nodes, '_execute_stage', missing)
    update = asyncio.run(nodes.run_build_node(state(tmp_path)))
    assert update['status'] == 'stopped_no_toolchain'
    async def failed(*_):
        return AllegroRun('build', 2, 'error', tmp_path / 'build.log', '', {'mode': 'ssh'})
    monkeypatch.setattr(nodes, '_execute_stage', failed)
    update = asyncio.run(nodes.run_build_node(state(tmp_path)))
    assert update['status'] == 'failed' and update['build_run']['returncode'] == 2


def test_generated_command_matches_real_allegro_flags(tmp_path):
    for stage in ('build', 'verify', 'props'):
        command = build_command('allegro.exe', tmp_path, 'WSON8', stage=stage)
        assert '-sq' not in command
        assert command[1:5] == ['-symbol', 'package', '-nographic', '-noopengl']


@pytest.mark.parametrize('case', ['timeout', 'missing_log'])
def test_remote_timeout_and_stale_local_log_cannot_report_success(monkeypatch, tmp_path, case):
    from backend.agents.symbol.tools import capture_remote
    (tmp_path / 'verify.log').write_text('prior success')
    contents = {'verify_stdout.txt': b'', 'verify_stderr.txt': b''}
    if case == 'timeout':
        contents['verify.log'] = b'partial evidence'
    path, manifest = archive(tmp_path, contents)
    class SSH:
        target, port = 'test@device', '22'
        def __init__(self, _):
            pass
        def powershell(self, *_):
            return {**manifest, 'stage': 'verify', 'returncode': 0, 'timed_out': case == 'timeout'}
        def download(self, remote, local):
            local.write_bytes(path.read_bytes())
    monkeypatch.setattr(capture_remote, 'WindowsSSH', SSH)
    options = {**E.execution_options({}, settings()), 'remote_dir': 'C:/runs/abc'}
    with pytest.raises(TimeoutError if case == 'timeout' else RuntimeError):
        E.run_remote_stage(state(tmp_path), 'verify', options, settings())
    if case == 'timeout':
        assert (tmp_path / 'verify.log').read_text() == 'partial evidence'
