"""Unified adapters keep ownership, persistence and review in the existing APIs."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
import httpx
import pytest

from backend.api.v1 import workbench as W

ID = str(uuid.uuid4())
USER = {"tenant_id": "tenant-a", "user_id": "trusted"}


@pytest.mark.parametrize('available', [True,False])
def test_readiness_reports_database_failure_without_exposing_connection_details(monkeypatch,available):
    class Connection:
        async def __aenter__(self):
            if not available:raise RuntimeError('private connection details')
            return self
        async def __aexit__(self,*_):pass
        async def execute(self,query):assert str(query)=='SELECT 1'
    monkeypatch.setattr(W,'engine',SimpleNamespace(connect=Connection))
    application=FastAPI();application.include_router(W.router,prefix='/workbench')
    response=request(application,'GET','/workbench/ready')
    assert response.status_code==(200 if available else 503)
    assert 'private connection details' not in response.text


@pytest.fixture
def app():
    app = FastAPI()
    app.include_router(W.router, prefix="/workbench")
    app.dependency_overrides[W.get_current_user] = lambda: USER
    return app


def request(app, method, path, **kwargs):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(scenario())


@pytest.mark.parametrize('kind', ['step', 'symbol', 'pcb'])
def test_submission_routes_once_to_existing_durable_api(app, monkeypatch, kind):
    calls = []
    async def submit(**kwargs):
        calls.append(kwargs)
        assert await kwargs['file'].read() == b'image'
        return {'drawing_id': ID, 'status': 'ai_processing', 'status_url': 'legacy'}
    monkeypatch.setattr(W.step, 'generate_step_from_user_input', submit)
    monkeypatch.setattr(W.symbol, 'generate_symbol', submit)
    monkeypatch.setattr(W.pcb, 'generate_package_from_image', submit)
    data = {'kind': kind, 'message': 'source dimensions'}
    if kind != 'step':
        data['execution_mode'] = 'ssh'
    response = request(app, 'POST', '/workbench/jobs', data=data, files={'file': ('drawing.png', b'image', 'image/png')})
    assert response.status_code == 202
    assert len(calls) == 1 and calls[0]['current_user'] == USER
    assert response.json()['status_url'] == f'/api/v1/workbench/jobs/{kind}/{ID}'
    assert calls[0]['file'].file.closed
    if kind != 'step':
        assert calls[0]['execution_mode'] == 'ssh'


def test_step_rejects_a_cadence_device_and_invalid_types(app):
    for data in [{'kind':'step','execution_mode':'ssh'}, {'kind':'unknown'}, {'kind':'pcb','execution_mode':'shell'}]:
        response = request(app, 'POST', '/workbench/jobs', data=data, files={'file': ('draw.png', b'data')})
        assert response.status_code == 422


@pytest.mark.parametrize('kind', ['step', 'symbol', 'pcb'])
def test_read_delegates_tenant_and_normalizes_artifacts(app, monkeypatch, kind):
    async def get(id, user, **kwargs):
        assert str(id) == ID and user == USER
        return {'drawing_id':ID,'status':'pending_review','pending_input':{'interrupt_id':'q'},
                'artifacts':['psm','preview_png','not_allowed'], 'capture_execution':{'mode':'ssh'},
                'artifact_urls':{'olb':'/olb'}, 'step_file_url':'/step', 'preview_urls':{'front':'/front'}}
    monkeypatch.setattr(W.step, 'get_step_drawing', get)
    monkeypatch.setattr(W.symbol, 'get_drawing', get)
    monkeypatch.setattr(W.pcb, 'get_package', get)
    response = request(app, 'GET', f'/workbench/jobs/{kind}/{ID}')
    assert response.status_code == 200
    data = response.json()
    assert data['kind'] == kind and data['pending_input']['interrupt_id'] == 'q'
    if kind == 'pcb':
        assert set(data['artifacts']) == {'psm','preview_png'}
    elif kind == 'step':
        assert data['artifacts']['step'] == '/step'
    else:
        assert data['execution']['mode'] == 'ssh'


def test_list_has_single_global_pagination_and_all_branches_are_tenant_scoped(app, monkeypatch):
    queries = []
    rows = [{'kind':kind,'id':ID,'status':'ai_processing','package_params':{'device':kind},'created_at':datetime.now(timezone.utc)}
            for kind in ('pcb','step','symbol')]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def execute(self, query, params):
            queries.append((str(query), params))
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))
    monkeypatch.setattr(W, 'AsyncSessionLocal', Session)
    result = request(app, 'GET', '/workbench/jobs?limit=2&offset=4').json()
    assert [item['kind'] for item in result['items']] == ['pcb','step'] and result['has_more']
    query, params = queries[0]
    assert query.count('tenant_id=:tenant_id') == 3 and 'deleted_at IS NULL' in query
    assert params == {'tenant_id':'tenant-a','kind':None,'limit':3,'offset':4}


@pytest.mark.parametrize('kind', ['step', 'symbol', 'pcb'])
def test_human_input_uses_existing_interrupt_checks_and_authenticated_actor(app, monkeypatch, kind):
    async def answer(id, request, user):
        assert str(id) == ID and user == USER and request.interrupt_id == 'stale'
        raise HTTPException(status_code=409, detail='当前问题已改变')
    monkeypatch.setattr(W.step, 'submit_step_human_input', answer)
    monkeypatch.setattr(W.symbol, 'submit_human_input', answer)
    monkeypatch.setattr(W.pcb, 'submit_human_input', answer)
    response = request(app,'POST',f'/workbench/jobs/{kind}/{ID}/human-input',json={'interrupt_id':'stale','answer':{'action':'approve'}})
    assert response.status_code == 409


def test_ownership_and_file_whitelist_errors_are_not_hidden(app, monkeypatch):
    async def missing(*_):
        raise HTTPException(status_code=404, detail='任务不存在')
    monkeypatch.setattr(W.pcb, 'get_package', missing)
    monkeypatch.setattr(W.pcb, 'download_artifact', missing)
    assert request(app,'GET',f'/workbench/jobs/pcb/{ID}').status_code == 404
    assert request(app,'GET',f'/workbench/jobs/pcb/{ID}/artifacts/unknown').status_code == 404
    assert request(app,'GET','/workbench/ui/assets/not-allowed').status_code == 404


def test_device_response_contains_no_connection_secrets(app, monkeypatch, tmp_path):
    key = tmp_path / 'key'
    key.write_text('do-not-expose')
    settings = SimpleNamespace(symbol_ssh_host='private-host',symbol_ssh_user='private-user',symbol_ssh_private_key=str(key),symbol_ssh_known_hosts='')
    monkeypatch.setattr(W, 'get_settings', lambda: settings)
    monkeypatch.setattr(W, 'resolve_mode', lambda _: 'ssh')
    monkeypatch.setattr(W.shutil, 'which', lambda _: 'client')
    response = request(app,'GET','/workbench/devices')
    assert response.status_code == 200 and response.json()['items'][1]['configured']
    assert all(secret not in response.text for secret in ['private-host','private-user','do-not-expose',str(key)])


@pytest.mark.parametrize('kind', ['step', 'symbol', 'pcb'])
def test_original_attachments_are_tenant_scoped_and_preserve_file_bytes(app, monkeypatch, tmp_path, kind):
    module, constant = {'step': (W.step, 'STEP_JOB_ROOT'), 'symbol': (W.symbol, 'SYMBOL_JOB_ROOT'),
                        'pcb': (W.pcb, 'PCB_JOB_ROOT')}[kind]
    root = tmp_path / kind
    directory = root / ID
    directory.mkdir(parents=True)
    source = directory / 'input.png'
    content = b'original image bytes'
    source.write_bytes(content)
    context = directory / 'context_1.png'
    context.write_bytes(b'context image')
    outside = tmp_path / 'private.png'
    outside.write_bytes(b'private')
    params = {'original_filename': '尺寸图.png', 'source_path': str(source), 'image_path': str(source),
              'context_image_paths': [str(context), str(outside)]}
    row = {'source_image_path': str(source), 'package_params': params}
    monkeypatch.setattr(module, constant, root)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def execute(self, query, bound):
            assert str(bound['id']) == ID and bound['tenant_id'] == USER['tenant_id']
            assert 'tenant_id=:tenant_id' in str(query)
            if kind == 'symbol': assert 'deleted_at IS NULL' in str(query)
            return SimpleNamespace(mappings=lambda: SimpleNamespace(fetchone=lambda: row))
    monkeypatch.setattr(W, 'AsyncSessionLocal', Session)
    response = request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments')
    assert response.status_code == 200
    items = response.json()['items']
    assert [item['name'] for item in items] == (['source', 'context_1'] if kind == 'pcb' else ['source'])
    assert items[0]['filename'] == '尺寸图.png' and items[0]['media_type'] == 'image/png'
    assert str(tmp_path) not in response.text
    downloaded = request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments/source')
    assert downloaded.status_code == 200 and downloaded.content == content
    assert downloaded.headers['cache-control'] == 'private, no-store'
    assert request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments/context_2').status_code == 404
    assert request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments/private').status_code == 404
    row['source_image_path'] = params['source_path'] = params['image_path'] = str(outside)
    assert request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments/source').status_code == 404
    row = None
    assert request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments').status_code == 404
    app.dependency_overrides.clear()
    assert request(app, 'GET', f'/workbench/jobs/{kind}/{ID}/attachments/source').status_code in {401, 403}
