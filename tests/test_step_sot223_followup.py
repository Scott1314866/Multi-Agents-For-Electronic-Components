"""Repeated correct widths must not hide the erroneous inch-column body length."""
import asyncio
import pytest
from backend.agents.step import human_nodes
from backend.agents.step.dimension_input import parse_dimension_answer
from backend.agents.step.image_nodes import validate_dimension_chain_node,validate_dimensions_node
from backend.agents.step.vision.schemas import FusedEvidence,FusedParameter


def stalled_state(tmp_path):
    values=dict(nominal_pin_count=4,terminal_pitch=2.3,pin_span=4.6,
        total_height=1.8,housing_height=1.65,body_standoff=.15,overall_width=7.3,
        body_width=3.7,body_length=6.6,terminal_length=1.16,terminal_thickness=.35,terminal_width=.82,
        negative_side_pin_count=3,positive_side_pin_count=1,negative_side_pin_span=4.6,positive_side_pin_span=0,
        negative_side_terminal_width=.82,positive_side_terminal_width=3.1)
    wrong={'body_length':(.2598,'D (Inches Max)'),
           'terminal_width':(.0236,'b (Inches Min)'),
           'terminal_length':(.0583,'family_projection_constraint:terminal_length'),
           'terminal_thickness':(.0059,'family_projection_constraint:terminal_thickness')}
    params=[]
    for name,value in values.items():
        data=dict(canonical_name=name,value=value,unit='count' if name.endswith('_count') else 'mm',
            evidence_ids=['human_input:previous:'+name],target_feature='operator_confirmed:'+name,evidence_kind='human_input',ocr_confidence=0,semantic_confidence=0)
        if name in wrong:
            data.update(value=wrong[name][0],target_feature=wrong[name][1],evidence_kind='dimension_line',
                evidence_ids=['ocr:'+name],token_ids=['ocr:'+name],token_bboxes=[(1,1,10,10)],ocr_confidence=.99,semantic_confidence=.99)
        params.append(FusedParameter(**data))
    conflict='negative_side_terminal_width:该侧首末中心跨度加宽度超过本体长度'
    fused=FusedEvidence(family_id='ic/gullwing_ic',category_id='ic',package_type='SOT223',
        parameters=params,identified_features=['molded_body','gullwing_lead','two_side_lead_array'],conflicting_fields=[conflict])
    return {'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path),
        'dimension_gate':{'passed':False,'missing_fields':[],'conflicting_fields':[conflict],'low_confidence_fields':list(wrong),'role_unconfirmed_fields':list(wrong)},
        'human_dimension_history':[{'question_id':'previous','values':{'negative_side_terminal_width':{'value':.82,'unit':'mm'}}}]}


@pytest.mark.parametrize('legacy',[True,False])
def test_span_width_body_conflict_exposes_every_operand(tmp_path,legacy):
    state=stalled_state(tmp_path)
    if not legacy:
        state['dimension_gate']['conflicting_fields']=['negative_side_pin_span+negative_side_terminal_width>body_length:该侧首末中心跨度加宽度超过本体长度']
    fields=human_nodes.actionable_dimension_fields(state,conflicts_only=True)
    assert set(fields)=={'body_length','negative_side_pin_span','negative_side_terminal_width'}


def test_correcting_source_values_escapes_repeated_width_question(tmp_path,monkeypatch):
    state=stalled_state(tmp_path);seen={}
    def answer(request):
        seen.update(request)
        return parse_dimension_answer(request,{'values':{
            'body_length':6.6,'terminal_width':.82,'terminal_thickness':.35,'terminal_length':1.16}})
    monkeypatch.setattr(human_nodes,'_ask',answer)
    updates=asyncio.run(human_nodes.ask_missing_dimensions_node(state))
    assert {'positive_side_pin_count','negative_side_pin_count','positive_side_terminal_length','negative_side_terminal_thickness'}<=set(seen['fields'])
    assert set(seen['fields'])>={'body_length','negative_side_pin_span','negative_side_terminal_width',
        'terminal_width','terminal_thickness','terminal_length'}
    assert all(seen['fields'][name]['source_review_required'] for name in ('body_length','terminal_width','terminal_thickness','terminal_length'))
    corrected={**state,**updates}
    corrected.update(asyncio.run(validate_dimension_chain_node(corrected)))
    corrected.update(asyncio.run(validate_dimensions_node(corrected)))
    assert corrected['dimension_gate']['passed']
    assert not corrected['fused_evidence']['conflicting_fields']
    indexed={p['canonical_name']:p for p in corrected['fused_evidence']['parameters']}
    assert indexed['negative_side_terminal_width']['value']==.82
    assert indexed['body_length']['value']==6.6 and indexed['body_length']['evidence_kind']=='human_input'
    assert corrected['human_dimension_history'][0]==state['human_dimension_history'][0]
    assert len(corrected['human_dimension_history'])==2
