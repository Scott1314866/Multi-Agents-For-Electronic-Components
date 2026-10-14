"""Upper and lower rows must use their own count, width, foot length and thickness."""
import asyncio
import cadquery as cq
import pytest
from backend.agents.step.families.ic import gullwing_ic
from backend.agents.step.families.registry import derive_family_parameters
from backend.agents.step.features.gullwing_layout import ROW_LENGTH_PARAMETERS,ROW_THICKNESS_PARAMETERS
from backend.agents.step.features.gullwing_verification import verify_gullwing_rows
from backend.agents.step.human_nodes import _dimension_question,actionable_dimension_fields
from backend.agents.step.dimension_input import parse_dimension_answer
from backend.agents.step.image_nodes import validate_dimensions_node,validate_dimension_chain_node
from backend.agents.step.ir.executor import build_feature_model
from tests.test_step_sot223_asymmetric import sot,lead_parameters


def separate_profiles(**changes):
    values=dict(negative_side_terminal_length=1.16,positive_side_terminal_length=.8,
        negative_side_terminal_thickness=.35,positive_side_terminal_thickness=.25)
    values.update(changes)
    return sot(**values)


def test_upper_and_lower_profile_dimensions_survive_native_step_readback(tmp_path):
    ir=gullwing_ic.plan_from_evidence(separate_profiles(),source_image_sha256='a'*64)
    model=build_feature_model(cq,ir);path=tmp_path/'independent.step';cq.exporters.export(model,str(path))
    shape=cq.importers.importStep(str(path)).val();params=lead_parameters(ir)
    readback=verify_gullwing_rows(shape,params,require_explicit=True)
    assert readback['passed'],readback
    assert readback['rows'][0]['actual_thicknesses']==pytest.approx([.35]*3,abs=.001)
    assert readback['rows'][1]['actual_thicknesses']==pytest.approx([.25],abs=.001)
    assert readback['rows'][0]['actual_foot_lengths']==pytest.approx([1.16]*3,abs=.05)
    assert readback['rows'][1]['actual_foot_lengths']==pytest.approx([.8],abs=.05)
    old=build_feature_model(cq,gullwing_ic.plan_from_evidence(sot(),source_image_sha256='a'*64))
    rejected=verify_gullwing_rows(old,params,require_explicit=True)
    assert not rejected['passed'] and any('上侧' in issue and '厚度' in issue for issue in rejected['issues'])


def test_side_values_supply_audited_defaults_and_total_without_hiding_independent_values():
    fused=separate_profiles();removed={'nominal_pin_count','terminal_length','terminal_thickness','terminal_width'}
    fused=fused.model_copy(update={'parameters':[p for p in fused.parameters if p.canonical_name not in removed]})
    derived=derive_family_parameters(fused);indexed={p.canonical_name:p for p in derived.parameters}
    assert indexed['nominal_pin_count'].value==4 and indexed['nominal_pin_count'].unit=='count'
    ir=gullwing_ic.plan_from_evidence(derived,source_image_sha256='a'*64);params=lead_parameters(ir)
    assert params['positive_side_terminal_thickness']==.25 and params['negative_side_terminal_thickness']==.35
    assert params['positive_side_terminal_length']==.8 and params['negative_side_terminal_length']==1.16


@pytest.mark.parametrize('pair',[ROW_LENGTH_PARAMETERS,ROW_THICKNESS_PARAMETERS])
def test_partial_independent_profile_requires_the_other_side(pair,tmp_path):
    fused=separate_profiles().model_copy(update={'parameters':[p for p in separate_profiles().parameters if p.canonical_name!=pair[0]]})
    state={'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}
    state.update(asyncio.run(validate_dimensions_node(state)))
    assert not state['dimension_gate']['passed'] and pair[0] in actionable_dimension_fields(state)


def test_dimension_question_keeps_both_rows_and_accepts_chinese_side_names():
    state={'fused_evidence':sot().model_dump(),'dimension_gate':{'missing_fields':['body_length']}}
    q=_dimension_question(state,['body_length'],'review')
    assert q['side_groups'] and q['fields']['positive_side_terminal_thickness']['group']=='upper'
    assert q['fields']['negative_side_pin_count']['group']=='lower'
    assert q['fields']['positive_side_pin_span']['observed_candidates'][0]['value']==0
    answer=parse_dimension_answer(q,{'text':'上侧引脚数量=1个；下侧引脚数量=3个；上侧引脚金属厚度=0.25mm；下侧引脚金属厚度=0.35mm'})
    assert answer['values']['positive_side_pin_count']['value']==1 and answer['values']['negative_side_pin_count']['value']==3
    assert answer['values']['positive_side_terminal_thickness']['value']==.25


@pytest.mark.parametrize('length',[3.1,.2])
def test_invalid_upper_profile_returns_all_related_dimensions_for_review(length,tmp_path):
    state={'fused_evidence':separate_profiles(positive_side_terminal_length=length).model_dump(),'output_dir':str(tmp_path)}
    state.update(asyncio.run(validate_dimension_chain_node(state)))
    state.update(asyncio.run(validate_dimensions_node(state)))
    assert not state['dimension_gate']['passed']
    assert {'positive_side_terminal_length','positive_side_terminal_thickness','body_width','overall_width'}<=set(actionable_dimension_fields(state))


def test_separate_soic_profiles_also_require_explicit_row_counts_and_widths(tmp_path):
    removed={'negative_side_pin_count','positive_side_pin_count','negative_side_pin_span','positive_side_pin_span',
        'negative_side_terminal_width','positive_side_terminal_width'}
    fused=separate_profiles().model_copy(update={'package_type':'SOIC','parameters':[p for p in separate_profiles().parameters if p.canonical_name not in removed]})
    state={'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}
    state.update(asyncio.run(validate_dimensions_node(state)))
    assert not state['dimension_gate']['passed'] and removed<=set(actionable_dimension_fields(state))
