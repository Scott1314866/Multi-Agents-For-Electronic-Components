"""Pure checks for evidence collisions and complete per-view review candidates.

These helpers report issues only. They do not select dimensions, rewrite evidence,
remove assignments, call a model, or decide which candidate should be retained.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from backend.agents.step.vision.ocr import (
    parse_dimension_expression,
    parse_explicit_count_expression,
    parse_table_nominal_expression,
)
from backend.agents.step.vision.schemas import (
    QwenSemanticResult,
    QwenViewSemanticResult,
    VisualEvidenceBundle,
)


_GULLWING_FAMILIES = frozenset({
    "ic/gullwing_ic", "gullwing_ic", "ic/quad_gullwing_ic", "quad_gullwing_ic",
})
_EXCLUSIVE_PAIRS = (
    ("total_height", "terminal_length"),
    ("body_standoff", "terminal_length"),
    ("body_standoff", "terminal_thickness"),
    ("pin_span", "overall_width"),
)
_COUNT_FIELDS = frozenset({"nominal_pin_count", "circuit_count"})


def _is_numeric(text: str, *, table_value: bool = False) -> bool:
    parsed = (
        parse_table_nominal_expression(text)
        if table_value else parse_dimension_expression(text)
    )
    return any(value is not None for value in (
        parsed.nominal_value, parsed.minimum_value, parsed.maximum_value,
    ))


def find_semantic_collisions(
    evidence: VisualEvidenceBundle,
    semantics: QwenSemanticResult,
) -> list[dict[str, Any]]:
    """Report explicitly incompatible gullwing quantities sharing numeric tokens.

    Equal numbers from different tokens and shared nonnumeric row labels are not
    collisions. Repeated references to the same canonical quantity are allowed.
    Each returned item describes one incompatible pair and its shared tokens.
    """
    if semantics.family_id not in _GULLWING_FAMILIES:
        return []
    tokens = {token.token_id: token for token in evidence.ocr_tokens}
    numeric_ids = {
        token.token_id for token in evidence.ocr_tokens
        if _is_numeric(token.text, table_value=token.value_role.startswith("table_"))
    }
    owners: dict[str, set[str]] = defaultdict(set)
    for assignment in semantics.assignments:
        for token_id in set(assignment.token_ids) & numeric_ids:
            owners[token_id].add(assignment.canonical_name)

    collisions = []
    for pair in _EXCLUSIVE_PAIRS:
        shared = sorted(token_id for token_id, names in owners.items() if set(pair) <= names)
        if shared:
            collisions.append({
                "token_ids": shared,
                "names": sorted(pair),
                "region_ids": sorted({tokens[token_id].source_region_id for token_id in shared}),
            })
    return collisions


def validate_review_candidate(
    candidate: QwenViewSemanticResult,
    evidence: dict[str, Any],
    allowed_fields: set[str],
    region_id: str,
    view_type: str,
) -> list[str]:
    """Validate a whole review response against the exact payload it received.

    The compact retrieval payload omits source-region fields; in that format,
    membership in its token/line lists is the view boundary. When source IDs are
    present, an explicit different region is rejected as well. Table value cells
    and explicit pin/circuit counts may omit lines; ordinary dimensions may not.
    """
    issues: list[str] = []
    if candidate.region_id != region_id:
        issues.append(f"region_id mismatch: expected {region_id}, got {candidate.region_id}")
    if candidate.view_type != view_type:
        issues.append(f"view_type mismatch: expected {view_type}, got {candidate.view_type}")
    payload_region = (evidence.get("region") or {}).get("region_id")
    if payload_region is not None and payload_region != region_id:
        issues.append(f"evidence region_id mismatch: {payload_region}")
    unknown_unresolved = sorted(set(candidate.unresolved_fields) - allowed_fields)
    if unknown_unresolved:
        issues.append(f"unresolved_fields outside contract: {unknown_unresolved}")

    tokens = {item["token_id"]: item for item in evidence.get("ocr_tokens", [])}
    lines = {item["line_id"]: item for item in evidence.get("lines", [])}
    groups = evidence.get("dimension_groups") or []
    context_ids = {token_id for group in groups for token_id in group.get("context_token_ids", [])}
    table_ids = {
        token_id
        for group in groups if group.get("evidence_type") == "table_row"
        for token_id in group.get("table_columns", {}).values()
    } | {
        token_id for token_id, token in tokens.items()
        if str(token.get("value_role", "")).startswith("table_")
    }
    numeric_ids = {
        token_id for token_id, token in tokens.items()
        if _is_numeric(str(token.get("text", "")), table_value=token_id in table_ids)
    }
    explicit_count_ids = {
        token_id for token_id, token in tokens.items()
        if parse_explicit_count_expression(str(token.get("text", ""))) is not None
    }

    def is_foreign(item: dict[str, Any], field: str) -> bool:
        return item.get(field) not in {None, "", "unassigned", region_id}

    for index, assignment in enumerate(candidate.assignments):
        prefix = f"assignment[{index}] {assignment.canonical_name}"
        referenced_tokens = set(assignment.token_ids)
        referenced_lines = set(assignment.line_ids)
        if assignment.canonical_name not in allowed_fields:
            issues.append(f"{prefix}: canonical_name outside contract")
        unknown_tokens = sorted(referenced_tokens - tokens.keys())
        unknown_lines = sorted(referenced_lines - lines.keys())
        if unknown_tokens:
            issues.append(f"{prefix}: token_ids outside current payload: {unknown_tokens}")
        if unknown_lines:
            issues.append(f"{prefix}: line_ids outside current payload: {unknown_lines}")
        foreign_tokens = sorted(
            token_id for token_id in referenced_tokens & tokens.keys()
            if is_foreign(tokens[token_id], "source_region_id")
        )
        foreign_lines = sorted(
            line_id for line_id in referenced_lines & lines.keys()
            if is_foreign(lines[line_id], "source_region_id")
        )
        foreign_groups = [
            group.get("dimension_id", "<unknown>") for group in groups
            if referenced_tokens.intersection(group.get("token_ids", []))
            and is_foreign(group, "view_id")
        ]
        if foreign_tokens or foreign_lines or foreign_groups:
            issues.append(
                f"{prefix}: cross-view evidence: tokens={foreign_tokens}, "
                f"lines={foreign_lines}, groups={foreign_groups}"
            )
        if referenced_tokens & context_ids:
            issues.append(f"{prefix}: context token_ids cannot supply dimensions: {sorted(referenced_tokens & context_ids)}")
        has_explicit_count = (
            assignment.canonical_name in _COUNT_FIELDS
            and bool(referenced_tokens & explicit_count_ids)
        )
        referenced_numeric = referenced_tokens & numeric_ids
        if not referenced_numeric and not has_explicit_count:
            issues.append(f"{prefix}: no numeric value or explicit count evidence")
        table_values = referenced_numeric & table_ids
        # A table cell cannot exempt unrelated ordinary dimensions from lines.
        is_table_value = bool(table_values) and referenced_numeric <= table_ids
        is_self_contained_count = has_explicit_count and not referenced_numeric
        if not referenced_lines and not (is_table_value or is_self_contained_count):
            issues.append(f"{prefix}: ordinary dimensions require current line_ids")
        if is_table_value:
            if len(table_values) != 1:
                issues.append(f"{prefix}: table assignment requires exactly one numeric value cell")
            matching_rows = [
                group for group in groups if group.get("evidence_type") == "table_row"
                and table_values.intersection(group.get("table_columns", {}).values())
            ]
            if matching_rows and not any(
                referenced_tokens <= (
                    set(group.get("token_ids", [])) | set(group.get("row_label_token_ids", []))
                )
                and referenced_lines <= (
                    set(group.get("dimension_line_ids", [])) | set(group.get("extension_line_ids", []))
                )
                for group in matching_rows
            ):
                issues.append(f"{prefix}: table references do not belong to a single current row")
    return issues
