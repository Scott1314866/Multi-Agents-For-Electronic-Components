"""PCB 封装 Agent 的离线回归。

金标准是 Module_02 留下的两组日志：

* ``golden/pcb/final/`` —— 成功版的 WSON8_3X3（``object_count=16``、无 DRC）；
* ``golden/pcb/v1/`` —— 焊盘过宽（``land_y=0.40``、``ep_x=1.20``）被拒的 v1
  （``object_count=30``、14 个 ``DRC ERROR CLASS/TOP`` 对象、``SPMHA1-301``）。

前者必须全过，后者必须被拦住。这些断言不依赖本机是否装了 Cadence。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from langgraph.types import Command

from backend.agents.pcb import assertions as A
from backend.agents.pcb import parser as P
from backend.agents.pcb import skill_emitter as em
from backend.agents.pcb.derive import check_clearances, derive_package, summarize
from backend.agents.pcb.graph import build_pcb_graph
from backend.agents.pcb.spec import PackageSpec, wson8_3x3

GOLDEN = Path(__file__).parent / "golden" / "pcb"
SPEC_NAME = "WSON8_3X3"


def _logs(version: str) -> dict[str, str]:
    return P.read_logs(GOLDEN / version, SPEC_NAME)


# ── 派生层 ────────────────────────────────────────────────────────


def test_derive_reproduces_source_hardcoded_geometry():
    """派生值必须与 build_wson8.il 里写死的数字逐项一致。"""
    derived = derive_package(wson8_3x3())

    assert (derived.asm_half, derived.pb_half) == (1.50, 1.55)
    assert (derived.silk_half, derived.silk_span) == (1.60, 1.65)
    assert derived.pin1_marker == (-1.95, -1.65, 1.75, 1.45)
    assert derived.land_pad.copper == (0.90, 0.35)
    assert derived.land_pad.mask == (1.00, 0.45)
    assert derived.ep_pad is not None
    assert derived.ep_pad.copper == (1.10, 2.00)
    assert derived.ep_pad.mask == (1.20, 2.10)

    # L180-L187 的八行 bAddPin：坐标、次序、引脚号文字偏移
    assert [(p.number, p.x, p.y) for p in derived.pins] == [
        ("1", -1.15, 0.75), ("2", -1.15, 0.25),
        ("3", -1.15, -0.25), ("4", -1.15, -0.75),
        ("8", 1.15, 0.75), ("7", 1.15, 0.25),
        ("6", 1.15, -0.25), ("5", 1.15, -0.75),
    ]
    assert [p.text_offset[0] for p in derived.pins[:4]] == [-1.25] * 4
    assert [p.text_offset[0] for p in derived.pins[4:]] == [1.25] * 4


def test_clearance_check_rejects_v1_but_accepts_final():
    """v1 与最终版只差两条 0.05mm 的余量，校验必须分辨得出来。"""
    assert check_clearances(wson8_3x3()) == []

    v1 = wson8_3x3().model_copy(update={"land_y": 0.40, "ep_x": 1.20})
    issues = check_clearances(v1)
    assert len(issues) == 2
    assert all("0.100" in issue for issue in issues)


def test_summary_reads_sanely():
    spec = wson8_3x3()
    text = summarize(spec, derive_package(spec))
    assert "WSON8_3X3" in text and "0.90×0.35" in text


# ── SKILL 生成 ────────────────────────────────────────────────────


def test_emitted_skill_is_balanced_and_on_allowed_layers(tmp_path):
    spec = wson8_3x3()
    written = em.write_skill_package(spec, derive_package(spec), tmp_path)

    assert set(written) == {
        "build_il", "verify_il", "props_il",
        "build_scr", "verify_scr", "props_scr", "ilinit",
    }
    for key in ("build_il", "verify_il", "props_il"):
        text = written[key].read_text(encoding="utf-8")
        assert em.paren_depth(text) == 0, f"{key} 括号不平衡"
    assert em.check_layers() == []


def test_emitted_skill_covers_every_pin_and_geometry(tmp_path):
    """生成的脚本里必须逐个引脚出现，且各层几何都用派生值。"""
    spec = wson8_3x3()
    text = em.write_skill_package(spec, derive_package(spec), tmp_path)[
        "build_il"
    ].read_text(encoding="utf-8")

    for number in spec.pin_numbering:
        assert f'bAddPin("{number}"' in text
    assert "bAddEpPin(0.0 0.0)" in text
    assert "bMakePadstack(bLand landX landY landX landY 1.00 0.45)" in text
    assert "bMakePadstack(bEp epX epY epX epY 1.20 2.10)" in text
    assert "asmHalf   = 1.50" in text
    assert "silkSpan  = 1.65" in text
    # 无头模式必须带 ?noConfirm t，否则卡在保存确认框
    assert "?noConfirm t" in text
    # 层名一个都不能越界
    for layer in em.ALLOWED_LAYERS:
        assert layer in text


# ── 判据（金标准日志）─────────────────────────────────────────────


def test_judge_passes_on_final_logs():
    logs = _logs("final")
    verdict = A.judge(
        wson8_3x3(),
        derive_package(wson8_3x3()),
        build=P.parse_build_log(logs["build"]),
        verify=P.parse_verify_log(logs["verify"]),
        props=P.parse_props_log(logs["props"]),
        symbol_log=logs["symbol"],
    )
    assert verdict.passed, verdict.to_text()
    # 对象总数的期望值由派生算出，不是抄来的常数
    assert A.expected_object_count(wson8_3x3(), derive_package(wson8_3x3())) == 16


def test_judge_fails_on_v1_logs_with_fourteen_drc_objects():
    logs = _logs("v1")
    verdict = A.judge(
        wson8_3x3(),
        derive_package(wson8_3x3()),
        build=P.parse_build_log(logs["build"]),
        symbol_log=logs["symbol"],
    )
    assert not verdict.passed
    failed = {item.name for item in verdict.failures}
    assert "构建期无 DRC 标记" in failed
    assert "编译报告无 DRC 警告" in failed

    build = P.parse_build_log(logs["build"])
    assert len(build.drc_objects) == 14
    assert build.dumped_pin_count == 9


def test_parser_reads_authoritative_geometry():
    """verify.log 是几何的权威来源：引脚 bbox 量的是阻焊矩形。"""
    verify = P.parse_verify_log(_logs("final")["verify"])
    assert verify.design_type == "PACKAGE"
    assert verify.units == "millimeters"
    assert (verify.accuracy, verify.pin_count, verify.object_count) == (4, 9, 16)

    first = next(p for p in verify.pins if p.number == "1")
    assert first.bbox is not None
    assert (first.bbox.width, first.bbox.height) == (1.00, 0.45)
    assert first.bbox.center == (-1.15, 0.75)


def test_props_value_is_uppercased_by_allegro():
    """源码写 "0.8 mm"，回读是 "0.8 MM" —— 比较前必须归一。"""
    props = P.parse_props_log(_logs("final")["props"])
    owners = props.with_properties
    assert len(owners) == 1
    assert owners[0].layer == A.PLACE_BOUND_LAYER
    assert owners[0].entries == {"PACKAGE_HEIGHT_MAX": "0.8 MM"}


# ── 图（离线模式）─────────────────────────────────────────────────


def _run(state: dict, thread: str) -> dict:
    graph = build_pcb_graph()
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 128}
    return asyncio.run(graph.ainvoke(state, config=config))


def _interrupt_id(out: dict) -> str:
    interrupts = out["__interrupt__"]
    first = interrupts[0]
    return first.id if hasattr(first, "id") else first["id"]


def _run_resumed(state: dict, thread: str, answer: dict) -> dict:
    graph = build_pcb_graph()
    config = {"configurable": {"thread_id": thread}, "recursion_limit": 128}
    out = asyncio.run(graph.ainvoke(state, config=config))
    if "__interrupt__" not in out:
        return out
    return asyncio.run(
        graph.ainvoke(Command(resume={_interrupt_id(out): answer}), config=config)
    )


def test_emit_only_round_trip_produces_full_skill_package(tmp_path):
    out = _run_resumed(
        {
            "task": "generate_pcb_package",
            "mode": "emit_only",
            "spec": wson8_3x3().model_dump(),
            "work_dir": str(tmp_path),
        },
        "pcb-test-emit",
        {"action": "confirm"},
    )
    assert out["status"] == "skills_ready"
    assert (tmp_path / "build_wson8_3x3.il").is_file()
    assert (tmp_path / "allegro.ilinit").is_file()
    assert "build_il" in out["artifact_paths"]


def test_invalid_spec_stops_before_asking_a_human(tmp_path):
    """坏参数不该让人用"确认"盖过去 —— 连中断都不该发生。"""
    bad = wson8_3x3().model_dump()
    bad.update(land_y=0.40, ep_x=1.20)
    out = _run(
        {
            "task": "generate_pcb_package",
            "mode": "emit_only",
            "spec": bad,
            "work_dir": str(tmp_path),
        },
        "pcb-test-bad",
    )
    assert "__interrupt__" not in out
    assert out["status"] == "stopped_invalid_spec"
    assert len(out["result"]["issues"]) == 2
    assert not list(tmp_path.glob("*.il")), "不合规的参数不该产出 SKILL"


def test_missing_toolchain_is_stopped_not_failed(tmp_path):
    """本机没有 allegro.exe：装置备好、任务停住，但不是失败。"""
    out = _run_resumed(
        {
            "task": "generate_pcb_package",
            "mode": "full",
            "spec": wson8_3x3().model_dump(),
            "work_dir": str(tmp_path),
            "allegro_exe": str(tmp_path / "definitely-not-here.exe"),
        },
        "pcb-test-no-toolchain",
        {"action": "confirm"},
    )
    assert out["status"] == "stopped_no_toolchain"
    assert (tmp_path / "build_wson8_3x3.il").is_file()


def test_missing_spec_fails_with_actionable_message():
    out = _run({"task": "generate_pcb_package", "mode": "emit_only"}, "pcb-test-nospec")
    assert out["status"] == "failed"
    assert "缺少 spec" in out["errors"][0]


# ── API 契约 ──────────────────────────────────────────────────────


def test_pcb_routes_are_registered():
    from backend.step_main import app

    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/pcb/generate",
        "/api/v1/pcb/packages",
        "/api/v1/pcb/packages/{drawing_id}",
        "/api/v1/pcb/packages/{drawing_id}/human-input",
        "/api/v1/pcb/packages/{drawing_id}/artifacts/{artifact_name}",
    ):
        assert path in paths, f"{path} 未注册"


def test_spec_rejects_self_inconsistent_input():
    import pytest

    # 引脚号数量对不上
    with pytest.raises(Exception):
        PackageSpec(**{**wson8_3x3().model_dump(), "pin_count": 10})
    # 标称散热盘却缺尺寸
    with pytest.raises(Exception):
        PackageSpec(**{**wson8_3x3().model_dump(), "ep_x": None})
