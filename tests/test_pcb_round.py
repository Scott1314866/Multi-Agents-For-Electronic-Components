from copy import deepcopy
from io import BytesIO
from pathlib import Path
from PIL import Image,ImageDraw
import pytest
from backend.agents.pcb.spec import PackageSpec
from backend.agents.pcb.derive import derive_package, check_clearances,summarize
from backend.agents.pcb.skill_emitter import emit_build_skill, emit_verify_skill, paren_depth
from backend.agents.pcb.preview import write_preview
from backend.agents.pcb.parser import parse_verify_log,parse_build_log,parse_props_log
from backend.agents.pcb.assertions import _judge_pin_geometry,judge
from backend.agents.pcb.image_input import validate_image_spec,ImageExtraction,FieldEvidence,assess_extraction
from backend.agents.pcb.contours import read_two_row_topology

def pcmf_spec():
    pads=[]
    for row,y,xs in [('A',.4,[-1,-.6,-.2,.2,.6,1]),('B',0,[-.8,0,.8]),('C',-.4,[-1,-.6,-.2,.2,.6,1])]:
        for i,x in enumerate(xs,1):
            pads.append(dict(number=f'{row}{i}',x=x,y=y,size_x=.25,size_y=.25,shape='CIRCLE'))
    return dict(name='PCMF3USB3B_C',family='WLCSP15',body_x=2.37,body_y=1.17,
                body_height_max=.64,pin_count=15,pin_layout='EXPLICIT',pads=pads,
                pin_numbering=[p['number'] for p in pads],ep_present=False,
                pin1_marker='NONE',mask_expansion=.0375)

def test_two_rows_of_circles_do_not_enter_rectangle_grid_extraction():
    image=Image.new('RGB',(800,400),'white');draw=ImageDraw.Draw(image)
    for x in (200,300,400,500):
        for y in (100,200):draw.ellipse((x,y,x+50,y+50),outline='black',width=2)
    data=BytesIO();image.save(data,format='PNG')
    assert read_two_row_topology(data.getvalue()) is None

def test_round_array_dimensions_topview_and_native_script(tmp_path):
    spec=PackageSpec(**pcmf_spec());derived=derive_package(spec)
    assert check_clearances(spec)==[]
    assert derived.pin_pads['A1'].mask==(.325,.325)
    assert derived.pin_pads['A1'].paste==(.25,.25)
    assert spec.pads[0].y==.4 and spec.pads[-1].y==-.4
    assert '圆形铜盘直径 0.2500' in summarize(spec,derived)
    text=emit_build_skill(spec,derived,tmp_path)
    assert "?figure 'CIRCLE" in text and '?figureSize (diameter:diameter)' in text
    assert 'r = bMakeCirclePadstack(bLand' in text
    assert text.count('r = bMakeCirclePadstack("')==14
    assert paren_depth(text)==0 and paren_depth(emit_verify_skill(spec,tmp_path))==0
    artifacts=write_preview(spec,derived,tmp_path)
    svg=artifacts['preview_svg'].read_text(encoding='utf-8')
    assert svg.count('<circle ')==30  # copper and mask, not reference pixels
    assert 'cx="-1.0" cy="-0.4" r="0.125"' in svg

def test_circle_clearance_uses_curved_edges_instead_of_rectangular_bounding_box():
    raw=pcmf_spec();raw.update(pin_count=2,pin_numbering=['A1','A2'])
    raw['pads']=deepcopy(raw['pads'][:2]);raw['pads'][0].update(x=0,y=0)
    raw['pads'][1].update(x=.3,y=.3)
    assert check_clearances(PackageSpec(**raw))==[] # 0.424264 - 0.25 > 0.15
    raw['pads'][1]['shape']='RECTANGLE'
    assert check_clearances(PackageSpec(**raw)) # mixed curved-to-flat gap is .122487
    raw['pads'][1].update(x=.35,y=.35)
    assert check_clearances(PackageSpec(**raw))==[]
    raw['pads'][1].update(x=.2,y=.2)
    assert check_clearances(PackageSpec(**raw))

def test_shape_mismatch_and_missing_ep_have_actionable_chinese_errors():
    raw=pcmf_spec();raw['pads'][0]['size_y']=.2
    with pytest.raises(ValueError,match='圆形焊盘.*直径'):validate_image_spec(raw)
    raw=pcmf_spec();raw['ep_present']=True
    with pytest.raises(ValueError,match='散热盘'):validate_image_spec(raw)

def test_source_mask_diameter_survives_extraction_without_changing_legacy_defaults():
    raw=pcmf_spec()
    evidence={key:FieldEvidence(text='手册标注，SR=0.325，SL=0.25，单边扩展=(SR-SL)/2',view='recommended land pattern',confidence=1) for key in raw}
    reading=ImageExtraction(spec=raw,evidence=evidence,source_units='mm')
    candidate,issues=assess_extraction(reading)
    assert issues==[] and candidate['mask_expansion']==.0375
    assert derive_package(validate_image_spec(candidate)).pin_pads['A1'].mask==(.325,.325)
    reading.spec.pop('mask_expansion')
    candidate,issues=assess_extraction(reading)
    assert issues==[] and validate_image_spec(candidate).mask_expansion==.05
    reading.uncertain_fields=['pads[0].shape']
    candidate,issues=assess_extraction(reading)
    assert candidate['pads'][0]['shape'] is None
    assert any('第 1 行焊盘的形状' in issue for issue in issues)

def test_native_circle_shape_is_required_not_just_square_bbox():
    raw=pcmf_spec();raw.update(pin_count=1,pin_numbering=['A1'],pads=raw['pads'][:1])
    spec=PackageSpec(**raw);derived=derive_package(spec)
    text='PIN number=("A1") padstack=("round") bbox=(((-1.1625 0.2375) (-0.8375 0.5625))) shape=("CIRCLE") copper_bbox=(((-1.125 0.275) (-0.875 0.525)))'
    record=parse_verify_log(text)
    assert record.pins[0].shape=='CIRCLE'
    assert all(a.passed for a in _judge_pin_geometry(spec,derived,record))
    square=parse_verify_log(text.replace('CIRCLE','RECTANGLE'))
    assert not all(a.passed for a in _judge_pin_geometry(spec,derived,square))
    wrong_diameter=parse_verify_log(text.replace('-1.125 0.275','-1.15 0.25').replace('-0.875 0.525','-0.85 0.55'))
    assert not all(a.passed for a in _judge_pin_geometry(spec,derived,wrong_diameter))

def test_workstation_allegro_round_footprint_readback():
    root=Path(__file__).parent/'golden/pcb/pcmf3usb3b'
    spec=PackageSpec.model_validate_json((root/'spec.json').read_text(encoding='utf-8'))
    read=lambda name:(root/name).read_text(encoding='utf-8')
    verdict=judge(spec,derive_package(spec),build=parse_build_log(read('build.log')),
                  verify=parse_verify_log(read('verify.log')),props=parse_props_log(read('props.log')),
                  symbol_log=read('symbol.log'))
    assert verdict.passed,verdict.to_dict()
