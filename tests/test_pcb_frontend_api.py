"""PCB UI stays in frontend; backend/API owns serving and tenant-scoped data."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
import httpx
import pytest

from backend.api.v1 import pcb

ID = str(uuid.uuid4())
USER = {"tenant_id": "tenant-a", "user_id": "trusted"}


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(pcb.router, prefix="/pcb")
    app.dependency_overrides[pcb.get_current_user] = lambda: USER
    return app


def request(app, path):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.get(path)
    return asyncio.run(scenario())


def test_pcb_page_and_all_assets_are_frontend_files(app):
    response = request(app, '/pcb/ui')
    assert response.status_code == 200
    assert '/api/v1/pcb/ui/assets/app.js' in response.text
    assert pcb.FRONTEND_ROOT.name == 'pcb' and pcb.FRONTEND_ROOT.parent.name == 'frontend'
    for name, (path, media_type) in pcb.UI_ASSETS.items():
        response = request(app, '/pcb/ui/assets/' + name)
        assert response.status_code == 200 and response.headers['content-type'].startswith(media_type)
        assert path.resolve().is_relative_to(pcb.PROJECT_ROOT / 'frontend')
    assert request(app, '/pcb/ui/assets/unknown').status_code == 404
    assert request(app, '/pcb/ui/assets/%2e%2e%2fapi.py').status_code == 404


def test_pcb_devices_reuses_device_metadata_for_authenticated_tenant(app, monkeypatch):
    tenants = []
    def status(tenant):
        tenants.append(tenant)
        return {'items': [{'id': 'local'}], 'defaults': {'pcb': 'local'}, 'queues': []}
    monkeypatch.setattr(pcb, 'device_status', status)
    response = request(app, '/pcb/devices')
    assert response.status_code == 200 and response.json()['defaults']['pcb'] == 'local'
    assert tenants == ['tenant-a']


def test_pcb_history_pagination_uses_tenant_bound_query(app, monkeypatch):
    observed = []
    rows = [{'id': str(uuid.uuid4()), 'status': 'completed', 'package_params': {'spec_name': 'XDFN16'},
             'created_at': datetime.now(timezone.utc)} for _ in range(3)]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def execute(self, query, params):
            observed.append((str(query), params))
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))
    monkeypatch.setattr(pcb, 'AsyncSessionLocal', Session)
    response = request(app, '/pcb/packages?limit=2&offset=4')
    data = response.json()
    assert response.status_code == 200 and data['has_more'] and len(data['items']) == 2
    assert data['offset'] == 4
    assert observed[0][1] == {'tenant_id': 'tenant-a', 'limit': 3, 'offset': 4}
    assert 'tenant_id = :tenant_id' in observed[0][0] and 'OFFSET :offset' in observed[0][0]


def test_pcb_status_has_safe_download_urls_and_same_tenant_queue(app, monkeypatch):
    now = datetime.now(timezone.utc)
    row = {'id': ID, 'status': 'pending_review', 'needs_review': True, 'error_msg': None,
           'created_at': now, 'updated_at': now, 'package_params': {'spec_name': 'XDFN16',
           'artifacts': {'psm': '/private/model.psm', 'preview_png': '/private/model.png', 'unknown': '/private/no'},
           'pending_input': {'stage': 'review', 'interrupt_id': 'current', 'options': ['approve', 'reject']}}}
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def execute(self, query, params):
            assert params == {'id': ID, 'tenant_id': 'tenant-a'}
            return SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))
    monkeypatch.setattr(pcb, 'AsyncSessionLocal', Session)
    def queue(tenant):
        assert tenant == 'tenant-a'
        return [{'kind': 'pcb', 'drawing_id': ID, 'mode': 'ssh', 'state': 'running'}]
    monkeypatch.setattr(pcb.cadence_scheduler, 'snapshot', queue)
    data = request(app, f'/pcb/packages/{ID}').json()
    assert set(data['artifact_urls']) == {'psm', 'preview_png'}
    assert all(url.startswith(f'/api/v1/pcb/packages/{ID}/artifacts/') for url in data['artifact_urls'].values())
    assert '/private/' not in str(data['artifact_urls'])
    assert data['queue']['mode'] == 'ssh' and data['human_input_url'].endswith('/human-input')


def test_completed_old_record_hides_resolved_extraction_errors_and_keeps_source_audit(app,monkeypatch):
    now=datetime.now(timezone.utc)
    row={'id':ID,'status':'completed','needs_review':False,'error_msg':None,'created_at':now,'updated_at':now,
         'package_params':{'human_image_parameters':{'action':'submit'},'extraction_issues':['old missing pin numbers']}}
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def execute(self,*_):return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:row))
    monkeypatch.setattr(pcb,'AsyncSessionLocal',Session)
    data=request(app,f'/pcb/packages/{ID}').json()
    assert data['extraction_issues']==[]
    assert data['source_extraction_issues']==['old missing pin numbers']
