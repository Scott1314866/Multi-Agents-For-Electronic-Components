import asyncio
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
import json
import uuid
import httpx
import pymupdf
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from PIL import Image
from backend.agents.pcb.clarification import plan_questions,apply_clarification
from backend.agents.pcb.human_nodes import validate_human_answer
from backend.agents.pcb.image_input import ImageExtraction,FieldEvidence
from backend.agents.pcb.graph import build_pcb_graph
from backend.agents.pcb.source_inputs import decode_supplement
from backend.api.v1 import pcb

def state():
    pads=[dict(number=str(i+1),shape='CIRCLE',x=i*.4,y=0,size_x=.25,size_y=.25) for i in range(2)]
    spec=dict(name='TEST_ROUND',family='TEST',body_x=1,body_y=1,body_height_max=.5,pin_count=2,
              pin_layout='EXPLICIT',pin_numbering=['1','2'],pads=pads,ep_present=False,pin1_marker='NONE')
    evidence={k:FieldEvidence(text='fixture dimensions supplied as source data',view='top',confidence=1).model_dump() for k in spec}
    reading=dict(spec=deepcopy(spec),source_units='mm',evidence=evidence)
    return {'spec':spec,'image_source_spec':deepcopy(spec),'image_reading':reading}

def request(s):
    return {'stage':'image_parameters','options':['clarify','submit','cancel'],'questions':plan_questions(s),
            'spec':s['spec'],'source_spec':s.get('image_source_spec'),
            'evidence':s['image_reading'].get('evidence'),
            'human_clarification_facts':s.get('human_clarification_facts',{})}

def test_followup_applies_process_only_to_matching_width_and_retains_sources():
    s=state();s['spec']['pads'][1].update(size_x=.55,size_y=.55,x=1)
    s['image_source_spec']=deepcopy(s['spec']);original=deepcopy(s['image_source_spec'])
    q=request(s);answer=validate_human_answer(q,{'action':'clarify','values':{'manufacturing':{'mode':'process','from':.25,'to':.2}},'user_id':'forged'})
    updated=apply_clarification(s,q,answer)
    assert updated['spec']['pads'][0]['size_x']==updated['spec']['pads'][0]['size_y']==.2
    assert updated['spec']['pads'][1]['size_x']==.55
    assert s['image_source_spec']==original and 'user_id' not in answer
    final=validate_human_answer({**q,'spec':updated['spec']},{'action':'submit','spec':{}})
    assert len(final['manufacturing_adjustments'])==2
    assert plan_questions({**s,**updated})==[]

def test_unknown_values_and_invalid_or_stale_followups_are_not_fabricated():
    s=state();s['spec'].pop('body_height_max');s['spec']['pads'][1]['number']=None
    q=request(s)
    for values in ({'body_height_max':-1},{'not_in_current_questions':.3},{'pin_numbers':'1 1'}):
        with pytest.raises(ValueError):validate_human_answer(q,{'action':'clarify','values':values})
    assert 'body_height_max' not in s['spec'] and s['spec']['pads'][1]['number'] is None
    s=state();q=request(s)
    with pytest.raises(ValueError,match='间距'):validate_human_answer(q,{'action':'clarify','values':{'manufacturing':{'mode':'process','from':.25,'to':.35}}})

def test_bottom_view_change_is_explicit_and_does_not_renumber_pads():
    s=state();s['image_reading']['evidence']['pin_numbering']['view']='bottom'
    q=request(s);answer=validate_human_answer(q,{'action':'clarify','values':{'view':'mirror_x'}})
    updated=apply_clarification(s,q,answer)
    assert updated['spec']['pads'][1]['x']==-.4
    assert updated['spec']['pin_numbering']==['1','2']
    assert 'view' not in [q['id'] for q in plan_questions({**s,**updated})]

def test_numbering_without_source_also_asks_for_top_view_conversion():
    s=state();s['image_reading']['evidence'].pop('pin_numbering')
    assert {'view','pin_numbers'} <= {q['id'] for q in plan_questions(s)}

