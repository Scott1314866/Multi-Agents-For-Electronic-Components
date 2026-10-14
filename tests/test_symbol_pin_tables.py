"""AD7682 real MinerU tables and user-verified 21-pin reference regressions."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.agents.symbol.contracts.extract import extract_pins_from_table
from backend.agents.symbol.contracts.pipeline import best_table
from backend.agents.symbol.contracts.pkgname import hints_from_text, pin_count_from_package
from backend.agents.symbol.contracts.selfcheck import check

GOLDEN = Path(__file__).parent / "golden/symbol/ad7682"


def tables():
    return json.loads((GOLDEN / "mineru_pin_tables.json").read_text(encoding="utf-8"))


def extracted():
    page, extraction = best_table(tables(), [11], package="LFCSP-20", device="AD7682BCPZRL7")
    assert page == 11
    return extraction


def test_real_tables_match_all_reference_numbers_and_model_names():
    extraction = extracted()
    actual = {pin.pin_number: pin for pin in extraction.pins}
    reference = json.loads((GOLDEN / "reference_pins.json").read_text(encoding="utf-8"))
    assert len(actual) == len(extraction.pins) == 21
    assert set(actual) == {pin["pin_number"] for pin in reference}
    # Preserve raw OCR and the datasheet's EPAD label until explicitly reviewed.
    for pin in reference:
        raw = {"16": "INO", "21": "EPAD"}.get(pin["pin_number"], pin["name"])
        assert actual[pin["pin_number"]].name == raw
    assert not extraction.warnings
    assert actual["16"].evidence[0].page == actual["21"].evidence[0].page == 12
    assert actual["1"].evidence[0].page == actual["20"].evidence[0].page == 11
    assert "Exposed Pad" in actual["21"].description
    assert actual["6"].type == "PAS"
    assert actual["11"].type == "IN" and actual["14"].type == "OUT"


def test_family_name_column_is_selected_for_full_ordering_code():
    _, extraction = best_table(tables(), [11], package="LFCSP-20", device="AD7689BCPZ")
    actual = {pin.pin_number: pin.name for pin in extraction.pins}
    assert actual["6"] == "IN4" and actual["8"] == "IN6"
    assert actual["17"] == "IN1" and actual["18"] == "IN2"
    assert actual["21"] == "EPAD"


def test_unknown_model_does_not_silently_select_first_mnemonic_column():
    assert best_table(tables(), [11], package="LFCSP-20", device="OTHER123") is None


@pytest.mark.parametrize("number", ["1,20", "4, 5", "A1, B2"])
def test_explicit_comma_numbers_expand_without_invention(number):
    html = f"<table><tr><th>Pin No.</th><th>Name</th></tr><tr><td>{number}</td><td>VDD</td></tr></table>"
    result = extract_pins_from_table(html, page=3)
    assert [pin.pin_number for pin in result.pins] == [n.strip() for n in number.split(",")]
    assert all(pin.evidence[0].page == 3 for pin in result.pins)


@pytest.mark.parametrize("number", ["1,一", "１,2", "1,,2", "1,2(foo)", "1-20"])
def test_invalid_compound_number_is_rejected_whole_with_warning(number):
    html = f"<table><tr><th>Pin No.</th><th>Name</th></tr><tr><td>3</td><td>REF</td></tr><tr><td>{number}</td><td>VDD</td></tr></table>"
    result = extract_pins_from_table(html, page=3)
    assert [pin.pin_number for pin in result.pins] == ["3"]
    assert any("VDD" in w or number in w for w in result.warnings)


@pytest.mark.parametrize("mutation", ["different_identity", "not_continued", "distant_page", "different_columns"])
def test_only_explicit_same_table_continuations_are_merged(mutation):
    content = tables()[:2]
    if mutation == "different_identity":
        content[1]["table_caption"] = ["Table 10. Pin Descriptions (Continued)"]
    elif mutation == "not_continued":
        content[1]["table_caption"][0] = content[1]["table_caption"][0].replace(" (Continued)", "")
    elif mutation == "distant_page":
        content[1]["page_idx"] = 20
    else:
        content[1]["table_body"] = content[1]["table_body"].replace("AD7682", "AD9999")
    _, result = best_table(content, [11], package="LFCSP-20", device="AD7682BCPZRL7")
    assert len(result.pins) == 16
    assert "16" not in {pin.pin_number for pin in result.pins}


def test_wrong_package_is_not_merged_or_selected_even_when_it_precedes_target():
    content = tables()
    _, result = best_table(content[2:] + content[:2], package="LFCSP-20", device="AD7682BCPZRL7")
    assert len(result.pins) == 21
    assert all(pin.pin_number.isdigit() for pin in result.pins)
    assert best_table(content[:2], package="WLCSP-20", device="AD7682BCPZRL7") is None


def test_contradictory_continuation_duplicate_remains_a_blocking_error():
    content = tables()[:2]
    content[1]["table_body"] = content[1]["table_body"].replace("<td>16</td>", "<td>1</td>")
    _, result = best_table(content, [11], package="LFCSP-20", device="AD7682BCPZRL7")
    report = check(result.pins, package="LFCSP-20", sides_evaluated=False)
    assert any(f.severity == "error" for f in report.findings)
    assert any("冲突" in w for w in result.warnings)


@pytest.mark.parametrize("name", ["EPAD", "EP"])
def test_documented_exposed_pad_is_preserved_and_counted_separately(name):
    pins = extracted().pins
    next(pin for pin in pins if pin.pin_number == "21").name = name
    report = check(pins, package="LFCSP-20", device="AD7682BCPZRL7", sides_evaluated=False)
    assert report.pin_count == 21
    assert not any(f.severity == "error" for f in report.findings)
    assert any(f.code == "exposed_pad" for f in report.findings)


@pytest.mark.parametrize("mutation", ["missing_pin", "ordinary_extra", "unverified_pad", "wrong_pad_number", "wrong_peripheral_number"])
def test_real_missing_or_extra_pins_still_block(mutation):
    pins = deepcopy(extracted().pins)
    if mutation == "missing_pin":
        pins = [pin for pin in pins if pin.pin_number != "9"]
    elif mutation == "ordinary_extra":
        pins[-1].name = "OTHER"
    elif mutation == "unverified_pad":
        pins[-1].description = ""
    elif mutation == "wrong_peripheral_number":
        next(pin for pin in pins if pin.pin_number == "9").pin_number = "22"
    else:
        pins[-1].pin_number = "22"
    report = check(pins, package="LFCSP-20", sides_evaluated=False)
    assert any(f.severity == "error" for f in report.findings)


def test_lfcsp_package_hint_is_recognized():
    assert pin_count_from_package("LFCSP20") == 20
    assert any(h.pin_count == 20 for h in hints_from_text(
        [{"type": "text", "page_idx": 10, "text": "20-lead LFCSP"}]))
