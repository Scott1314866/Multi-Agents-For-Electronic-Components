"""符号生成 Agent 的契约回归。

被加密的七个模块（``models`` / ``extract`` / ``layout`` / ``config`` /
``vision`` / ``capture`` / ``pipeline``）是按调用方契约重建的，所以**必须有
对拍**才算数。这里用的金标准是 Module_01 的 ``spike/`` 里留下的真实产物：

* ``golden/symbol/{stm32,ads1115}/*_spec.json`` —— 输入（引脚清单）；
* ``golden/symbol/{stm32,ads1115}/*_gen.tcl`` —— 输出（实时生成的 Capture 脚本）；
* ``golden/symbol/pin_table_sample.html`` —— MinerU 的真实表格输出。

STM32 那份是四边 100 脚，ADS1115 是双列 10 脚 —— 两种布局都覆盖到了。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re

import pytest

from backend.agents.symbol.contracts.extract import extract_pins_from_table, parse_table_html
from backend.agents.symbol.contracts.layout import build_layout, describe, preview
from backend.agents.symbol.contracts.models import DeviceSpec, Pin
from backend.agents.symbol.contracts.pipeline import best_table, guess_device
from backend.agents.symbol.tools.capture import PIN_TYPE_PASSIVE, render_tcl

GOLDEN = Path(__file__).parent / "golden" / "symbol"

CASES = (("stm32", "STM32F105VCT6"), ("ads1115", "ADS1115"))


def _load_pins(name: str, part: str) -> list[Pin]:
    payload = json.loads((GOLDEN / name / f"{part}_spec.json").read_text(encoding="utf-8"))
    return [
        Pin(
            pin_number=item["pin_number"],
            name=item["name"],
            type=item["type"],
            side=item["side"],
            side_order=item.get("side_order", 0),
            description=item.get("description", ""),
        )
        for item in payload["pins"]
    ]


def _original_tcl(name: str, part: str) -> str:
    return (GOLDEN / name / f"{part}_gen.tcl").read_text(encoding="utf-8")


def _original_out_dir(tcl: str) -> str:
    match = re.search(r'set outDir  "([^"]+)"', tcl)
    assert match, "真实生成物里应有 outDir"
    return match.group(1)


# ── 型号猜测（源程序的可执行规格）────────────────────────────────


def test_guess_device_matches_source_cases():
    """五条用例逐字取自 tests/regress.py:334-341。"""
    assert guess_device("C8350_单片机(MCU-MPU-SOC)_STM32F105VCT6_规格书_WJ94202.PDF") == "STM32F105VCT6"
    assert guess_device("ads1115.pdf") == "ADS1115"
    assert guess_device("TPS5430DDAR.PDF") == "TPS5430DDAR"
    # 上传落盘的任务号不是型号
    assert guess_device("9b88e7418e27.pdf") == ""
    assert guess_device("datasheet.pdf") == ""


# ── 表格解析（用 MinerU 的真实输出）──────────────────────────────


def test_extract_pins_from_real_mineru_table():
    html = (GOLDEN / "pin_table_sample.html").read_text(encoding="utf-8")
    grid = parse_table_html(html)
    assert len(grid) == 13 and max(len(row) for row in grid) == 6

    extraction = extract_pins_from_table(html, page=1, device="ADS1115")
    assert len(extraction.pins) == 10
    # 表头两行（PIN + 三个型号），选列命中 ADS1115
    assert extraction.device_columns == [3]
    assert "ADS1115" in extraction.headers[3]

    by_number = {pin.pin_number: pin for pin in extraction.pins}
    assert (by_number["1"].name, by_number["1"].type) == ("ADDR", "IN")
    assert by_number["2"].type == "OUT"
    assert by_number["3"].type == "PAS"
    # NC 那一行的 ADS1115 格子被 OCR 读成汉字「一」—— 必须被挡下并留痕
    assert any("NC" in warning for warning in extraction.warnings)


def test_best_table_skips_figure_and_picks_real_table():
    """§11.5 缺陷 A：逐页试表，别信第一个候选页。"""
    content = [
        {"type": "text", "page_idx": 22, "text": "引脚图"},
        {
            "type": "table",
            "page_idx": 23,
            "table_body": "<table><tr><td>fig</td></tr><tr><td>1</td></tr></table>",
        },
        {
            "type": "table",
            "page_idx": 25,
            "table_body": (
                "<table>"
                "<tr><td>PIN</td><td>NAME</td></tr>"
                "<tr><td>1</td><td>ADDR</td></tr>"
                "<tr><td>2</td><td>SCL</td></tr>"
                "<tr><td>3</td><td>SDA</td></tr>"
                "</table>"
            ),
        },
    ]
    found = best_table(content, [23, 24, 25, 26], package="")
    assert found is not None
    page, extraction = found
    assert page == 26 and [pin.pin_number for pin in extraction.pins] == ["1", "2", "3"]


# ── 几何对拍 ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("name", "part"), CASES)
def test_layout_reproduces_real_tcl_geometry(name: str, part: str):
    """布局的每个坐标、hotspot、引脚次序都必须与真实生成物对上。"""
    tcl = _original_tcl(name, part)
    layout = build_layout(_load_pins(name, part), part)

    bbox = re.search(r"sMakeCRect (-?\d+) (-?\d+) (-?\d+) (-?\d+)", tcl)
    assert bbox is not None
    left, top, right, bottom = (int(g) for g in bbox.groups())
    assert (left, top, right, bottom) == (
        -layout.body_width // 2,
        layout.half_height,
        layout.body_width // 2,
        -layout.half_height,
    )

    # 取自生成物的 (name, start, hot, index)
    placed = []
    for chunk in tcl.split("NewSymbolPinScalar")[1:]:
        name_match = re.search(r'sMakeCString "([^"]+)"', chunk)
        points = re.findall(r"sMakeCPoint (-?\d+) (-?\d+)", chunk)
        index = re.search(r"\] 1 (\d+)\]", chunk)
        # .dsn 段落里也有 NewPlacedInst 的 sMakeCPoint，只认引脚那一种：
        # 它必须同时有名字、两个坐标点和序号。
        if not (name_match and len(points) >= 2 and index):
            continue
        placed.append(
            (
                name_match.group(1),
                (int(points[0][0]), int(points[0][1])),
                (int(points[1][0]), int(points[1][1])),
                int(index.group(1)),
            )
        )
    numbers = re.findall(
        r'NewPinNumber \[DboTclHelper_sMakeCString "([^"]+)"\] \[DboTclHelper_sMakeInt \d+\]',
        tcl,
    )

    assert len(placed) == len(layout.pins)
    for index, pin in enumerate(layout.pins):
        got_name, start, hot, got_index = placed[index]
        assert got_name == pin.name, f"#{index} 引脚名不符"
        assert start == (pin.x, pin.y), f"#{index} start 不符"
        assert hot == (pin.hx, pin.hy), f"#{index} hotspot 不符"
        assert got_index == index, f"#{index} 序号不连续"
        assert numbers[index] == pin.number, f"#{index} 引脚号不符"


def test_layout_describe_and_preview_are_readable():
    layout = build_layout(_load_pins("ads1115", "ADS1115"), "ADS1115")
    assert "ADS1115" in describe(layout)
    assert "10 脚" in describe(layout)
    # 一度电的"全堆在一边"要从预览里看得出来
    assert "|" in preview(layout)


def test_layout_rewrites_unknown_sides_in_place():
    """源程序的隐式契约：``build_layout`` 会把 unknown 原地改成 left。

    调用方（``web/service.py``）为此三处注释反复强调"喂副本进去"。
    """
    pins = [Pin(pin_number="1", name="A", type="PAS", side="unknown")]
    layout = build_layout(pins, "X")
    assert layout.warnings and "侧别未定" in layout.warnings[0]
    assert pins[0].side == "left", "应当是原地改写，调用方要靠这个行为拿警告"
    assert layout.pins[0].side == "left"


# ── TCL 对拍（逐字节）─────────────────────────────────────────────


@pytest.mark.parametrize(("name", "part"), CASES)
def test_capture_renders_byte_identical_tcl(name: str, part: str, tmp_path: Path):
    """渲染出的 TCL 必须与真实生成物逐字节一致。

    唯一允许不同的是第一行注释（生成器路径变了）。这条断言一旦通过，
    就说明 ``capture.py`` 的模板、坐标格式、引脚循环、.dsn 段落全部正确。
    """
    tcl = _original_tcl(name, part)
    out_dir = _original_out_dir(tcl)
    rendered = render_tcl(build_layout(_load_pins(name, part), part), Path(out_dir))

    expected = tcl.splitlines()
    actual = rendered.splitlines()
    assert len(actual) == len(expected), "行数不同"
    assert actual[0].endswith("请勿手改") and "capture.py" in actual[0]
    assert actual[1:] == expected[1:]


def test_rendered_tcl_uses_passive_pins_and_avoids_newbox():
    rendered = render_tcl(build_layout(_load_pins("ads1115", "ADS1115"), "ADS1115"), Path("/tmp/x"))
    # NewBox 在目标环境恒失败，必须用 4 条 NewLine 拼矩形
    assert "NewBox" not in rendered
    assert rendered.count("$mPart NewLine") == 1  # 在 foreach 里，写一次跑四边
    # 引脚类型恒 4 = Passive
    assert f"\n              {PIN_TYPE_PASSIVE} \\" in rendered


# ── 数据模型往返 ──────────────────────────────────────────────────


def test_device_spec_round_trips_through_json():
    payload = json.loads((GOLDEN / "ads1115" / "ADS1115_spec.json").read_text(encoding="utf-8"))
    spec = DeviceSpec.from_dict(payload)
    assert spec.device == "ADS1115"
    assert len(spec.pins) == len(payload["pins"])
    assert spec.to_dict() == payload, "往返之后必须与落盘快照完全一致"


# ── 图与 API ──────────────────────────────────────────────────────


def test_symbol_graph_compiles_with_all_stops():
    from backend.agents.symbol.graph import build_symbol_graph

    graph = build_symbol_graph()
    nodes = set(graph.nodes)
    for expected in (
        "validate_input", "locate_pages", "parse_document", "pick_table",
        "discover_names", "ask_device", "ask_package", "render_pages",
        "vision_extract", "merge_channels", "review_pins", "resolve_conflicts",
        "resolve_review_diffs", "self_check", "ask_check_questions",
        "build_layout", "confirm_output", "generate_capture", "finalize",
        "stopped_no_pin_table", "stopped_offline", "stopped_no_toolchain", "failed",
    ):
        assert expected in nodes, f"缺少节点 {expected}"


def test_human_answer_validation_rejects_unknown_stage_and_action():
    from backend.agents.symbol.human_nodes import validate_human_answer

    request = {"stage": "device", "options": ["choose", "provide", "cancel"],
               "candidates": ["STM32F105VCT6"]}
    assert validate_human_answer(request, {"action": "choose", "value": "STM32F105VCT6"})["value"] == "STM32F105VCT6"

    with pytest.raises(ValueError):
        validate_human_answer(request, {"action": "choose", "value": "不在候选里"})
    with pytest.raises(ValueError):
        validate_human_answer(request, {"action": "approve"})
    with pytest.raises(ValueError):
        validate_human_answer({"stage": "不存在"}, {"action": "choose"})


def test_conflict_resolution_cannot_invent_entries():
    from backend.agents.symbol.human_nodes import validate_human_answer

    request = {
        "stage": "conflicts",
        "options": ["resolve", "cancel"],
        "items": [{"pin_number": "3", "field": "side", "values": {}, "pages": [26]}],
    }
    good = validate_human_answer(
        request, {"action": "resolve", "resolutions": [{"pin_number": "3", "field": "side", "value": "left"}]}
    )
    assert good["resolutions"][0]["value"] == "left"
    with pytest.raises(ValueError):
        validate_human_answer(
            request, {"action": "resolve", "resolutions": [{"pin_number": "9", "field": "side", "value": "left"}]}
        )


def test_symbol_routes_are_registered():
    from backend.step_main import app

    paths = app.openapi()["paths"]
    for path in (
        "/api/v1/symbol/generate",
        "/api/v1/symbol/drawings",
        "/api/v1/symbol/drawings/{drawing_id}",
        "/api/v1/symbol/drawings/{drawing_id}/human-input",
        "/api/v1/symbol/drawings/{drawing_id}/artifacts/{artifact_name}",
    ):
        assert path in paths, f"{path} 未注册"


def test_validate_input_rejects_missing_and_bogus_files(tmp_path: Path):
    from backend.agents.symbol.nodes import validate_input_node

    out = asyncio.run(validate_input_node({"pdf_path": str(tmp_path / "nope.pdf")}))
    assert out["status"] == "failed" and "不存在" in out["errors"][0]

    bogus = tmp_path / "a.pdf"
    bogus.write_text("not a pdf", encoding="utf-8")
    out = asyncio.run(validate_input_node({"pdf_path": str(bogus)}))
    assert out["status"] == "failed" and "%PDF-" in out["errors"][0]

    real = tmp_path / "b.pdf"
    real.write_bytes(b"%PDF-1.7\n%%EOF\n")
    out = asyncio.run(validate_input_node({"pdf_path": str(real), "work_dir": str(tmp_path)}))
    assert out["status"] == "input_valid"