def test_multiturn_graph_persists_partial_answers_and_asks_remaining_fields(tmp_path,monkeypatch):
    from backend.agents.pcb import image_input
    source=state();source['spec'].pop('body_height_max');source['spec'].pop('ep_present')
    image=tmp_path/'source.png';Image.new('RGB',(20,20),'white').save(image)
    async def extract(*args,**kwargs):return ImageExtraction(**{**source['image_reading'],'spec':source['spec']})
    monkeypatch.setattr(image_input,'extract_image',extract)
    async def scenario():
        saver=InMemorySaver();graph=build_pcb_graph(saver);cfg={'configurable':{'thread_id':'conversation'}}
        out=await graph.ainvoke({'input_kind':'image','image_path':str(image),'work_dir':str(tmp_path),'mode':'emit_only'},cfg)
        old=out['__interrupt__'][0]
        out=await graph.ainvoke(Command(resume={old.id:{'action':'clarify','values':{'body_height_max':.5},'_actor':{'user_id':'test-actor','answered_at':'test'}}}),cfg)
        nextq=out['__interrupt__'][0]
        assert nextq.id!=old.id and nextq.value['spec']['body_height_max']==.5
        assert 'body_height_max' not in [q['id'] for q in nextq.value['questions']]
        assert 'ep_present' in [q['id'] for q in nextq.value['questions']]
        # Recreate the application graph with the same saver, as after restart.
        graph=build_pcb_graph(saver)
        out=await graph.ainvoke(Command(resume={'action':'clarify','values':{'ep_present':'no','manufacturing':{'mode':'source'}}}),cfg)
        q=out['__interrupt__'][0]
        assert q.value['questions']==[] and q.value['spec']['ep_present'] is False
        assert len(out['human_history'])==2 and out['human_history'][0]['user_id']=='test-actor'
        out=await graph.ainvoke(Command(resume={'action':'submit','spec':{}}),cfg)
        assert out['status']=='skills_ready' and out['image_source_spec'].get('body_height_max') is None
    asyncio.run(scenario())

@pytest.mark.parametrize('already_applied',[False,True])
def test_source_recovery_applies_revision_once_and_retains_answer_history(monkeypatch,already_applied):
    supplement={'id':'revision','interrupt_id':'q','image_path':'source.png','context_image_paths':['context.png'],
                'source_document_paths':['manual.pdf'],'context_source_info':[{'page':3}],
                'user_id':'trusted','answered_at':'test','role':'context','filename':'manual.pdf','pages':[3]}
    row={'status':'ai_processing','package_params':{'accepted_source':supplement}}
    snapshot={'checkpoint_id':'cp','values':{'source_revision_id':'revision' if already_applied else '',
                'human_history':[{'stage':'image_clarification','values':{'manufacturing':{'mode':'source'}}}]},
              'next_nodes':[],'interrupts':[{'id':'q','value':{'stage':'image_parameters'}}]}
    events=[]
    async def read(*args):return row
    async def snap(*args):return snapshot
    async def sync(*args):events.append('project')
    async def resume(task,updates,**kwargs):
        assert not already_applied and kwargs['as_node']=='prepare_input'
        assert updates['source_revision_id']=='revision' and updates['human_clarification_facts']=={}
        assert [h['stage'] for h in updates['human_history']]==['image_clarification','source_supplement']
        events.append('resume')
    monkeypatch.setattr(pcb,'_read_worker_job',read);monkeypatch.setattr(pcb,'get_postgres_pcb_snapshot',snap)
    monkeypatch.setattr(pcb,'_sync_job_projection',sync);monkeypatch.setattr(pcb,'resume_postgres_pcb_graph_at_node',resume)
    asyncio.run(pcb._run_owned_job(str(uuid.uuid4()),'tenant-a',recovering=True))
    assert events==(['project'] if already_applied else ['resume','project'])

def pdf_bytes():
    with pymupdf.open() as doc:
        for text in ('Overview unrelated introduction','PACKAGE DIMENSIONS PCMF3USB3B/C','RECOMMENDED SOLDERING FOOTPRINT PCMF3USB3B/C','PINNING INFORMATION PCMF3USB3B/C'):
            doc.new_page().insert_text((50,50),text)
        return doc.tobytes()

