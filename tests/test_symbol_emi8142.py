"""Real EMI8142 OCR and physical-package selection regressions."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from backend.agents.symbol import nodes
from backend.agents.symbol.contracts.extract import FigureInfo, canonical_dual_row
from backend.agents.symbol.contracts.pdf_locator import figure_pages
from backend.agents.symbol.contracts.pipeline import best_table
from backend.agents.symbol.contracts.pkgname import hints_from_text
from backend.agents.symbol.contracts.merge import merge_channels
from backend.agents.symbol.tools.review import review_pins

GOLDEN = Path(__file__).parent / "golden/symbol/emi8142"


def table_pins():
    tables = json.loads((GOLDEN / "mineru_pin_tables.json").read_text(encoding="utf-8"))
    page, extracted = best_table(tables, [2], package="XDFN-10", device="EMI8142MUTAG")
    assert page == 2
    return extracted.pins


def physical_payload():
    return {"pins": [{"pin_number": str(n), "side": "top" if n <= 5 else "bottom"} for n in range(1, 11)]}


def test_real_ocr_selects_only_emi8142_and_repairs_io_type_without_changing_names():
    pins = table_pins()
    actual = {p.pin_number: p for p in pins}
    assert set(actual) == {str(n) for n in range(1, 11)}
    assert all(p.type == "IO" for p in pins if p.pin_number not in {"3", "8"})
    assert actual["3"].name == actual["8"].name == "VN"
    assert actual["3"].description == actual["8"].description == "Ground"
    assert actual["1"].name == "In_1+" and actual["10"].name == "Out_1+"
    assert not any("3" in p.name for p in pins)


def test_complete_dual_bank_normalizes_to_reference_without_using_reference_as_input():
    figure = canonical_dual_row(FigureInfo.from_vision(physical_payload(), page=8), "XDFN-10")
    actual = {p.pin_number: p for p in merge_channels(table_pins(), figure, page=2).pins}
    expected = json.loads((GOLDEN / "reference_pins.json").read_text(encoding="utf-8"))
    assert {p.pin_number: (p.side, p.side_order) for p in actual.values()} == {
        p["pin_number"]: (p["side"], p["side_order"]) for p in expected}
    assert actual["3"].side == "left" and actual["8"].side == "right"


@pytest.mark.parametrize("mutation", ["missing", "third_bank", "duplicate", "different_family"])
def test_incomplete_or_unrelated_diagrams_cannot_supply_dual_bank_proof(mutation):
    payload = deepcopy(physical_payload())
    package = "XDFN-10"
    if mutation == "missing": payload["pins"].pop()
    elif mutation == "third_bank": payload["pins"][2]["side"] = "left"
    elif mutation == "duplicate": payload["pins"][-1]["pin_number"] = "1"
    else: package = "QFN-10"
    figure = FigureInfo.from_vision(payload, page=8)
    assert canonical_dual_row(figure, package) is figure


def test_matching_physical_outline_precedes_schematic_and_rejects_other_variants(tmp_path):
    pdf = tmp_path / "multi-family.pdf"
    with pymupdf.open() as document:
        for text in ["XDFN6 XDFN10 XDFN16 ELECTRICAL SCHEMATICS", "PACKAGE DIMENSIONS XDFN6 TOP VIEW PIN 1",
                     "PACKAGE DIMENSIONS XDFN10 BOTTOM VIEW PIN ONE", "PACKAGE DIMENSIONS XDFN16 TOP VIEW PIN 1"]:
            document.new_page().insert_text((30, 30), text)
        document.save(pdf)
    assert figure_pages(pdf, [1, 2, 3, 4], "XDFN-10") == [3, 1]
    assert any(h.style == "XDFN" and h.pin_count == 10 for h in hints_from_text([
        {"type": "text", "text": "XDFN10 2.2x1.35", "page_idx": 7}]))


def test_independent_review_uses_original_table_and_same_package_rotation():
    figure = canonical_dual_row(FigureInfo.from_vision(physical_payload(), page=8), "XDFN-10")
    pins = merge_channels(table_pins(), figure, page=2).pins
    payload = physical_payload()
    names = {p.pin_number: p.name for p in pins}
    for item in payload["pins"]:
        item.update(name=names[item["pin_number"]], confidence=.99, uncertain=False)
    calls = []
    client = SimpleNamespace(ask_json=lambda images, prompt: calls.append((images, prompt)) or payload)
    result = review_pins(client, "physical-8.png", device="EMI8142MUTAG", pins=pins, page=8,
                         package="XDFN-10", support_image_paths=["table-2.png"])
    assert result.passed and calls[0][0] == ["physical-8.png", "table-2.png"]
    assert "XDFN-10" in calls[0][1]
    assert {p["pin_number"]: p["side"] for p in result.raw["pins"]}["3"] == "left"


def test_render_selection_includes_package_chapter_and_selected_name_table(monkeypatch, tmp_path):
    seen = []
    async def ranked(pdf, candidates, package):
        seen.extend(candidates)
        return [8, 1]
    async def render(pdf, page, out_dir):
        return SimpleNamespace(path=Path(out_dir) / f"page-{page}.png")
    monkeypatch.setattr(nodes, "_figure_pages", ranked)
    monkeypatch.setattr(nodes, "_render_page", render)
    result = asyncio.run(nodes.render_pages_node({"pdf_path": "manual.pdf", "package": "XDFN-10",
        "locate": {"targets": {"pins": [1, 2, 3], "package": [7, 8, 9]}}, "pin_page": 2, "work_dir": str(tmp_path)}))
    assert seen == [1, 2, 3, 7, 8, 9]
    assert [item["page"] for item in result["rendered_pages"]] == [8, 1, 2]
