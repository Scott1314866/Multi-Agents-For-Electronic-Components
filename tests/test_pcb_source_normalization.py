import asyncio,base64
from pathlib import Path
from PIL import Image
from backend.agents.pcb import image_input as I


def png(path,color):Image.new('RGB',(16,16),color).save(path)
def color(path):return Image.open(path).convert('RGB').getpixel((0,0))


def test_source_revision_never_overwrites_old_primary_or_dimension_table(tmp_path):
    old=tmp_path/'input.png';table=tmp_path/'context_1.png';new=tmp_path/'supplement.png'
    png(old,'red');png(table,'green');png(new,'blue')
    before={p:p.read_bytes() for p in (old,table)}
    out=asyncio.run(I.prepare_input_node({'image_path':str(new),'context_image_paths':[str(old),str(table)],
                        'work_dir':str(tmp_path),'source_revision_id':'latest-revision'}))
    assert out['status']=='input_ready'
    assert color(out['image_path'])==(0,0,255)
    assert [color(p) for p in out['context_image_paths']]==[(255,0,0),(0,128,0)]
    assert all(p.read_bytes()==content for p,content in before.items())


def test_all_inputs_are_snapshotted_before_reusing_initial_slots(tmp_path):
    main=tmp_path/'input.png';one=tmp_path/'context_1.png';two=tmp_path/'context_2.png'
    png(main,'blue');png(one,'red');png(two,'green')
    out=asyncio.run(I.prepare_input_node({'image_path':str(main),'context_image_paths':[str(two),str(one)],'work_dir':str(tmp_path)}))
    assert [color(p) for p in out['context_image_paths']]==[(0,128,0),(255,0,0)]


def test_mechanical_outline_in_context_is_used_instead_of_pin_assignment_diagram(monkeypatch):
    root=Path(__file__).parent/'golden/pcb/mechanical_outline'
    primary=root/'five-pin-top-view.png';body=root/'five-pin-outline.png'
    observed=[]
    async def extract(content,message,topology):
        observed.append((content,message,topology))
        return I.ImageExtraction(spec={},evidence={},source_units='mm')
    monkeypatch.setattr(I,'_extract_max_outline',extract)
    asyncio.run(I.extract_image(str(primary),'尺寸根据max值来',context_images=[str(body)]))
    content,message,topology=observed[0]
    assert base64.b64decode(content[0]['image_url']['url'].split(',',1)[1])==body.read_bytes()
    assert topology['source_image_index']==1
    assert len(topology['rows']['bottom'])==3 and len(topology['rows']['top'])==2
