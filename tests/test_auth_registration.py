"""Registration cannot grant roles, escape its invitation, or bypass revocation."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
import pytest

from backend.api.v1 import auth
from backend import dependencies

ADMIN={'user_id': str(uuid4()), 'tenant_id':'company-a', 'role':'admin'}
BODY={'username':'new.member','email':'new@example.com','password':'long trial password','invite_code':'provided-code'}


class Result:
    def __init__(self, rows=()): self.rows=list(rows)
    def first(self): return self.rows[0] if self.rows else None
    def mappings(self): return self
    def all(self): return self.rows
    def fetchall(self): return self.rows


@pytest.fixture
def setup(monkeypatch):
    from backend.core.auth_throttle import _attempts
    _attempts.clear()
    state={'mode':'invite','valid_invite':True,'duplicate':False,'inserts':[], 'queries':[], 'commits':0,'role':'admin'}
    real=auth.get_settings()
    settings=real.model_copy(update={'registration_mode':'invite'})
    monkeypatch.setattr(auth,'get_settings',lambda: settings.model_copy(update={'registration_mode':state['mode']}))
    class Database:
        async def execute(self, query, params=None):
            sql=str(query);state['queries'].append((sql, params))
            if 'SELECT id FROM users' in sql: return Result([{'id':'existing'}] if state['duplicate'] else [])
            if 'UPDATE registration_invites SET used_count' in sql: return Result([{'tenant_id':'invited-company'}] if state['valid_invite'] else [])
            if 'INSERT INTO users' in sql: state['inserts'].append(params);return Result()
            if 'INSERT INTO registration_invites' in sql: state['invite']=params;return Result()
            if 'UPDATE registration_invites SET revoked_at' in sql: return Result([{'id':params['id']}])
            return Result()
        async def commit(self): state['commits']+=1
        async def rollback(self): state['rollback']=True
    db=Database()
    application=FastAPI();application.include_router(auth.router,prefix='/auth')
    application.dependency_overrides[auth.get_db]=lambda:db
    application.dependency_overrides[auth.get_current_user]=lambda:{**ADMIN,'role':state['role']}
    return application,state,db,settings


def request(app,method,path,**kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            return await client.request(method,path,**kwargs)
    return asyncio.run(run())


def test_invited_registration_uses_invite_tenant_and_user_role(setup):
    app,state,_,settings=setup
    response=request(app,'POST','/auth/register',json=BODY)
    assert response.status_code==201
    inserted=state['inserts'][0]
    assert inserted['tenant_id']=='invited-company' and inserted['role']=='user'
    assert auth.pwd_context.verify(BODY['password'],inserted['password_hash'])
    assert state['commits']==1
    token=jwt.decode(response.json()['access_token'],settings.jwt_secret_key,algorithms=[settings.jwt_algorithm])
    assert token['tenant_id']=='invited-company' and token['role']=='user'
    query=next(sql for sql,_ in state['queries'] if 'used_count + 1' in sql)
    assert 'revoked_at IS NULL' in query and 'expires_at > NOW()' in query and 'used_count < max_uses' in query


@pytest.mark.parametrize('extra',[{'role':'admin'},{'tenant_id':'other'},{'is_active':True}])
def test_registration_rejects_privilege_fields(setup,extra):
    app,state,_,_=setup
    assert request(app,'POST','/auth/register',json={**BODY,**extra}).status_code==422
    assert not state['inserts']


def test_invalid_invite_and_closed_registration_never_create_user(setup):
    app,state,_,_=setup;state['valid_invite']=False
    assert request(app,'POST','/auth/register',json=BODY).status_code==400
    state['mode']='closed'
    assert request(app,'POST','/auth/register',json=BODY).status_code==403
    assert not state['inserts'] and state['commits']==0


def test_duplicate_identity_does_not_consume_invite(setup):
    app,state,_,_=setup;state['duplicate']=True
    assert request(app,'POST','/auth/register',json=BODY).status_code==409
    assert not any('used_count + 1' in q for q,_ in state['queries'])


@pytest.mark.parametrize('password',['short','汉'*30])
def test_password_constraints_fail_before_database(setup,password):
    app,state,_,_=setup
    assert request(app,'POST','/auth/register',json={**BODY,'password':password}).status_code==422
    assert not state['queries']


def test_only_admin_can_create_invites_and_code_is_not_stored(setup):
    import hashlib
    app,state,_,_=setup;state['role']='user'
    assert request(app,'POST','/auth/invites',json={}).status_code==403
    state['role']='admin'
    response=request(app,'POST','/auth/invites',json={'max_uses':3,'expires_days':2})
    assert response.status_code==201
    invite=state['invite'];code=response.json()['code']
    assert invite['token_hash']==hashlib.sha256(code.encode()).hexdigest()
    assert code not in invite.values() and invite['tenant_id']==ADMIN['tenant_id']


def test_invite_revocation_is_tenant_scoped(setup):
    app,state,_,_=setup
    assert request(app,'DELETE',f'/auth/invites/{uuid4()}').status_code==200
    query,params=state['queries'][-1]
    assert 'tenant_id = :tenant_id' in query and params['tenant_id']==ADMIN['tenant_id']


def test_public_registration_pages_have_allowlisted_assets(setup):
    app,_,_,_=setup
    for url in ('/auth/ui','/auth/ui/admin','/auth/ui/assets/auth.js','/auth/ui/assets/auth.css'):
        assert request(app,'GET',url).status_code==200
    assert request(app,'GET','/auth/ui/assets/credentials.json').status_code==404


def test_repeated_registration_attempts_are_limited(setup):
    app,state,_,_=setup;state['valid_invite']=False
    for _ in range(40):
        assert request(app,'POST','/auth/register',json=BODY).status_code==400
    response=request(app,'POST','/auth/register',json=BODY)
    assert response.status_code==429 and response.headers.get('retry-after')


def test_disabled_user_token_and_stale_admin_role_are_checked_against_database():
    settings=dependencies.settings; user_id=str(uuid4())
    token=auth._create_access_token({'sub':user_id,'tenant_id':'company-a','role':'admin'},5)
    credentials=HTTPAuthorizationCredentials(scheme='Bearer',credentials=token)
    row={'id':user_id,'username':'member','email':'member@example.com','role':'user','tenant_id':'company-a','is_active':True}
    class DB:
        async def execute(self,query,params):return Result([row])
    result=asyncio.run(dependencies.get_current_user(credentials,DB()))
    assert result['role']=='user' and result['username']=='member'
    row['is_active']=False
    with pytest.raises(HTTPException) as raised:
        asyncio.run(dependencies.get_current_user(credentials,DB()))
    assert raised.value.status_code==401
