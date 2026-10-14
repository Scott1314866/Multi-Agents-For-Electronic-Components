"""9534bf02: 3 narrow leads and 1 wide tab must not become a uniform 2+2."""
import asyncio
from copy import deepcopy
import pytest
import cadquery as cq
from backend.agents.step.families.ic import gullwing_ic
from backend.agents.step.features.gullwing_layout import ROW_PARAMETERS, ROW_WIDTH_PARAMETERS
from backend.agents.step.features.gullwing_verification import verify_gullwing_rows
from backend.agents.step.ir.executor import build_feature_model
from backend.agents.step.image_nodes import validate_dimensions_node,validate_dimension_chain_node,verify_step_node
from backend.agents.step.human_nodes import actionable_dimension_fields
from backend.agents.step.families.registry import derive_family_parameters
from tests.test_step_odd_gullwing import five
from backend.agents.step.vision.schemas import QwenViewClassificationResult
from backend.agents.step.families.registry import reconcile_image_family_classification


def sot(**changes):
    values=dict(nominal_pin_count=4,terminal_pitch=2.3,pin_span=4.6,
        total_height=1.8,housing_height=1.65,body_standoff=.15,overall_width=7.3,
        body_width=3.7,body_length=6.6,terminal_length=1.16,terminal_thickness=.35,terminal_width=.82,
        negative_side_pin_count=3,positive_side_pin_count=1,negative_side_pin_span=4.6,positive_side_pin_span=0,
        negative_side_terminal_width=.82,positive_side_terminal_width=3.1)
    values.update(changes)
    return five(**values).model_copy(update={'package_type':'SOT223'})


def lead_parameters(ir):
    feature=next(f for f in ir['features'] if f['feature_type']=='gullwing_lead_array')
    return {k:v['value'] for k,v in feature['parameters'].items()}


def test_sot223_step_roundtrip_preserves_3_plus_1_and_independent_widths(tmp_path):
    ir=gullwing_ic.plan_from_evidence(sot(),source_image_sha256='a'*64)
    model=build_feature_model(cq,ir);path=tmp_path/'sot223.step';cq.exporters.export(model,str(path))
    shape=cq.importers.importStep(str(path)).val()
    result=verify_gullwing_rows(shape,lead_parameters(ir),require_explicit=True)
    assert result['passed'] and len(shape.Solids())==5
    assert result['rows'][0]['actual_positions']==pytest.approx([-2.3,0,2.3])
    assert result['rows'][0]['actual_widths']==pytest.approx([.82]*3)
    assert result['rows'][1]['actual_positions']==pytest.approx([0])
    assert result['rows'][1]['actual_widths']==pytest.approx([3.1])
    bbox=shape.BoundingBox()
    assert [bbox.xlen,bbox.ylen,bbox.zlen]==pytest.approx([6.6,7.3,1.8])
    verified=asyncio.run(verify_step_node({'feature_ir':ir,'model_source':'local_feature_ir','artifact_paths':{'step':str(path)}}))
    assert verified['verification']['gullwing_rows']['passed']


@pytest.mark.parametrize('wrong_rows',[True,False])
def test_old_bbox_and_solid_count_cannot_hide_wrong_rows_or_widths(tmp_path,wrong_rows):
    expected=gullwing_ic.plan_from_evidence(sot(),source_image_sha256='a'*64)
    wrong=deepcopy(expected);feature=wrong['features'][1]['parameters']
    for name in ROW_WIDTH_PARAMETERS:feature.pop(name)
    feature['lead_width']['value']=.82
    if wrong_rows:
        for name in ROW_PARAMETERS:feature.pop(name)
        feature['pitch']['value']=4.6
    model=build_feature_model(cq,wrong);path=tmp_path/'wrong.step';cq.exporters.export(model,str(path))
    shape=cq.importers.importStep(str(path)).val()
    assert len(shape.Solids())==5
    assert [shape.BoundingBox().xlen,shape.BoundingBox().ylen,shape.BoundingBox().zlen]==pytest.approx([6.6,7.3,1.8])
    result=asyncio.run(verify_step_node({'feature_ir':expected,'model_source':'local_feature_ir','artifact_paths':{'step':str(path)}}))
    assert not result['verification']['passed']
    assert any('数量' in s if wrong_rows else '宽度' in s for s in result['verification']['gullwing_rows']['issues'])


