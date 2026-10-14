"""Five-pin outline must produce exactly 3+2 native leads, with explicit row spans."""
import asyncio
import subprocess
import sys
from pathlib import Path
import pytest
import cadquery as cq
from backend.agents.step.features.gullwing_layout import ROW_PARAMETERS,row_positions
from backend.agents.step.families.ic import gullwing_ic
from backend.agents.step.families.registry import derive_family_parameters
from backend.agents.step.vision.schemas import FusedEvidence,FusedParameter
from backend.agents.step.image_nodes import validate_dimension_chain_node,validate_dimensions_node
from backend.agents.step.human_nodes import actionable_dimension_fields,_dimension_question
from backend.agents.step.dimension_input import parse_dimension_answer
from backend.agents.step.ir.executor import build_feature_model
from backend.agents.step.image_nodes import route_after_dimension_gate


def five(**changes):
    values=dict(nominal_pin_count=5,terminal_pitch=.95,pin_span=1.9,total_height=1.25,
                housing_height=1.1,body_standoff=.15,overall_width=3,body_width=1.72,
                body_length=3.02,terminal_length=.6,terminal_thickness=.23,terminal_width=.5,
                negative_side_pin_count=3,positive_side_pin_count=2,
                negative_side_pin_span=1.9,positive_side_pin_span=1.9,
                negative_side_terminal_width=.5,positive_side_terminal_width=.5)
    values.update(changes)
    return FusedEvidence(family_id=gullwing_ic.FAMILY_ID,package_type='SOT23-5 test',
        identified_features=list(gullwing_ic.REQUIRED_FEATURES),parameters=[
        FusedParameter(canonical_name=k,value=v,unit='count' if k.endswith('_count') else 'mm',
                       evidence_ids=['human_input:test:'+k],evidence_kind='human_input',
                       token_ids=[],token_bboxes=[],target_feature='operator_confirmed:'+k,
                       ocr_confidence=0,semantic_confidence=0,raw_texts=[f'test operator confirmed {k}={v}'])
        for k,v in values.items() if v is not None])


def test_five_pin_3_plus_2_preserves_end_positions_in_native_step(tmp_path):
    fused=five();ir=gullwing_ic.plan_from_evidence(fused,source_image_sha256='f'*64)
    model=build_feature_model(cq,ir)
    solids=list(model.Solids())
    assert len(solids)==6 and all(s.isValid() for s in solids)
    leads=solids[1:]
    neg=sorted(round(s.Center().x,6) for s in leads if s.Center().y<0)
    pos=sorted(round(s.Center().x,6) for s in leads if s.Center().y>0)
    assert neg==[-.95,0,.95] and pos==[-.95,.95]
    file=tmp_path/'sot23-5.step';cq.exporters.export(model,str(file))
    imported=cq.importers.importStep(str(file)).val()
    assert len(imported.Solids())==6
    assert abs(imported.BoundingBox().xlen-3.02)<1e-5
    assert abs(imported.BoundingBox().ylen-3)<1e-5
    assert abs(imported.BoundingBox().zlen-1.25)<1e-5


