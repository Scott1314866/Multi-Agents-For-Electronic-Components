"""Electrical IDs from the reported TL331U-S5TR TOP VIEW, not row indices."""
import asyncio
from copy import deepcopy
import pytest

from backend.agents.pcb.pin_reference import PinReferenceReading, bind_pin_reference, pin_reference_issues
from backend.agents.pcb.human_nodes import validate_human_answer, confirm_image_parameters_node
from backend.agents.pcb.image_input import ImageExtraction, assess_extraction
from backend.agents.pcb.clarification import plan_questions
from backend.agents.pcb.nodes import validate_spec_node
from tests.test_pcb_mechanical_outline import five_pin


def source():
    return PinReferenceReading(view='top', view_evidence='TL331U-S5TR SOT23-5 Top View', image_index=2,
        confidence=.99, rows=[dict(side='left', numbers=['1','2','3'], evidence='left top 1, middle 2, bottom 3'),
                             dict(side='right', numbers=['5','4'], evidence='right bottom 4, top 5')])


def draft():
    outline=five_pin();outline.rotation_deg=-90
    reading=ImageExtraction(spec={'name':'MAX_SMD_5P','family':'SMD','ep_present':False}, evidence={},
        source_units='mm', mechanical_outline=outline, pin_reference_reading=source())
    spec,_=assess_extraction(reading)
    return spec,reading


def test_real_five_pin_map_survives_rotated_mechanical_drawing_and_table_reorder():
    spec,reading=draft()
    positions={p['number']:(p['x'],p['y']) for p in spec['pads']}
    assert positions=={'1':(-1.2,.95),'2':(-1.2,0),'3':(-1.2,-.95),'4':(1.2,-.95),'5':(1.2,.95)}
    assert pin_reference_issues(list(reversed(spec['pads'])),reading.pin_reference)==[]
    assert not {'view','pin_numbers'} & {q['id'] for q in plan_questions({'spec':spec,'image_reading':reading.model_dump()})}


def test_old_bf_sequential_table_numbers_rejected_before_cadence():
    spec,reading=draft()
    # The actual completed record starts with right-upper/right-lower rows.
    spec['pads']=sorted(spec['pads'],key=lambda p:(-p['x'],-p['y']))
    for i,p in enumerate(spec['pads'],1):p['number']=str(i)
    spec['pin_numbering']=[p['number'] for p in spec['pads']]
    request={'stage':'image_parameters','spec':spec,'pin_reference':reading.pin_reference}
    with pytest.raises(ValueError,match='脚号应为 5'):
        validate_human_answer(request,{'action':'submit','spec':spec})
    result=asyncio.run(validate_spec_node({'spec':spec,'image_reading':reading.model_dump()}))
    assert result['status']=='spec_invalid' and any('脚号应为 5' in x for x in result['clearance_issues'])


def test_dimensions_and_table_order_can_change_without_changing_electrical_ids():
    spec,reading=draft();pads=list(reversed(deepcopy(spec['pads'])))
    for p in pads:p.update(x=p['x']*1.1,y=p['y']*1.2,size_x=.7)
    assert pin_reference_issues(pads,reading.pin_reference)==[]
    pads.pop()
    assert pin_reference_issues(pads,reading.pin_reference)


@pytest.mark.parametrize('change',[{'view':'unknown'},{'view':'bottom'},{'confidence':.8},{'view_evidence':''}])
def test_no_unproven_view_or_low_confidence_binding(change):
    spec,_=draft()
    with pytest.raises(ValueError):bind_pin_reference(spec['pads'],source().model_copy(update=change))


def test_missing_visible_label_and_symmetric_orientation_are_not_guessed():
    spec,_=draft();ref=source();ref.rows[1].numbers[1]=None
    with pytest.raises(ValueError,match='完整'):bind_pin_reference(spec['pads'],ref)
    ref=source();ref.rows[0].numbers=['3','1'];pads=[p for p in spec['pads'] if p['number']!='2']
    with pytest.raises(ValueError,match='不唯一'):bind_pin_reference(pads,ref)


def test_valid_submit_clears_current_errors_but_preserves_source_and_history(monkeypatch):
    from backend.agents.pcb import human_nodes
    spec,reading=draft()
    answer=validate_human_answer({'stage':'image_parameters','spec':spec,'pin_reference':reading.pin_reference},{'action':'submit','spec':spec})
    monkeypatch.setattr(human_nodes,'_ask',lambda request:answer)
    state={'spec':spec,'image_reading':reading.model_dump(),'extraction_issues':['old missing pin numbers'],
           'source_extraction_issues':['old missing pin numbers'],'human_history':[]}
    update=asyncio.run(confirm_image_parameters_node(state))
    assert update['extraction_issues']==[]
    assert {**state,**update}['source_extraction_issues']==['old missing pin numbers']
    assert update['human_history'][0]['stage']=='image_parameters'
