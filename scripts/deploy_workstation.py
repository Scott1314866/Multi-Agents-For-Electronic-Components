"""Apply a validated release on the already provisioned Windows workstation.

Run locally on the target with its existing Python environment. Backups contain
private environment settings and must remain outside the web-served directories.
"""
from __future__ import annotations
import argparse
import base64
import csv
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def run(command,timeout=60):
    result=subprocess.run(command,capture_output=True,timeout=timeout)
    if result.returncode:
        # stderr may contain environment information; keep it out of the console.
        raise RuntimeError('Deployment subprocess failed: '+Path(str(command[0])).name)
    return result


async def active_tasks():
    import asyncpg
    from backend.config import get_settings
    s=get_settings()
    c=await asyncpg.connect(host=s.db_host,port=s.db_port,database=s.db_name,
                           user=s.db_user,password=s.db_password,timeout=5,command_timeout=5)
    try:
        return {t:await c.fetchval("SELECT COUNT(*) FROM "+t+" WHERE status='ai_processing'")
                for t in ('step_drawings','pcb_drawings','symbol_drawings')}
    finally:await c.close()


def publish_runtime_file(target:Path,content:bytes):
    target.parent.mkdir(parents=True,exist_ok=True)
    if os.name=='nt' and target.suffix.lower()=='.ps1':
        # On the workstation a temporary extension followed by rename produced
        # an unreadable script. Publish through PowerShell under its final name,
        # and verify both the parser and the runtime-visible bytes.
        path=str(target).replace("'","''")
        data=base64.b64encode(content).decode('ascii')
        digest=hashlib.sha256(content).hexdigest()
        script=f"""
$ErrorActionPreference='Stop'
$path='{path}'
[IO.File]::WriteAllBytes($path,[Convert]::FromBase64String('{data}'))
$errors=$null;$tokens=$null
[void][Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
if($errors.Count){{exit 3}}
$hasher=[Security.Cryptography.SHA256]::Create()
try{{$hash=[BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($path))).Replace('-','').ToLowerInvariant()}}finally{{$hasher.Dispose()}}
if($hash -ne '{digest}'){{exit 4}}
"""
        encoded=base64.b64encode(script.encode('utf-16le')).decode('ascii')
        run(['powershell.exe','-NoProfile','-NonInteractive','-EncodedCommand',encoded],timeout=20)
    else:
        temporary=target.with_name(target.name+'.pilot-new')
        temporary.write_bytes(content);os.replace(temporary,target)


def apply_release(archive:Path, expected_hash:str, root:Path, backup:Path):
    from backend.config import get_settings
    from dotenv import set_key
    archive=archive.resolve();root=root.resolve();backup=backup.resolve()
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected_hash:
        raise ValueError('Release archive checksum mismatch')
    if backup.exists():raise ValueError('Backup destination already exists')
    with zipfile.ZipFile(archive) as release:
        manifest=json.loads(release.read('release-manifest.json'))
        names=set(manifest['files'])
        if names!=set(release.namelist())-{'release-manifest.json'}:
            raise ValueError('Release archive/manifest mismatch')
        for name,digest in manifest['files'].items():
            target=(root/name).resolve()
            if not target.is_relative_to(root) or (name != 'docker-compose.yml' and
                    Path(name).parts[0] not in ('backend','frontend','scripts','docs')):
                raise ValueError('Release contains an invalid path')
            if hashlib.sha256(release.read(name)).hexdigest()!=digest:
                raise ValueError('Release file checksum mismatch')
        active=asyncio.run(active_tasks())
        if any(active.values()):raise RuntimeError('Running engineering jobs must finish before deployment')
        backup.mkdir(parents=True)
        identity='*'+next(csv.reader(run(['whoami','/user','/fo','csv','/nh']).stdout.decode(errors='replace').splitlines()))[1]
        run(['icacls',str(backup),'/inheritance:r','/grant:r',identity+':(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'])
        s=get_settings()
        run(['docker','exec','project01-postgres','pg_dump','-U',s.db_user,'-d',s.db_name,
             '-Fc','-f','/tmp/foundry-pilot-before.dump'],timeout=120)
        run(['docker','cp','project01-postgres:/tmp/foundry-pilot-before.dump',str(backup/'database.dump')])
        with zipfile.ZipFile(backup/'application.zip','w',zipfile.ZIP_DEFLATED) as snapshot:
            for name in sorted(names):
                old=root/name
                if old.is_file():snapshot.write(old,name)
            snapshot.write(root/'.env.local','.env.local')
        (backup/'manifest.json').write_text(json.dumps({'release':manifest['version'],
            'files':sorted(names),'new_files':[n for n in names if not (root/n).exists()],
            'archive_sha256':expected_hash,'active_tasks_before':active},indent=2),encoding='utf-8')
        # Recheck after the snapshot, because a user could submit during backup.
        if any(asyncio.run(active_tasks()).values()):
            raise RuntimeError('A job started during backup; no running process or source changed')
        pid_path=root/'tmp/server.pid'
        if not pid_path.is_file():raise RuntimeError('Managed backend PID is missing')
        pid=int(pid_path.read_text().strip())
        # Verify the exact PID owns the managed port before stopping it.
        verification=run(['powershell.exe','-NoProfile','-Command',
            f"$p=Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop; if($p.OwningProcess -notcontains {pid}){{exit 2}}"],timeout=15)
        run(['powershell.exe','-NoProfile','-Command',f'Stop-Process -Id {pid} -ErrorAction Stop'],timeout=15)
        try:
            for name in sorted(names):
                publish_runtime_file(root/name,release.read(name))
            environment=root/'.env.local'
            for key,value in {'REGISTRATION_MODE':'invite','APP_ENV':'production','APP_DEBUG':'false',
                              'APP_HOST':'0.0.0.0','APP_PORT':'8000','JWT_ACCESS_TOKEN_EXPIRE_MINUTES':'480'}.items():
                set_key(str(environment),key,value)
            # Existing 64-character signing key stays on the workstation.
            run([sys.executable,'-s','-X','utf8','-c',
                 'import asyncio; from backend.db.migrations import run_migrations; asyncio.run(run_migrations())'],timeout=60)
        except Exception:
            rollback_release(root,backup)
            print(json.dumps({'rolled_back':True,'backup':str(backup)}))
            raise
        print(json.dumps({'deployed':manifest['version'],'files':len(names),
                          'backup':str(backup),'database_backup_bytes':(backup/'database.dump').stat().st_size},ensure_ascii=False))


def rollback_release(root:Path,backup:Path):
    root=root.resolve()
    manifest=json.loads((backup/'manifest.json').read_text(encoding='utf-8'))
    with zipfile.ZipFile(backup/'application.zip') as snapshot:
        for name in snapshot.namelist():
            target=(root/name).resolve()
            if not target.is_relative_to(root):raise ValueError('Invalid backup path')
            publish_runtime_file(target,snapshot.read(name))
    for name in manifest['new_files']:
        target=(root/name).resolve()
        if not target.is_relative_to(root):raise ValueError('Invalid rollback path')
        if target.is_file():target.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--backup',type=Path,required=True)
    args=parser.parse_args()
    if sys.platform!='win32':raise SystemExit('This release targets the existing Windows workstation')
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    os.chdir(ROOT)
    apply_release(args.archive,args.sha256,ROOT,args.backup)


if __name__=='__main__':main()