def test_no_guessing_side_counts_or_short_row_span(tmp_path):
    fused=five(**{k:None for k in ROW_PARAMETERS})
    gate=asyncio.run(validate_dimensions_node({'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}))
    assert set(gate['dimension_gate']['missing_fields'])==set(ROW_PARAMETERS)
    state={'fused_evidence':fused.model_dump(),**gate}
    fields=actionable_dimension_fields(state)
    assert set(fields)==set(ROW_PARAMETERS)
    question=_dimension_question(state,fields,'odd-layout')
    assert question['fields']['negative_side_pin_count']['unit']=='count'
    parsed=parse_dimension_answer(question,{'values':{k:{'value':v,'unit':'count' if k.endswith('_count') else 'mm'} for k,v in zip(ROW_PARAMETERS,(3,2,1.9,1.9))}})
    assert parsed['values']['negative_side_pin_count']['value']==3
    with pytest.raises(ValueError,match='关键参数'):gullwing_ic.plan_from_evidence(fused,source_image_sha256='a'*64)


@pytest.mark.parametrize('span,expected',[(1.9,[-.95,.95]),(.95,[-.475,.475])])
def test_two_pin_row_uses_its_own_explicit_span(span,expected):
    values={p.canonical_name:p.value for p in five(positive_side_pin_span=span).parameters}
    assert row_positions(gullwing_ic.layout_parameters(values))[1][1]==expected


@pytest.mark.parametrize('changes,match',[
    ({'positive_side_pin_count':3},'nominal_pin_count'),
    ({'positive_side_pin_span':.7},'positive_side_pin_span'),
    ({'nominal_pin_count':5.5},'引脚数量'),
    ({'negative_side_pin_count':2.5},'negative_side_pin_count'),
])
def test_conflicting_asymmetric_layout_rejected(changes,match):
    with pytest.raises(ValueError,match=match):gullwing_ic.plan_from_evidence(five(**changes),source_image_sha256='a'*64)


def test_odd_chain_and_gate_accept_five_without_even_count_conflict(tmp_path):
    state=asyncio.run(validate_dimension_chain_node({'fused_evidence':five().model_dump(),'output_dir':str(tmp_path)}))
    assert not state['fused_evidence']['conflicting_fields']
    gate=asyncio.run(validate_dimensions_node({**state,'output_dir':str(tmp_path)}))
    assert gate['dimension_gate']['passed']


def test_pin_span_derived_from_actual_two_row_spans_not_total_divided_by_two():
    fused=derive_family_parameters(five(pin_span=None))
    span=next(p for p in fused.parameters if p.canonical_name=='pin_span')
    assert span.value==1.9 and span.evidence_kind=='derived'
    assert set(span.evidence_ids)=={'human_input:test:negative_side_pin_span','human_input:test:positive_side_pin_span'}


def test_three_pin_layout_has_single_center_pin_and_zero_span():
    fused=five(nominal_pin_count=3,negative_side_pin_count=2,positive_side_pin_count=1,
               pin_span=.95,negative_side_pin_span=.95,positive_side_pin_span=0)
    ir=gullwing_ic.plan_from_evidence(fused,source_image_sha256='a'*64)
    assert len(build_feature_model(cq,ir).Solids())==4
    parsed=parse_dimension_answer({'fields':{'positive_side_pin_span':{'unit':'mm'}}},
                                  {'values':{'positive_side_pin_span':{'value':0,'unit':'mm'}}})
    assert parsed['values']['positive_side_pin_span']['value']==0


def test_seven_pin_explicit_row_positions():
    rows=row_positions(dict(count=7,pitch=.95,row_span=2.85,
        negative_side_pin_count=4,positive_side_pin_count=3,
        negative_side_pin_span=2.85,positive_side_pin_span=1.9))
    assert [len(row) for _,row in rows]==[4,3]
    assert rows[1][1]==[-.95,0,.95]


def test_low_confidence_optional_angle_has_human_entry_instead_of_terminal_stop(tmp_path):
    fused=five()
    angle=FusedParameter(canonical_name='lead_angle_deg',value=8,unit='deg',evidence_ids=['angle-token'],
                         token_ids=['angle-token'],token_bboxes=[(1,1,10,10)],target_feature='angle',
                         ocr_confidence=.99,semantic_confidence=.76,raw_texts=['8°'])
    fused=fused.model_copy(update={'parameters':[*fused.parameters,angle]})
    gate=asyncio.run(validate_dimensions_node({'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}))
    state={'fused_evidence':fused.model_dump(),**gate}
    assert actionable_dimension_fields(state)==['lead_angle_deg']
    assert route_after_dimension_gate(state)=='human'


def test_odd_layout_does_not_accept_cyclic_numeric_table_role_guess(tmp_path):
    fused=five()
    params=[]
    for p in fused.parameters:
        if p.canonical_name in {'terminal_length','terminal_width','terminal_thickness'}:
            p=p.model_copy(update={'evidence_kind':'dimension_line','target_feature':'family_constraint:'+p.canonical_name,
                'evidence_ids':['numeric-row'],'token_ids':['numeric-row'],'token_bboxes':[(1,1,10,10)],
                'ocr_confidence':.99,'semantic_confidence':.9})
        params.append(p)
    fused=fused.model_copy(update={'parameters':params})
    gate=asyncio.run(validate_dimensions_node({'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}))
    state={'fused_evidence':fused.model_dump(),**gate}
    assert not gate['dimension_gate']['passed']
    assert set(actionable_dimension_fields(state))=={'terminal_length','terminal_width','terminal_thickness'}


def test_core_cad_import_does_not_initialize_vision_stack():
    result=subprocess.run([sys.executable,'-c',
        "from backend.agents.step.ir.executor import build_feature_model;import sys;assert 'cv2' not in sys.modules;assert 'backend.agents.step.image_nodes' not in sys.modules"],
        capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