def test_even_sot_requires_source_rows_and_widths_instead_of_half_total(tmp_path):
    fused=sot(**{name:None for name in (*ROW_PARAMETERS,*ROW_WIDTH_PARAMETERS)})
    gate=asyncio.run(validate_dimensions_node({'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}))
    assert not gate['dimension_gate']['passed']
    assert set(actionable_dimension_fields({'fused_evidence':fused.model_dump(),**gate}))==set((*ROW_PARAMETERS,*ROW_WIDTH_PARAMETERS))
    with pytest.raises(ValueError,match='关键参数'):gullwing_ic.plan_from_evidence(fused,source_image_sha256='a'*64)
    derived=derive_family_parameters(fused.model_copy(update={'parameters':[p for p in fused.parameters if p.canonical_name!='pin_span']}))
    assert not any(p.canonical_name=='pin_span' for p in derived.parameters)


def test_sot223_span_is_not_adjacent_pitch_and_wide_tab_is_not_added_to_long_row(tmp_path):
    state=asyncio.run(validate_dimension_chain_node({'fused_evidence':sot().model_dump(),'output_dir':str(tmp_path)}))
    assert not state['fused_evidence']['conflicting_fields']
    wrong=asyncio.run(validate_dimension_chain_node({'fused_evidence':sot(terminal_pitch=4.6).model_dump(),'output_dir':str(tmp_path)}))
    assert wrong['fused_evidence']['conflicting_fields']


def test_even_sot_numeric_inch_column_role_guesses_need_review(tmp_path):
    fused=sot();params=[]
    for p in fused.parameters:
        if p.canonical_name in {'terminal_length','terminal_thickness'}:
            p=p.model_copy(update={'value':.0583 if p.canonical_name=='terminal_length' else .0059,
                'evidence_kind':'dimension_line','target_feature':'family_constraint:'+p.canonical_name,
                'evidence_ids':['wrong-inch-column'],'token_ids':['wrong-inch-column'],'token_bboxes':[(1,1,10,10)],
                'ocr_confidence':.99,'semantic_confidence':.9})
        params.append(p)
    fused=fused.model_copy(update={'parameters':params})
    gate=asyncio.run(validate_dimensions_node({'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}))
    assert not gate['dimension_gate']['passed']
    assert {'terminal_length','terminal_thickness'}<=set(actionable_dimension_fields({'fused_evidence':fused.model_dump(),**gate}))


@pytest.mark.parametrize('change',[{'negative_side_terminal_width':2.3},{'positive_side_terminal_width':7},{'negative_side_terminal_width':-.1}])
def test_invalid_independent_widths_cannot_generate(change):
    with pytest.raises(ValueError):gullwing_ic.plan_from_evidence(sot(**change),source_image_sha256='a'*64)


@pytest.mark.parametrize('text,confidence,corrected',[('SOT223',.99,True),('SOT223',.7,False),('SOIC8',.99,False)])
def test_swapped_package_and_template_fields_only_repaired_with_matching_identity(text,confidence,corrected):
    raw=QwenViewClassificationResult(category_id='ic',family_id='SOT223',package_type='ic/gullwing_ic',
        identified_views={'region_001':'composite_outline'},identified_features=[],unresolved_fields=[],ambiguities=[],overall_confidence=.91)
    result=reconcile_image_family_classification(raw,[{'text':text,'confidence':confidence}])
    assert (result.family_id=='ic/gullwing_ic')==corrected
    if corrected:
        assert result.package_type=='SOT223' and result.overall_confidence==.91


def test_live_projection_fallback_and_inches_marked_as_mm_are_all_reviewed(tmp_path):
    fused=sot();params=[]
    wrong={'body_length':(.2598,'D (Inches Max)'),
           'housing_height':(.0374,'A3 (Inches Min/Max)'),
           'terminal_width':(.0236,'b (Inches Min)'),
           'overall_width':(.2784,'E (Inches Max)'),
           'terminal_length':(.0583,'family_projection_constraint:terminal_length'),
           'terminal_thickness':(.0059,'family_projection_constraint:terminal_thickness'),
           'total_height':(1.45,'family_projection_constraint:total_height'),
           'body_standoff':(.2,'family_projection_constraint:body_standoff')}
    for p in fused.parameters:
        if p.canonical_name in wrong:
            value,target=wrong[p.canonical_name]
            p=p.model_copy(update={'value':value,'target_feature':target,'evidence_kind':'dimension_line',
                'token_ids':['source-token'],'token_bboxes':[(1,1,10,10)],'evidence_ids':['source-token'],
                'ocr_confidence':.99,'semantic_confidence':.99})
        params.append(p)
    fused=fused.model_copy(update={'parameters':params})
    gate=asyncio.run(validate_dimensions_node({'fused_evidence':fused.model_dump(),'output_dir':str(tmp_path)}))
    assert not gate['dimension_gate']['passed']
    assert set(wrong)<=set(actionable_dimension_fields({'fused_evidence':fused.model_dump(),**gate}))
    assert set(gate['dimension_gate']['unit_unconfirmed_fields'])=={'body_length','housing_height','terminal_width','overall_width'}
