"""Synthetic regression coverage for stacked OCR bounds reconciliation."""

from __future__ import annotations

import pytest

from backend.agents.step.vision.evidence_fusion import _reconcile_split_stacked_ranges
from backend.agents.step.vision.schemas import (
    GeometryLine,
    OCRToken,
    QwenSemanticResult,
    SemanticAssignment,
    VisualEvidenceBundle,
)


def _token(token_id, text, region, bbox, *, unit="mm", role="unknown"):
    return OCRToken(
        token_id=token_id, text=text, source_region_id=region, bbox=bbox,
        confidence=0.99, unit_context=unit, value_role=role,
    )


def _line(line_id, region, y, *, x0=90, x1=110, kind="line_segment"):
    return GeometryLine(
        line_id=line_id, type=kind, start=(x0, y), end=(x1, y),
        confidence=0.99, source_region_id=region,
    )


def _assignment(name, token_ids, line_ids=()):
    return SemanticAssignment(
        canonical_name=name, token_ids=list(token_ids), line_ids=list(line_ids),
        target_feature="body", confidence=0.99,
    )


def _base_case(*, scale=1, side_upper="1.5", side_lower="1.0", side_region="side"):
    def box(x0, y0, x1, y1):
        return tuple(round(value * scale) for value in (x0, y0, x1, y1))

    tokens = [
        _token("side_upper", side_upper, side_region, box(95, 10, 105, 20)),
        _token("side_lower", side_lower, side_region, box(95, 21, 105, 31)),
        _token("front_upper", "1.1", "front", box(95, 110, 105, 120)),
        _token("front_lower", "0.9", "front", box(95, 121, 105, 131)),
    ]
    lines = [
        _line("side_separator", side_region, round(20.5 * scale), x0=round(94 * scale), x1=round(106 * scale)),
        _line("front_separator", "front", round(120.5 * scale), x0=round(94 * scale), x1=round(106 * scale)),
    ]
    evidence = VisualEvidenceBundle(
        image_sha256="0" * 64, image_size=(1000, 1000), ocr_tokens=tokens, lines=lines,
    )
    semantics = QwenSemanticResult(
        family_id="ic", package_type="gullwing_ic", overall_confidence=0.99,
        assignments=[
            _assignment("terminal_thickness", ["side_upper"], ["side_separator"]),
            _assignment("body_standoff", ["side_lower"], ["side_separator"]),
            _assignment("body_standoff", ["front_upper", "front_lower"], ["front_separator"]),
        ],
    )
    return evidence, semantics


def _reconcile(evidence, semantics):
    return _reconcile_split_stacked_ranges(evidence, semantics)


def _add_support(evidence, semantics, *, upper="1.1", lower="0.9", region="anchor", scale=1):
    evidence.ocr_tokens.extend([
        _token("anchor_upper", upper, region, (95 * scale, 210 * scale, 105 * scale, 220 * scale)),
        _token("anchor_lower", lower, region, (95 * scale, 221 * scale, 105 * scale, 231 * scale)),
    ])
    evidence.lines.append(_line("anchor_separator", region, round(220.5 * scale), x0=94 * scale, x1=106 * scale))
    semantics.assignments[2].token_ids = ["anchor_upper", "anchor_lower"]
    semantics.assignments[2].line_ids = ["anchor_separator"]


def test_front_body_standoff_and_side_terminal_thickness_split_ranges_reconcile():
    evidence, semantics = _base_case()
    _add_support(evidence, semantics)
    # Retain an independent, complete front body-standoff range.
    evidence.ocr_tokens.extend([
        _token("front_body_upper", "2.0", "front_body", (95, 310, 105, 320)),
        _token("front_body_lower", "1.7", "front_body", (95, 321, 105, 331)),
    ])
    evidence.lines.append(_line("front_body_separator", "front_body", 320, x0=94, x1=106))
    semantics.assignments.append(
        _assignment("body_standoff", ["front_body_upper", "front_body_lower"], ["front_body_separator"])
    )
    result = _reconcile(evidence, semantics)
    side = next(item for item in result.assignments if item.canonical_name == "terminal_thickness")
    assert side.token_ids == ["side_upper", "side_lower"]
    assert "side_separator" in side.line_ids
    assert any("stacked_range_reconciled:side_upper+side_lower->terminal_thickness" in item for item in result.ambiguities)
    assert any(item.token_ids == ["front_body_upper", "front_body_lower"] for item in result.assignments)


