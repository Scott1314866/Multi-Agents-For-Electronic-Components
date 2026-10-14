"""Package runtime source and an integrity manifest; omit credentials and data."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def build(destination:Path):
    files={}
    for directory in ('backend','frontend','scripts','docs'):
        for source in (ROOT/directory).rglob('*'):
            relative=source.relative_to(ROOT)
            if not source.is_file() or any(p in ('__pycache__','node_modules','.pytest_cache','tests','test-results','playwright-report') for p in relative.parts):continue
            if source.suffix in ('.pyc','.pyo','.log') or source.name.startswith('.env'):continue
            files[relative.as_posix()]=source.read_bytes()
    files['docker-compose.yml']=(ROOT/'docker-compose.yml').read_bytes()
    manifest={'version':'1.0.0-pilot.1','built_at':datetime.now(timezone.utc).isoformat(),
              'files':{name:hashlib.sha256(content).hexdigest() for name,content in sorted(files.items())}}
    destination.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,content in sorted(files.items()):archive.writestr(name,content)
        archive.writestr('release-manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    print(json.dumps({'archive':str(destination),'files':len(files),'bytes':destination.stat().st_size,
                      'sha256':hashlib.sha256(destination.read_bytes()).hexdigest()}))


if __name__=='__main__':build(ROOT/'tmp/releases/company-pilot-20261012.zip')
