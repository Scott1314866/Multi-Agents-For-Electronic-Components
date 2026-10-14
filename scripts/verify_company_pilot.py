"""Live acceptance of a deployed pilot; never print passwords, codes or JWTs."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo
import httpx

ROOT=Path(__file__).resolve().parents[1]


async def verify(args):
    access=json.loads(args.credentials_file.read_text(encoding='utf-8-sig'))
    base=access['server'].rstrip('/')
    report={'server':base,'checked_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'checks':[]}
    async with httpx.AsyncClient(base_url=base,timeout=10,trust_env=False) as client:
        async def check(name,method,path,expected=200,**kwargs):
            response=await client.request(method,path,**kwargs)
            report['checks'].append({'name':name,'status':response.status_code,'passed':response.status_code==expected})
            if response.status_code!=expected:raise RuntimeError(f'Acceptance check failed: {name}, status={response.status_code}')
            return response
        for path,name in [('/health','process health'),('/api/v1/workbench/ready','database readiness'),
                          ('/','workbench entry'),('/api/v1/auth/ui','registration page'),('/api/v1/auth/ui/admin','invitation page'),
                          ('/api/v1/step/ui','STEP page'),('/api/v1/pcb/ui','PCB page'),('/api/v1/symbol/ui','Symbol page')]:
            await check(name,'GET',path,expected=307 if path=='/' else 200)
        options=(await check('invitation-only registration','GET','/api/v1/auth/registration')).json()
        assert options['mode']=='invite'
        login=(await check('administrator login','POST','/api/v1/auth/login',json={
            'username':access['admin_username'],'password':access['admin_password']})).json()
        admin={'Authorization':'Bearer '+login['access_token']}
        me=(await check('administrator database identity','GET','/api/v1/auth/me',headers=admin)).json()
        assert me['role']=='admin'
        await check('anonymous job access denied','GET','/api/v1/workbench/jobs',expected=403)
        await check('administrator invitation list','GET','/api/v1/auth/invites',headers=admin)
        if args.exercise_registration:
            invite=(await check('single-use invitation creation','POST','/api/v1/auth/invites',expected=201,headers=admin,
                                json={'label':'第一版自动验收 · 单次邀请码','max_uses':1,'expires_days':1})).json()
            suffix=secrets.token_hex(4);password=secrets.token_urlsafe(24)
            bodies=[{'username':f'pilot-test-{suffix}-{i}','email':f'pilot-test-{suffix}-{i}@example.com',
                     'password':password,'invite_code':invite['code']} for i in range(2)]
            responses=await asyncio.gather(*(client.post('/api/v1/auth/register',json=body) for body in bodies))
            statuses=sorted(r.status_code for r in responses)
            assert statuses==[201,400],f'Concurrent invite limit failed: {statuses}'
            report['checks'].append({'name':'single-use concurrent registration','passed':True,'statuses':statuses})
            winner=next(i for i,r in enumerate(responses) if r.status_code==201)
            registered=responses[winner].json();assert registered['role']=='user'
            member={'Authorization':'Bearer '+registered['access_token']}
            record={'server':base,'username':bodies[winner]['username'],'password':password}
            member_file=ROOT/'tmp/company-pilot-test-user.json';member_file.write_text(json.dumps(record,indent=2),encoding='utf-8')
            await check('member login','POST','/api/v1/auth/login',json={'username':record['username'],'password':password})
            await check('member cannot invite','POST','/api/v1/auth/invites',expected=403,headers=member,json={})
            await check('member cannot list invites','GET','/api/v1/auth/invites',expected=403,headers=member)
            await check('member task list','GET','/api/v1/workbench/jobs',headers=member)
            await check('member device status','GET','/api/v1/workbench/devices',headers=member)
            exhausted={**bodies[1-winner],'username':f'pilot-unused-{suffix}'}
            await check('exhausted invitation rejected','POST','/api/v1/auth/register',expected=400,json=exhausted)
            revoke=(await check('revocable invitation creation','POST','/api/v1/auth/invites',expected=201,headers=admin,
                                json={'label':'第一版自动验收 · 撤销测试'})).json()
            await check('invitation revocation','DELETE',f"/api/v1/auth/invites/{revoke['id']}",headers=admin)
            await check('revoked invitation rejected','POST','/api/v1/auth/register',expected=400,
                        json={**exhausted,'invite_code':revoke['code']})
        downloads=[]
        for kind in ('step','symbol','pcb'):
            listing=(await check(kind+' task list','GET',f'/api/v1/workbench/jobs?kind={kind}&limit=20',headers=admin)).json()
            for item in listing['items']:
                job_id=item['drawing_id'];job=(await check(kind+' task details','GET',f'/api/v1/workbench/jobs/{kind}/{job_id}',headers=admin)).json()
                artifacts=job.get('artifacts',{})
                if not artifacts:continue
                for name in ({'step':('step',),'symbol':('olb','dsn'),'pcb':('dra','psm','padstacks_zip')}[kind]):
                    if name not in artifacts:continue
                    response=await client.get(artifacts[name],headers=admin,timeout=30)
                    downloads.append({'kind':kind,'job_id':job_id,'artifact':name,'status':response.status_code,'bytes':len(response.content)})
                    assert response.status_code==200 and response.content
                if any(x['kind']==kind for x in downloads):break
        report['downloads']=downloads
        report['passed']=all(item['passed'] for item in report['checks'])
    destination=ROOT/'output/deployment_verification/company-pilot-20261012/report.json'
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'checks':len(report['checks']),'downloads':downloads,'report':str(destination)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials-file',type=Path,required=True)
    parser.add_argument('--exercise-registration',action='store_true',help='Create a labelled acceptance-test member and temporary test invitations')
    asyncio.run(verify(parser.parse_args()))
