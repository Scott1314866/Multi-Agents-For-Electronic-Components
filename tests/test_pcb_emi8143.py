"""Source-grounded EMI8143 regression: the result screenshot is never an input."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.agents.pcb import image_input as I
from backend.agents.pcb.contours import read_two_row_topology
from backend.agents.pcb.derive import check_clearances, derive_package
from backend.agents.pcb.human_nodes import validate_human_answer
from backend.agents.pcb.spec import PackageSpec

GOLDEN = Path(__file__).parent / 'golden/pcb/emi8143'


def source_reading():
    return I.ImageExtraction.model_validate(json.loads((GOLDEN / 'source_reading.json').read_text(encoding='utf-8'))['reading'])


def test_contours_verify_relative_topology_without_measuring_mm():
    topology = read_two_row_topology((GOLDEN / 'footprint.png').read_bytes())
    assert topology['columns'] == 8
    assert topology['bridged_columns'] == [2, 5]
    assert topology['wide_pad_alignments']['bottom_left'] == 'right'
    assert len(topology['top_boxes']) == len(topology['bottom_boxes']) == 8


def test_independent_rule_derives_pin1_and_all_16_lands_from_source_dimensions():
    reading = source_reading()
    candidate, issues = I.assess_extraction(reading)
    request = {'stage': 'image_parameters', 'options': ['submit', 'cancel'],
               'spec': candidate, 'grid': reading.grid.model_dump()}
    answer = validate_human_answer(request, {'action': 'submit', 'pad_layout_policy': 'independent'})
    spec = PackageSpec.model_validate(answer['spec'])
    assert (spec.body_x, spec.body_y, spec.body_height_max) == (3.5, 1.35, .5)
    assert spec.pin_count == 16 and spec.ep_present is False
    first = spec.pads[0]
    assert (first.number, first.x, first.y, first.size_x, first.size_y) == ('1', -1.55, -.45, .55, .65)
    assert all(p.size_x == .25 and p.size_y == .65 for p in spec.pads[1:])
    assert [p.number for p in spec.pads[8:]] == [str(i) for i in range(16, 8, -1)]
    assert all(p.y == .45 for p in spec.pads[8:])
    assert check_clearances(spec) == []
    assert any('连通' in issue for issue in issues)
    derived = derive_package(spec)
    assert derived.pin_pads['1'].mask == (.65, .75)
    assert derived.pin_pads['16'].mask == (.35, .75)


def test_source_bridges_are_preserved_until_an_explicit_split_choice():
    reading = source_reading()
    candidate, _ = I.assess_extraction(reading)
    by_number = {p['number']: p for p in candidate['pads']}
    assert by_number['3']['size_y'] == by_number['14']['size_y'] == 1.55
    assert by_number['3']['y'] == by_number['14']['y'] == 0
    assert by_number['6']['x'] == by_number['11']['x'] == .6
    assert any('3-14' in issue for issue in check_clearances(PackageSpec(**candidate)))


def test_split_policy_preserves_operator_numbers_when_source_only_labels_pin_one():
    from copy import deepcopy
    reading = source_reading()
    expected = [*reading.grid.bottom_numbers, *reading.grid.top_numbers]
    reading.grid.bottom_numbers = ['1', *([None] * 7)]
    reading.grid.top_numbers = [None] * 8
    candidate, _ = I.assess_extraction(reading)
    reviewed = deepcopy(candidate['pads'])
    for pad, number in zip(reviewed, expected):
        pad['number'] = number
    for pad in reviewed[1:]:
        pad['size_x'] = .2  # Operator manufacturing adjustment; source is .25.
    request = {'stage': 'image_parameters', 'options': ['submit', 'cancel'],
               'spec': candidate, 'grid': reading.grid.model_dump()}
    answer = validate_human_answer(request, {'action': 'submit',
        'spec': {'pads': reviewed, 'pin_numbering': expected}, 'pad_layout_policy': 'independent'})
    spec = PackageSpec.model_validate(answer['spec'])
    assert spec.pin_numbering == expected
    assert all(p.size_x == .2 for p in spec.pads[1:])
    assert spec.pads[0].size_x == .55
    assert all(p.size_y == .65 for p in spec.pads)
    assert all(p.y == -.45 for p in spec.pads[:8])
    assert all(p.y == .45 for p in spec.pads[8:])
    assert candidate['pads'][2]['number'] is None  # Source evidence stays partial.
    assert len(answer['manufacturing_adjustments']) == 15
    assert all(a['source_value'] == .25 and a['generated_value'] == .2
               for a in answer['manufacturing_adjustments'])
    assert reading.grid.standard_size_x == .25
    with pytest.raises(ValueError, match='行数'):
        validate_human_answer(request, {'action': 'submit',
            'spec': {'pads': reviewed[:-1]}, 'pad_layout_policy': 'independent'})


def test_unknown_numbers_produce_chinese_correction_prompt_without_fabrication():
    reading = source_reading()
    reading.grid.bottom_numbers = ['1', *([None] * 7)]
    reading.grid.top_numbers = [None] * 8
    candidate, _ = I.assess_extraction(reading)
    request = {'stage': 'image_parameters', 'options': ['submit', 'cancel'],
               'spec': candidate, 'grid': reading.grid.model_dump()}
    with pytest.raises(ValueError, match='焊盘脚号') as error:
        validate_human_answer(request, {'action': 'submit', 'pad_layout_policy': 'independent'})
    assert '15 行' in str(error.value)
    assert 'pydantic' not in str(error.value) and 'NoneType' not in str(error.value)


def test_nested_uncertainty_cannot_survive_as_an_invented_dimension():
    reading = source_reading()
    reading.uncertain_fields = ['pads[0].size_x']
    candidate, issues = I.assess_extraction(reading)
    assert candidate['pads'][0]['size_x'] is None
    assert reading.spec.get('pads') is None  # The audit source was not mutated.
    assert any('pads[0].size_x' in issue for issue in issues)


def test_actual_reading_keeps_true_dimensions_and_requires_unknown_ep_confirmation():
    raw = json.loads((GOLDEN / 'real_reading.json').read_text(encoding='utf-8'))
    assert raw['reading_source'] == 'real_model_with_contour_verification'
    reading = I.ImageExtraction.model_validate(raw['reading'])
    candidate, issues = I.assess_extraction(reading)
    assert candidate['pads'][0]['x'] == -1.55
    assert candidate['pads'][0]['size_x'] == .55
    assert candidate['pin_numbering'][:8] == [str(i) for i in range(1, 9)]
    assert any('ep_present' in x for x in issues)
    assert reading.contour_topology['wide_pad_alignments']['bottom_left'] == 'right'


def test_primary_geometry_uses_source_corner_ids_instead_of_bottom_view_frame():
    reading = source_reading()
    reading.grid.top_numbers = [str(i) for i in range(1, 9)]
    reading.grid.bottom_numbers = [str(i) for i in range(16, 8, -1)]
    geometry = I.FootprintGeometryReading(columns=8, pitch_x=.4, standard_size_x=.25,
        standard_size_y=.65, outer_span_y=1.55, pin1_corner='bottom_left', pin1_size_x=.55,
        pin1_alignment='right', bridged_columns=[2, 5], evidence='source dimensions', confidence=1, source_units='mm')
    result = I._apply_primary_geometry(reading, geometry)
    assert result.grid.bottom_numbers == [str(i) for i in range(1, 9)]
    assert result.grid.top_numbers == [str(i) for i in range(16, 8, -1)]


def test_schema_shaped_values_are_unwrapped_but_schema_descriptors_are_rejected():
    values = {'columns': 8, 'pitch_x': .4, 'standard_size_x': .25, 'standard_size_y': .65,
              'outer_span_y': 1.55, 'pin1_corner': 'bottom_left', 'evidence': 'labels', 'confidence': .95}
    assert I.FootprintGeometryReading.model_validate({'properties': values}).columns == 8
    with pytest.raises(ValueError):
        I.FootprintGeometryReading.model_validate({'properties': {'columns': {'type': 'integer'}}})


@pytest.mark.parametrize('stage', ['build', 'verify', 'props'])
def test_remote_command_uses_actual_non_graphic_switch_and_package_mode(monkeypatch, tmp_path, stage):
    from backend.agents.pcb import runner
    (tmp_path / f'{stage}_xdfn16.il').write_text('; test', encoding='utf-8')
    commands = []
    monkeypatch.setattr(runner, 'resolve_allegro_exe', lambda *_: Path('allegro.exe'))
    class Completed:
        returncode = 0
        stdout = b''
        stderr = b''
    def run(command, **kwargs):
        commands.append(command)
        return Completed()
    monkeypatch.setattr(runner.subprocess, 'run', run)
    runner.run_stage(tmp_path, 'XDFN16', stage=stage)
    command = commands[0]
    assert '-nographic' in command and '-noopengl' in command and '-sq' not in command
    assert command[command.index('-symbol') + 1] == 'package'


def test_two_channel_reading_corrects_only_topology_and_retains_metadata_evidence(monkeypatch):
    geometry = dict(columns=8, pitch_x=.4, standard_size_x=.25, standard_size_y=.65,
        outer_span_y=1.55, pin1_corner='bottom_left', pin1_size_x=.55,
        pin1_alignment='left', bridged_columns=[3, 4], evidence='dimension labels', confidence=.95, source_units='mm')
    metadata = dict(name='XDFN16 3.5x1.35, 0.4P', family='CASE 711AW', body_x=3.5,
        body_y=1.35, body_height_max=.5, pin_count=16, source_top_numbers=['1', '8'],
        source_bottom_numbers=['16', '9'], ep_present=False, evidence='D/E/A table and bottom corner IDs', confidence=.95, source_units='mm')
    class Model:
        def __init__(self, schema):
            self.schema = schema
        async def ainvoke(self, messages, **kwargs):
            assert kwargs['extra_body'] == {'enable_thinking': False}
            values = geometry if self.schema is I.FootprintGeometryReading else metadata
            return self.schema.model_validate({'properties': values})
    monkeypatch.setattr(I, 'get_structured_llm', lambda _, schema: Model(schema))
    reading = asyncio.run(I.extract_image(str(GOLDEN / 'footprint.png'), context_images=[str(GOLDEN / 'footprint.png')]))
    assert reading.geometry_reading.pin1_alignment == 'right'
    assert reading.grid.bridged_columns == [2, 5]
    assert reading.grid.bottom_numbers == [str(i) for i in range(1, 9)]
    assert reading.package_metadata.source_top_numbers == ['1', '8']
    candidate, _ = I.assess_extraction(reading)
    assert candidate['pads'][0]['x'] == -1.55 and candidate['family'] == 'XDFN'


def test_missing_numbering_retains_known_pin1_geometry():
    reading = source_reading()
    reading.grid.top_numbers = [None] * 8
    reading.grid.bottom_numbers = [None] * 8
    geometry = I.FootprintGeometryReading(columns=8, pitch_x=.4, standard_size_x=.25,
        standard_size_y=.65, outer_span_y=1.55, pin1_corner='bottom_left', pin1_size_x=.55,
        pin1_alignment='right', evidence='primary pin 1', confidence=.95, source_units='mm')
    reading = I._apply_primary_geometry(reading, geometry)
    candidate, issues = I.assess_extraction(reading)
    assert candidate['pads'][0]['number'] == '1' and candidate['pads'][0]['size_x'] == .55
    assert candidate['pads'][1]['number'] is None
    assert any('pin_numbering' in x for x in issues)


def test_every_emitted_stage_initializes_the_log_variable_it_uses(tmp_path):
    import re
    from backend.agents.pcb.skill_emitter import write_skill_package
    reading = source_reading()
    candidate, _ = I.assess_extraction(reading)
    answer = validate_human_answer({'stage': 'image_parameters', 'spec': candidate, 'grid': reading.grid.model_dump()},
                                   {'action': 'submit', 'pad_layout_policy': 'independent'})
    spec = PackageSpec.model_validate(answer['spec'])
    files = write_skill_package(spec, derive_package(spec), tmp_path)
    for stage in ('build_il', 'verify_il', 'props_il'):
        text = files[stage].read_text(encoding='utf-8')
        log_vars = re.findall(r'outfile\((\w+) "a"\)', text)
        assert log_vars
        assert all(re.search(r'^' + variable + r'\s*=\s*strcat\(', text, re.M) for variable in log_vars)


def test_real_allegro_logs_pass_all_geometry_and_persistence_assertions():
    from backend.agents.pcb.assertions import judge
    from backend.agents.pcb.parser import parse_build_log, parse_verify_log, parse_props_log, read_logs
    root = GOLDEN / 'live'
    spec = PackageSpec.model_validate(json.loads((root / 'package_spec.json').read_text(encoding='utf-8')))
    logs = read_logs(root, spec.name)
    verdict = judge(spec, derive_package(spec), build=parse_build_log(logs['build']),
                    verify=parse_verify_log(logs['verify']), props=parse_props_log(logs['props']), symbol_log=logs['symbol'])
    assert verdict.passed, verdict.to_text()
    assert len(verdict.to_dict()['assertions']) == 53
