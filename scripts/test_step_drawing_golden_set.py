"""运行二维工程图 → Feature IR → STEP 的 Golden Set 样本。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.agents.step.drawing import DEFAULT_GOLDEN_SET_PATH, load_golden_cases
from backend.agents.step.graph import build_drawing_to_step_graph


DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "drawing_to_step" / "golden_set"


async def run_case(
    graph: Any,
    case: dict[str, Any],
    output_root: Path,
    extraction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """运行一个 Golden Set 用例并整理结果。"""
    started = time.perf_counter()
    case_output = output_root / case["case_id"]
    input_state = {
            "golden_case_id": case["case_id"],
            "drawing_path": str(PROJECT_ROOT / case["drawing_path"]),
            "output_dir": str(case_output),
            "output_name": case["case_id"],
    }
    if case.get("reference_image_path"):
        input_state["reference_image_path"] = str(
            PROJECT_ROOT / case["reference_image_path"]
        )
    if extraction:
        input_state["drawing_extraction"] = extraction
    state = await graph.ainvoke(
        input_state,
        config={
            "configurable": {
                "thread_id": f"drawing-golden-{case['case_id']}-{uuid.uuid4().hex[:8]}"
            }
        },
    )
    result = state.get("result", {})
    return {
        "case_id": case["case_id"],
        "name": case["name"],
        "expected_gate": case["expected_gate"],
        "status": result.get("status", state.get("status", "unknown")),
        "step_file": result.get("step_file"),
        "previews": result.get("previews", {}),
        "extraction": state.get("drawing_extraction", {}),
        "extraction_gate": state.get("extraction_gate", {}),
        "feature_ir": state.get("feature_ir", {}),
        "verification": state.get("drawing_verification", {}),
        "golden_comparison": state.get("golden_comparison", {}),
        "errors": state.get("errors", []),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }


def write_report(output_root: Path, results: list[dict[str, Any]]) -> None:
    """保存 Golden Set JSON 与 Markdown 报告。"""
    output_root.mkdir(parents=True, exist_ok=True)
    evidence_files = {
        "extraction.json": "extraction",
        "gate_result.json": "extraction_gate",
        "feature_ir.json": "feature_ir",
        "step_metrics.json": "verification",
        "golden_comparison.json": "golden_comparison",
    }
    for item in results:
        case_dir = output_root / item["case_id"]
        case_dir.mkdir(parents=True, exist_ok=True)
        for filename, key in evidence_files.items():
            (case_dir / filename).write_text(
                json.dumps(item.get(key, {}), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    summary = {
        "total": len(results),
        "candidate_ready": sum(
            item["status"] == "candidate_ready_for_review" for item in results
        ),
        "stopped_insufficient": sum(
            item["status"] == "stopped_insufficient_extraction" for item in results
        ),
        "failed": sum(item["status"] == "failed" for item in results),
    }
    (output_root / "results.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# STEP Agent Drawing Golden Set",
        "",
        f"- 样本：{summary['total']}",
        f"- 生成候选件：{summary['candidate_ready']}",
        f"- 信息不足安全停止：{summary['stopped_insufficient']}",
        f"- 执行失败：{summary['failed']}",
        "",
        "| Case | 预期门禁 | 实际状态 | STEP |",
        "|---|---|---|---|",
    ]
    for item in results:
        lines.append(
            f"| {item['case_id']} | {item['expected_gate']} | {item['status']} | "
            f"{'有' if item['step_file'] else '无'} |"
        )
    lines.extend(["", "## 提取门禁明细", ""])
    for item in results:
        gate = item["extraction_gate"]
        lines.extend([
            f"### {item['case_id']}",
            "",
            f"- 缺失：{gate.get('missing_required_fields', [])}",
            f"- 冲突：{gate.get('conflicting_fields', [])}",
            f"- 低置信度：{gate.get('low_confidence_fields', [])}",
            f"- 未解决：{gate.get('unresolved_required_fields', [])}",
            f"- 错误：{item['errors'] or '无'}",
            "",
        ])
    (output_root / "report.md").write_text("\n".join(lines), encoding="utf-8")


async def async_main(args: argparse.Namespace) -> int:
    """运行所选 Golden Set。"""
    cases = load_golden_cases(args.manifest)
    selected = [cases[args.case_id]] if args.case_id else list(cases.values())
    graph = build_drawing_to_step_graph()
    reused: dict[str, dict[str, Any]] = {}
    if args.reuse_extractions:
        old = json.loads(args.reuse_extractions.read_text(encoding="utf-8"))
        reused = {
            item["case_id"]: item.get("extraction", {})
            for item in old.get("results", [])
        }
    results: list[dict[str, Any]] = []
    for case in selected:
        result = await run_case(
            graph, case, args.output_dir, reused.get(case["case_id"])
        )
        results.append(result)
        print(
            f"[{case['case_id']}] status={result['status']} "
            f"step={'yes' if result['step_file'] else 'no'}",
            flush=True,
        )
    write_report(args.output_dir, results)
    print(json.dumps({"output": str(args.output_dir), "results": [
        {"case_id": item["case_id"], "status": item["status"]}
        for item in results
    ]}, ensure_ascii=False, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_GOLDEN_SET_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--case-id", choices=list(load_golden_cases().keys()))
    parser.add_argument(
        "--reuse-extractions",
        type=Path,
        default=None,
        help="重放已有 results.json 中的视觉提取，避免再次调用 API。",
    )
    return parser.parse_args()


def main() -> int:
    """同步命令行入口。"""
    return asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
