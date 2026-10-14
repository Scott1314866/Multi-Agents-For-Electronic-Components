"""Replay a real image reading, apply an explicit split rule, and test Allegro.

The reference screenshot is never an input to extraction or geometry generation.
Use --ssh only with the already configured Cadence Windows SSH host.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.agents.pcb.derive import check_clearances
from backend.agents.pcb.graph import build_pcb_graph
from backend.agents.pcb.image_input import ImageExtraction, assess_extraction
from backend.agents.pcb.spec import PackageSpec


async def replay_reading(reading: ImageExtraction, out_dir: Path, confirmed_spec: dict | None = None,
                         execution_mode: str | None = None) -> dict:
    candidate, issues = assess_extraction(reading)
    report = out_dir / 'image_reading.json'
    report.write_text(json.dumps(reading.model_dump(), ensure_ascii=False, indent=2), encoding='utf-8')
    state = {'task': 'generate_pcb_package', 'mode': 'full' if execution_mode else 'emit_only',
             'execution_mode': execution_mode, 'input_kind': 'image',
             'work_dir': str(out_dir.resolve()), 'spec': candidate, 'image_reading': reading.model_dump(),
             'extraction_issues': issues, 'extraction_report_path': str(report.resolve()), 'errors': []}
    graph = build_pcb_graph(InMemorySaver())
    config = {'configurable': {'thread_id': 'emi8143-' + uuid.uuid4().hex}}
    # Replay the recorded source with its provenance; never label a manually
    # checked fixture as a successful vision-model result.
    await graph.aupdate_state(config, state, as_node='extract_image')
    pending = await graph.ainvoke(None, config)
    interrupt = pending['__interrupt__'][0]
    answer = {'action': 'submit', 'pad_layout_policy': 'independent',
              'spec': confirmed_spec or {},
              'comment': '用户明确选择16个独立焊盘；按图纸普通焊盘长度拆分连通地焊盘，参考结果截图不提供尺寸。'}
    result = await graph.ainvoke(Command(resume={interrupt.id: answer}), config)
    expected_status = 'judged' if execution_mode else 'skills_ready'
    if result['status'] != expected_status:
        raise ValueError('Image reading is not ready for generation: ' + str(result.get('errors') or result.get('result')))
    return result


def execution_report(state: dict) -> dict:
    """Report the normal graph's native run, left at its review interrupt."""
    root = Path(state['work_dir'])
    native = []
    for path in sorted(root.iterdir()):
        if path.suffix.lower() in {'.dra', '.psm', '.pad'}:
            content = path.read_bytes()
            native.append({'name': path.name, 'size': len(content),
                           'sha256': hashlib.sha256(content).hexdigest()})
    runs = [{'stage': state[key]['stage'], 'exit_code': state[key]['returncode'], 'timed_out': False}
            for key in ('build_run', 'verify_run', 'props_run')]
    return {'runs': runs, 'native_files': native, 'verdict': state['verdict'],
            'remote_dir': state['execution']['remote_dir'], 'local_dir': str(root.resolve()),
            'passed': state['verdict']['passed'] and all(r['exit_code'] == 0 for r in runs),
            'workflow': 'normal_pcb_graph', 'human_review_pending': True}


def package_verified_native(out_dir: Path, execution: dict) -> Path:
    if (not execution['verdict']['passed'] or [r['stage'] for r in execution['runs']] != ['build', 'verify', 'props']
            or any(r['exit_code'] != 0 or r['timed_out'] for r in execution['runs'])):
        raise ValueError('Only fully verified native artifacts may be packaged')
    root = Path(execution['local_dir'])
    manifest = {'units': 'millimeters', 'source': 'EMI8143MUTAG datasheet page 9, CASE 711AW',
                'reference_image_used_as_input': False,
                'policy': 'User selected 16 independent lands; source ground bridges split by standard short-land length',
                'assertions_passed': len(execution['verdict']['assertions']), 'files': execution['native_files']}
    output = out_dir / 'EMI8143_Allegro_package.zip'
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for record in execution['native_files']:
            path = root / record['name']
            content = path.read_bytes()
            if len(content) != record['size'] or hashlib.sha256(content).hexdigest() != record['sha256']:
                raise ValueError('Native artifact checksum mismatch: ' + record['name'])
            archive.writestr(path.name, content)
        archive.write(out_dir / 'package_spec.json', 'package_spec.json')
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reading', type=Path, default=ROOT / 'output/pcb_agent/emi8143/vision_calibrated_reading.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/pcb_agent/emi8143/generated')
    parser.add_argument('--ssh', action='store_true')
    parser.add_argument('--confirmed-spec', type=Path, help='Explicit source-grounded supplementary fields; recorded as human-review input')
    args = parser.parse_args()
    payload = json.loads(args.reading.read_text(encoding='utf-8'))
    reading = ImageExtraction.model_validate(payload['reading'])
    args.output.mkdir(parents=True, exist_ok=True)
    confirmed = json.loads(args.confirmed_spec.read_text(encoding='utf-8')) if args.confirmed_spec else {}
    state = asyncio.run(replay_reading(reading, args.output, confirmed, 'ssh' if args.ssh else None))
    spec = PackageSpec.model_validate(state['spec'])
    (args.output / 'package_spec.json').write_text(spec.model_dump_json(indent=2), encoding='utf-8')
    report = {'source_reading': str(args.reading.resolve()), 'vision_seconds': payload.get('seconds'),
              'mode': payload.get('reading_source', 'real_model_reading') + '_replay',
              'confirmed_spec': confirmed,
              'reference_image_used_as_input': False,
              'policy': '16 independent pads, split source bridges using standard land length',
              'spec': spec.model_dump(), 'clearance_issues': check_clearances(spec),
              'status': state['status'], 'artifacts': state['artifact_paths']}
    if args.ssh:
        report['remote_execution'] = execution_report(state)
        report['native_artifacts_accepted'] = report['remote_execution']['passed']
        if report['native_artifacts_accepted']:
            report['native_package'] = str(package_verified_native(args.output, report['remote_execution']).resolve())
    path = args.output / 'test_report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': report['status'], 'pin_count': spec.pin_count,
                      'issues': report['clearance_issues'],
                      'remote_passed': report.get('remote_execution', {}).get('verdict', {}).get('passed'),
                      'report': str(path)}, ensure_ascii=True))


if __name__ == '__main__':
    main()
