"""Regression for the two mechanical-only drawings reported on 2026-10-12."""
import pytest
import asyncio
from pathlib import Path
from backend.agents.pcb.mechanical_outline import MechanicalOutlineReading, derive_max_outline
from backend.agents.pcb.image_input import ImageExtraction, assess_extraction
from backend.agents.pcb.clarification import plan_questions


def sot223():
    values={'D':(6.2,6.6), 'D1':(2.9,3.1), 'E':(6.7,7.3), 'E1':(3.3,3.7),
            'A':(1.48,1.8), 'b':(.6,.82), 'L':(.76,1.16)}
    return MechanicalOutlineReading(dimensions={**{k:dict(minimum=a, maximum=b,evidence=f'{k} mm MIN {a} MAX {b}') for k,(a,b) in values.items()},
          'e':dict(typical=2.3,evidence='e=2.3 TYP mm')},
          body_x_symbol='D',body_y_symbol='E1',overall_y_symbol='E',source_units='mm',
          centered_rows=True,evidence='3 bottom terminals + 1 centered wide tab; unknown pin IDs',confidence=.95,
          terminals=[dict(side='bottom',pitch_index=i,width_symbol='b',length_symbol='L') for i in (-1,0,1)]
                    +[dict(side='top',pitch_index=0,width_symbol='D1')])


def five_pin():
    values={'E':(2.6,3.),'E1':(1.5,1.72),'D':(2.82,3.02),'A':(1.05,1.25),'b':(.28,.5),'L':(.3,.6)}
    return MechanicalOutlineReading(dimensions={**{k:dict(minimum=a,maximum=b,evidence=f'{k} mm MIN {a} MAX {b}') for k,(a,b) in values.items()},
          'e':dict(typical=.95,evidence='e=0.95 BSC mm')},
          body_x_symbol='D',body_y_symbol='E1',overall_y_symbol='E',source_units='mm',
          centered_rows=True,evidence='2 top and 3 bottom rectangular terminals; unknown IDs',confidence=.95,
          terminals=[dict(side=side,pitch_index=i,width_symbol='b',length_symbol='L')
                     for side,indices in [('bottom',(-1,0,1)),('top',(-1,1))] for i in indices])


def test_sot223_keeps_body_span_tab_and_pitch_distinct():
    spec,audit=derive_max_outline(sot223())
    assert (spec['body_x'],spec['body_y'],spec['body_height_max'])==(6.6,3.7,1.8)
    assert spec['pin_count']==4 and spec['pin_numbering']==[None]*4
    assert [p['x'] for p in spec['pads'][:3]]==[-2.3,0,2.3]
    assert spec['pads'][0]['size_x']==.82 and spec['pads'][0]['size_y']==1.16
    assert spec['pads'][0]['y']==-3.07
    assert spec['pads'][3]['size_x']==3.1 and spec['pads'][3]['size_y']==1.8
    assert spec['pads'][3]['y']==2.75
    assert '(E.MAX-E1.MAX)/2=1.8 (projection rule)' in audit


def test_five_pin_uses_dimension_arrows_and_can_rotate_requested_rows():
    outline=five_pin()
    spec,_=derive_max_outline(outline)
    assert (spec['body_x'],spec['body_y'],spec['body_height_max'])==(3.02,1.72,1.25)
    assert spec['pin_count']==5
    assert [p['y'] for p in spec['pads']]==[-1.2]*3+[1.2]*2
    outline.rotation_deg=-90
    rotated,_=derive_max_outline(outline)
    assert (rotated['body_x'],rotated['body_y'])==(1.72,3.02)
    assert [p['x'] for p in rotated['pads']]==[-1.2]*3+[1.2]*2
    assert all(p['size_x']==.6 and p['size_y']==.5 for p in rotated['pads'])


@pytest.mark.parametrize('mutation', [dict(source_units='unknown'),dict(centered_rows=False),dict(body_y_symbol='E')])
def test_ambiguous_mapping_units_or_row_offsets_cannot_generate(mutation):
    with pytest.raises(ValueError):derive_max_outline(sot223().model_copy(update=mutation))


def test_missing_max_not_replaced_with_midpoint_or_typical():
    outline=sot223()
    outline.dimensions['b'].maximum=None
    outline.dimensions['b'].typical=.71
    with pytest.raises(ValueError,match='b'):derive_max_outline(outline)


