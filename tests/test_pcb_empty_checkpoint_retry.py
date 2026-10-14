"""A DB failure before checkpoint 1 must preserve source and support safe retry."""
import asyncio
import pytest
from backend.api.v1 import pcb


def test_empty_checkpoint_does_not_project_default_spec_over_source(monkeypatch):
    calls=[]
    async def failed(drawing,tenant,error):calls.append((drawing,tenant,error))
    monkeypatch.setattr(pcb,'_update_job_failed',failed)
    monkeypatch.setattr(pcb,'AsyncSessionLocal',lambda:pytest.fail('An empty snapshot must not overwrite source metadata'))
    asyncio.run(pcb._persist_job_snapshot('drawing','tenant',{'values':{},'checkpoint_id':None,'next_nodes':[]}))
    assert len(calls)==1 and '原始资料已保留' in calls[0][2]


def test_authenticated_retry_without_first_checkpoint_restarts_image_reading(monkeypatch):
    calls=[]
    params={'input_kind':'image','image_path':'saved.png','context_image_paths':['table.png'],
            'message':'尺寸根据max值来','accepted_retry':{'id':'retry1'},'mode':'full'}
    async def row(*args):return {'status':'ai_processing','package_params':params}
    async def snapshot(*args):return {'values':{},'checkpoint_id':None,'next_nodes':[]}
    async def invoke(drawing,state):calls.append(('invoke',drawing,state))
    async def sync(*args):calls.append(('sync',*args))
    monkeypatch.setattr(pcb,'_read_worker_job',row)
    monkeypatch.setattr(pcb,'get_postgres_pcb_snapshot',snapshot)
    monkeypatch.setattr(pcb,'invoke_postgres_pcb_graph',invoke)
    monkeypatch.setattr(pcb,'_sync_job_projection',sync)
    asyncio.run(pcb._run_owned_job('drawing','tenant',recovering=True))
    state=calls[0][2]
    assert state['input_kind']=='image' and state['image_path']=='saved.png'
    assert state['message']=='尺寸根据max值来' and state['context_image_paths']==['table.png']
    assert state['extraction_retry_id']=='retry1'
    assert state.get('human_image_parameters') is None
    assert calls[1]==('sync','drawing','tenant')
