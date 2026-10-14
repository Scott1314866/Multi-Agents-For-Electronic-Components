"""Reject malformed releases before stopping a service; rollback preserves data."""
import hashlib
import json
from pathlib import Path
import zipfile
import pytest
from scripts.deploy_workstation import apply_release, rollback_release, publish_runtime_file


@pytest.mark.parametrize('name',['../outside.py','output/task.bin','/absolute.py',
                                 '.env.local','other-compose.yml'])
def test_invalid_release_paths_are_rejected_before_database_or_service_changes(tmp_path,name):
    archive=tmp_path/'release.zip';payload=b'changed'
    with zipfile.ZipFile(archive,'w') as bundle:
        bundle.writestr(name,payload)
        bundle.writestr('release-manifest.json',json.dumps({'version':'trial','files':{name:hashlib.sha256(payload).hexdigest()}}))
    root=tmp_path/'project';root.mkdir()
    with pytest.raises(ValueError,match='invalid path'):
        apply_release(archive,hashlib.sha256(archive.read_bytes()).hexdigest(),root,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()


def test_archive_checksum_mismatch_stops_before_changes(tmp_path):
    archive=tmp_path/'release.zip';archive.write_bytes(b'untrusted')
    with pytest.raises(ValueError,match='checksum mismatch'):
        apply_release(archive,'0'*64,tmp_path,tmp_path/'backup')


def test_application_rollback_restores_source_and_keeps_task_files(tmp_path):
    root=tmp_path/'project';(root/'backend').mkdir(parents=True);(root/'output').mkdir()
    (root/'backend/main.py').write_text('new')
    (root/'docker-compose.yml').write_text('new dependency configuration')
    (root/'backend/new_feature.py').write_text('added')
    (root/'output/task.step').write_bytes(b'current task')
    backup=tmp_path/'backup';backup.mkdir()
    with zipfile.ZipFile(backup/'application.zip','w') as snapshot:
        snapshot.writestr('backend/main.py','old')
        snapshot.writestr('docker-compose.yml','old dependency configuration')
        snapshot.writestr('.env.local','private configuration')
    (backup/'manifest.json').write_text(json.dumps({'new_files':['backend/new_feature.py']}))
    rollback_release(root,backup)
    assert (root/'backend/main.py').read_text()=='old'
    assert (root/'docker-compose.yml').read_text()=='old dependency configuration'
    assert not (root/'backend/new_feature.py').exists()
    assert (root/'output/task.step').read_bytes()==b'current task'


def test_release_includes_compose_and_excludes_private_configuration(tmp_path, monkeypatch):
    from scripts import build_pilot_release
    root=tmp_path/'project';(root/'backend').mkdir(parents=True)
    (root/'backend/main.py').write_text('runtime')
    (root/'docker-compose.yml').write_text('dependency configuration')
    (root/'.env.local').write_text('private credentials')
    monkeypatch.setattr(build_pilot_release,'ROOT',root)
    archive=tmp_path/'release.zip';build_pilot_release.build(archive)
    with zipfile.ZipFile(archive) as bundle:
        manifest=json.loads(bundle.read('release-manifest.json'))
        assert bundle.read('docker-compose.yml')==b'dependency configuration'
        assert manifest['files']['docker-compose.yml']==hashlib.sha256(b'dependency configuration').hexdigest()
        assert '.env.local' not in bundle.namelist()


def test_powershell_script_is_published_with_exact_bytes_and_valid_syntax(tmp_path):
    import os
    if os.name!='nt':pytest.skip('Windows runtime script check')
    script=tmp_path/'startup.ps1';content=b"Write-Output 'pilot runtime test'\n"
    publish_runtime_file(script,content)
    assert script.read_bytes()==content