def test_assessment_keeps_projection_audit_and_requests_unknown_ids_and_view():
    reading=ImageExtraction(spec={'name':'SOT223','family':'SOT223','ep_present':False},
          source_units='unknown',evidence={},mechanical_outline=sot223())
    candidate,issues=assess_extraction(reading)
    assert len(candidate['pads'])==4 and all(p['number'] is None for p in candidate['pads'])
    assert reading.source_units=='mm' and 'projection rule' in reading.evidence['pads'].text
    assert any('MAX' in issue for issue in issues)
    questions=plan_questions({'spec':candidate,'image_reading':reading.model_dump()})
    assert {'pin_numbers','view'} <= {q['id'] for q in questions}


def test_attached_outlines_keep_asymmetric_terminal_counts_and_wide_tab():
    from backend.agents.pcb.outline_topology import read_outline_rows
    root=Path(__file__).parent/'golden/pcb/mechanical_outline'
    sot=read_outline_rows((root/'sot223-outline.png').read_bytes())
    five=read_outline_rows((root/'five-pin-outline.png').read_bytes())
    assert sot['rows']=={'top':[{'pitch_index':0,'wide':True}],
        'bottom':[{'pitch_index':i,'wide':False} for i in (-1,0,1)]}
    assert five['rows']=={'top':[{'pitch_index':i,'wide':False} for i in (-1,1)],
        'bottom':[{'pitch_index':i,'wide':False} for i in (-1,0,1)]}
    assert read_outline_rows((root.parent/'recommended_six_pad/footprint.png').read_bytes()) is None


def test_primary_topology_rejects_model_two_by_two_reinterpretation(monkeypatch):
    from backend.agents.pcb import image_input as I
    from backend.agents.pcb.outline_topology import read_outline_rows
    import base64
    source=Path(__file__).parent/'golden/pcb/mechanical_outline/sot223-outline.png'
    topology=read_outline_rows(source.read_bytes())
    checked=[]
    async def invoke(schema,messages,budget):
        if schema.__name__=='PinReferenceReading':
            return schema(view='unknown',rows=[],confidence=0)
        wrong=sot223().model_dump()
        wrong['terminals']=[dict(side=side,pitch_index=i,width_symbol='b',length_symbol='L') for side in ('top','bottom') for i in (-1,1)]
        with pytest.raises(ValueError,match='拓扑'):schema.model_validate(wrong)
        wrong=sot223().model_dump();wrong['terminals'][-1]['width_symbol']='b'
        with pytest.raises(ValueError,match='宽端子'):schema.model_validate(wrong)
        checked.append(True)
        return schema.model_validate(sot223().model_dump())
    monkeypatch.setattr(I,'_invoke_reading',invoke)
    content=[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(p.read_bytes()).decode()}} for p in (source,source.with_name('sot223-dimensions.png'))]
    result=asyncio.run(I._extract_max_outline(content,'按MAX派生',topology))
    assert checked and len(result.mechanical_outline.terminals)==4


def test_dimension_table_guard_distinguishes_supplied_tables_from_geometry():
    from backend.agents.pcb.outline_topology import has_dimension_table
    root=Path(__file__).parent/'golden/pcb/mechanical_outline'
    assert all(has_dimension_table((root/p).read_bytes()) for p in ('sot223-dimensions.png','five-pin-dimensions.png'))
    assert not any(has_dimension_table((root/p).read_bytes()) for p in ('sot223-outline.png','five-pin-outline.png','five-pin-top-view.png'))


def test_outline_without_dimension_table_preserves_topology_without_guessing(monkeypatch):
    from backend.agents.pcb import image_input as I
    from backend.agents.pcb.outline_topology import read_outline_rows
    import base64
    source=Path(__file__).parent/'golden/pcb/mechanical_outline/five-pin-outline.png'
    async def forbidden(*args):raise AssertionError('No numeric source: model must not guess MAX values')
    monkeypatch.setattr(I,'_invoke_reading',forbidden)
    content=[{'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(source.read_bytes()).decode()}}]
    result=asyncio.run(I._extract_max_outline(content,'按MAX派生',read_outline_rows(source.read_bytes())))
    assert result.spec['pin_count']==5 and len(result.spec['pads'])==5
    assert all(p['x'] is None and p['size_x'] is None for p in result.spec['pads'])