@pytest.mark.parametrize("scale", [1, 4])
def test_reconciliation_is_independent_of_pixel_scale(scale):
    evidence, semantics = _base_case(scale=scale)
    _add_support(evidence, semantics, scale=scale)
    result = _reconcile(evidence, semantics)
    assert next(item for item in result.assignments if item.canonical_name == "terminal_thickness").token_ids == ["side_upper", "side_lower"]


@pytest.mark.parametrize("mutation", ["missing_separator", "different_view", "different_unit", "table_row", "no_anchor"])
def test_unproven_stacked_values_are_not_reconciled(mutation):
    evidence, semantics = _base_case()
    _add_support(evidence, semantics)
    if mutation == "missing_separator":
        evidence.lines = [line for line in evidence.lines if line.line_id != "side_separator"]
    elif mutation == "different_view":
        evidence.ocr_tokens[1].source_region_id = "other_side"
    elif mutation == "different_unit":
        evidence.ocr_tokens[1].unit_context = "unknown"
    elif mutation == "table_row":
        evidence.ocr_tokens[0].value_role = "table_maximum"
    elif mutation == "no_anchor":
        evidence.ocr_tokens = evidence.ocr_tokens[:2]
        evidence.lines = evidence.lines[:1]
        semantics.assignments = semantics.assignments[:2]
    result = _reconcile(evidence, semantics)
    assert [item.token_ids for item in result.assignments[:2]] == [["side_upper"], ["side_lower"]]
    assert not any(item.startswith("stacked_range_reconciled:side_upper+") for item in result.ambiguities)


def test_independent_dimension_lines_block_cross_field_reconciliation():
    evidence, semantics = _base_case()
    _add_support(evidence, semantics)
    evidence.lines.extend([
        _line("upper_dimension", "side", 5, kind="dimension_line"),
        _line("lower_dimension", "side", 36, kind="dimension_line"),
    ])
    semantics.assignments[0].line_ids = ["side_separator", "upper_dimension"]
    semantics.assignments[1].line_ids = ["side_separator", "lower_dimension"]
    result = _reconcile(evidence, semantics)
    assert [item.token_ids for item in result.assignments[:2]] == [["side_upper"], ["side_lower"]]


def test_independent_nearby_dimension_label_blocks_reconciliation():
    evidence, semantics = _base_case()
    _add_support(evidence, semantics)
    evidence.ocr_tokens.append(_token("dimension_label", "A1", "side", (108, 10, 120, 20)))
    result = _reconcile(evidence, semantics)
    assert [item.token_ids for item in result.assignments[:2]] == [["side_upper"], ["side_lower"]]


def test_two_body_standoff_assignments_with_own_dimension_lines_stay_distinct():
    evidence, semantics = _base_case()
    _add_support(evidence, semantics)
    evidence.lines.extend([
        _line("upper_dimension", "side", 5, kind="dimension_line"),
        _line("lower_dimension", "side", 36, kind="dimension_line"),
    ])
    semantics.assignments[0].canonical_name = "body_standoff"
    semantics.assignments[0].line_ids = ["side_separator", "upper_dimension"]
    semantics.assignments[1].canonical_name = "body_standoff"
    semantics.assignments[1].line_ids = ["side_separator", "lower_dimension"]
    result = _reconcile(evidence, semantics)
    assert [item.token_ids for item in result.assignments[:2]] == [["side_upper"], ["side_lower"]]


def test_values_without_valid_cross_view_range_anchor_stay_separate():
    evidence, semantics = _base_case()
    _add_support(evidence, semantics, upper="0.8", lower="0.6")
    result = _reconcile(evidence, semantics)
    assert [item.token_ids for item in result.assignments[:2]] == [["side_upper"], ["side_lower"]]