def test_pdf_selects_bounded_source_pages_without_deriving_dimensions():
    source=decode_supplement(pdf_bytes(),'PCMF3USB3B.pdf')
    assert source['pages']==[3,2,4] and len(source['images'])==3
    assert source['document'].startswith(b'%PDF-')
    assert all(image.startswith(b'\x89PNG') for image in source['images'])
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50,50),'No dimensions supplied')
        with pytest.raises(ValueError,match='未找到'):decode_supplement(pdf.tobytes(),'unknown.pdf')

def test_initial_image_upload_accepts_pdf_and_preserves_page_provenance(monkeypatch):
    from starlette.datastructures import UploadFile
    captured=[]
    async def submit(request,user,*,image_input):captured.append(image_input);return {'status':'ai_processing'}
    monkeypatch.setattr(pcb,'_submit_job',submit)
    image=BytesIO();Image.new('RGB',(20,20),'white').save(image,format='PNG')
    async def scenario():
        result=await pcb.generate_package_from_image(file=UploadFile(BytesIO(image.getvalue()),filename='footprint.png'),
            context_images=[UploadFile(BytesIO(pdf_bytes()),filename='PCMF3USB3B.pdf')],
            message='',mode='emit_only',execution_mode=None,allegro_timeout=None,
            current_user={'tenant_id':'tenant-a','user_id':'actor'})
        assert result['status']=='ai_processing'
    asyncio.run(scenario())
    assert len(captured[0]['contexts'])==3 and len(captured[0]['documents'])==1
    assert [info['page'] for info in captured[0]['context_source_info']]==[3,2,4]

@pytest.mark.parametrize('stale,tenant_missing',[(False,False),(True,False),(False,True)])
def test_source_upload_checks_current_interrupt_and_tenant_before_writing(tmp_path,monkeypatch,stale,tenant_missing):
    task=str(uuid.uuid4());folder=tmp_path/task;folder.mkdir();original=folder/'input.png';Image.new('RGB',(20,20),'white').save(original)
    params={'input_kind':'image','image_path':str(original),'context_image_paths':[],
            'pending_input':{'stage':'image_parameters','interrupt_id':'current'}}
    row={'status':'awaiting_input','package_params':params};updates=[];runs=[]
    class Session:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def begin(self):return self
        async def execute(self,query,args):
            assert args['tenant_id']=='tenant-a'
            if str(query).lstrip().startswith('SELECT'):return SimpleNamespace(mappings=lambda:SimpleNamespace(first=lambda:None if tenant_missing else row))
            updates.append(args);return SimpleNamespace()
    @asynccontextmanager
    async def lock(*args,**kwargs):yield True
    async def run(*args,**kwargs):runs.append(args)
    monkeypatch.setattr(pcb,'PCB_JOB_ROOT',tmp_path);monkeypatch.setattr(pcb,'AsyncSessionLocal',Session)
    monkeypatch.setattr(pcb,'open_postgres_pcb_job_lock',lock);monkeypatch.setattr(pcb,'_run_job',run)
    app=FastAPI();app.include_router(pcb.router,prefix='/pcb');app.dependency_overrides[pcb.get_current_user]=lambda:{'tenant_id':'tenant-a','user_id':'actor'}
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            response=await client.post(f'/pcb/packages/{task}/sources',data={'interrupt_id':'old' if stale else 'current'},files={'file':('manual.pdf',pdf_bytes(),'application/pdf')})
            if stale or tenant_missing:
                assert response.status_code==(409 if stale else 404)
                assert not updates and sorted(p.name for p in folder.iterdir())==['input.png']
            else:
                assert response.status_code==202
                saved=json.loads(updates[0]['params'])['accepted_source']
                assert saved['interrupt_id']=='current' and saved['user_id']=='actor'
                assert len(saved['context_image_paths'])==3 and len(saved['source_document_paths'])==1
                assert original.read_bytes().startswith(b'\x89PNG')
    asyncio.run(scenario())
