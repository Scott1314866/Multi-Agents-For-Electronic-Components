"""STEP 工作流的人工询问、回答校验与审核节点。

问题由 LangGraph interrupt 持久化。只有显式 resume 才消费回答；
人工尺寸以独立来源保存，不伪装成 OCR token 或图像坐标。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from langgraph.types import interrupt

from backend.agents.step.families.taxonomy import (
    FAMILY_LOCATIONS,
    resolve_family_selection,
)
from backend.agents.step.dimension_input import (
    expected_unit,
    parse_dimension_answer,
    parse_package_pin_count,
)


MIN_ROUTING_CONFIDENCE = 0.80
_IDENTITY_FIELDS = {
    "family_id", "category_id", "subcategory_id", "package_type",
    "resistor_or_capacitor", "supported_family",
}


def _text(value: Any, name: str, *, required: bool = False, limit: int = 500) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ValueError(f"请填写 {name}")
    if len(value) > limit:
        raise ValueError(f"{name} 不能超过 {limit} 个字符")
    return value


def validate_human_answer(request: dict, answer: dict) -> dict:
    """校验阶段特有的回答，并剔除客户端提交的 state/审核人等额外字段。"""
    if not isinstance(answer, dict):
        raise ValueError("answer 必须是 JSON 对象")
    stage = request.get("stage")
    raw_action = answer.get("action")
    if stage == "dimensions" and not raw_action:
        utterance = str(answer.get("text") or "")
        raw_action = "cancel" if re.search(r"取消任务|取消|停止生成|终止任务|不做了", utterance) else "provide"
    action = _text(raw_action, "action", required=True)
    stage_actions = {
        "package": ({"provide", "auto", "cancel"}, "封装询问只接受 provide、auto 或 cancel"),
        "routing": ({"confirm", "change", "cancel"}, "路由询问只接受 confirm、change 或 cancel"),
        "dimensions": ({"provide", "cancel"}, "参数补充只接受 provide 或 cancel"),
        "review": ({"approve", "reject"}, "审核只接受 approve 或 reject"),
    }
    if stage not in stage_actions:
        raise ValueError("未知的人工询问阶段")
    allowed_actions, error = stage_actions[stage]
    if action not in allowed_actions:
        raise ValueError(error)
    # 旧 checkpoint 可能未带 options；已带选项的问题必须严格按当前选项回答。
    if "options" in request and (
        not isinstance(request["options"], (list, tuple)) or action not in request["options"]
    ):
        raise ValueError(f"当前问题不提供 {action} 操作，请从 options 中选择")
    normalized: dict[str, Any] = {"action": action}
    if stage == "package":
        if action == "provide":
            normalized["package_type"] = _text(
                answer.get("package_type"), "package_type", required=True, limit=200
            )
    elif stage == "routing":
        if action != "cancel":
            from backend.agents.step.families.registry import image_family_catalog

            source = request.get("suggested", {}) if action == "confirm" else answer
            if not isinstance(source, dict):
                raise ValueError("当前没有可确认的模板，请改选已实现的模板或取消")
            family_id = _text(source.get("family_id"), "family_id", required=True)
            selection = resolve_family_selection(
                family_id,
                category_id=_text(source.get("category_id"), "category_id"),
                subcategory_id=_text(source.get("subcategory_id"), "subcategory_id") or None,
            )
            if selection["status"] != "resolved":
                raise ValueError(selection["reason"] or "请指定目录中的明确模板")
            if selection["family_id"] not in image_family_catalog():
                raise ValueError("所选模板尚未实现，请选择 candidates 中 implemented=true 的模板或取消")
            normalized.update({
                "family_id": selection["family_id"],
                "category_id": selection["category_id"],
                "subcategory_id": selection["subcategory_id"],
                "package_type": _text(source.get("package_type"), "package_type", limit=200),
            })
    elif stage == "review":
        if action == "approve" and not request.get("can_approve", False):
            raise ValueError("产物未通过验证，不能批准")
    elif stage == "dimensions":
        normalized = parse_dimension_answer(request, {**answer, "action": action})
    normalized["comment"] = _text(answer.get("comment"), "comment", limit=2000)
    return normalized


def _ask(request: dict) -> dict:
    """无效的直接图调用回答会重新询问；API 在 resume 前使用相同校验。"""
    while True:
        raw_answer = interrupt(request)
        try:
            answer = validate_human_answer(request, raw_answer)
        except ValueError as exc:
            request = {**request, "validation_error": str(exc)}
            continue
        # _actor 由 API 认证上下文注入，不接受 API 客户端自行指定。
        actor = raw_answer.get("_actor", {})
        if isinstance(actor, dict):
            answer.update({
                key: str(actor[key])
                for key in ("user_id", "answered_at")
                if actor.get(key)
            })
        return answer


def _history(state: dict, stage: str, answer: dict) -> list[dict]:
    return [*state.get("human_history", []), {"stage": stage, **answer}]


def _cancelled(state: dict, stage: str, answer: dict) -> dict:
    return {
        "status": "cancelled",
        "needs_review": False,
        "human_history": _history(state, stage, answer),
        "result": {"status": "cancelled", "stage": stage, "reason": answer.get("comment", "")},
    }


async def ask_package_node(state: dict[str, Any]) -> dict[str, Any]:
    """开始时询问封装；用户可以明确选择由 Agent 自行识别。"""
    # The public input adapter always supplies a natural-language request. In
    # that path, let the image evidence and JEV route determine the package;
    # asking for an internal package label here would defeat the adapter.
    human_request = str(state.get("human_request") or "").strip()
    if human_request:
        import re

        package_hint = ""
        match = re.search(
            r"(?i)\b(?:SOP|SOIC|SSOP|TSSOP|QSOP|MSOP|QFN|DFN|LQFP|TQFP|BGA|DIP|SOT)[- ]?\d{1,3}\b",
            human_request,
        )
        if match:
            package_hint = re.sub(r"\s+", "", match.group(0)).upper()
        return {
            "human_package": {
                "action": "auto",
                "package_type": package_hint,
                "source": "natural_language_request",
            },
            "package_type_hint": package_hint,
            "human_history": _history(state, "package", {
                "action": "auto",
                "source": "natural_language_request",
                "package_type": package_hint,
            }),
            "status": "package_auto_detect",
        }
    answer = _ask({
        "stage": "package",
        "question": "这张工程图需要生成什么封装？请填写封装名称；不确定可选择自动识别。",
        "options": ["provide", "auto", "cancel"],
        "fields": {"package_type": "provide 时必填，例如 TSSOP-16、LQFP64"},
    })
    if answer["action"] == "cancel":
        return _cancelled(state, "package", answer)
    return {
        "human_package": answer,
        "package_type_hint": answer.get("package_type", ""),
        "human_history": _history(state, "package", answer),
        "status": "package_confirmed",
    }


def _package_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def routing_question(state: dict[str, Any]) -> dict | None:
    """仅当身份分类不明确、置信度不足或与人工封装输入冲突时询问。"""
    # registry 也被 drawing 的兼容导出层引用，节点执行时再加载以免循环导入。
    from backend.agents.step.families.registry import image_family_catalog

    supported = image_family_catalog()
    classification = dict(state.get("view_classification") or {})
    selection = resolve_family_selection(
        str(classification.get("family_id") or ""),
        category_id=str(classification.get("category_id") or ""),
        subcategory_id=classification.get("subcategory_id"),
        identity_texts=[
            str(token.get("text") or "")
            for token in state.get("all_ocr_tokens", [])
            if float(token.get("confidence", 0)) >= 0.80
        ],
    )
    reasons: list[str] = []
    if selection["status"] != "resolved":
        reasons.append(selection["reason"])
    else:
        classification.update({
            name: selection[name] for name in ("family_id", "category_id", "subcategory_id")
        })
        if selection["family_id"] not in supported:
            reasons.append("识别出的模板尚未实现，可改选模板或取消任务")
    if float(classification.get("overall_confidence", 0)) < MIN_ROUTING_CONFIDENCE:
        reasons.append("器件分类置信度低于 0.80")
    if any(
        str(item).split(":")[-1] in _IDENTITY_FIELDS
        for item in classification.get("unresolved_fields", [])
    ):
        reasons.append("仍有未确定的器件身份字段")
    if classification.get("ambiguities"):
        reasons.append("视图分类存在歧义，需要确认器件身份")
    hint = _package_key(str(state.get("package_type_hint") or ""))
    detected = _package_key(str(classification.get("package_type") or ""))
    if hint and (not detected or (hint not in detected and detected not in hint)):
        reasons.append("人工填写的封装与识别结果不一致")
    if not reasons:
        return None
    can_confirm = selection["status"] == "resolved" and selection["family_id"] in supported
    return {
        "stage": "routing",
        "question": (
            "进入 Jev 模板路由前，请确认或修改器件分类和封装。改选仅支持 implemented=true 的模板。"
            if can_confirm else
            "当前没有可执行的建议模板，请选择 implemented=true 的模板或取消任务。"
        ),
        "reasons": reasons,
        "options": (["confirm"] if can_confirm else []) + (["change"] if supported else []) + ["cancel"],
        "suggested": classification,
        "package_type_hint": state.get("package_type_hint", ""),
        "candidates": [
            {"family_id": family_id, "implemented": family_id in supported}
            for family_id in FAMILY_LOCATIONS
        ],
        "fields": {
            "family_id": "change 时必填，只能选择 candidates 中 implemented=true 的模板",
            "package_type": "change 时可选",
        },
    }


async def confirm_template_node(state: dict[str, Any]) -> dict[str, Any]:
    """在 Jev 之前按需确认身份，不修改原始 OCR 或任何尺寸证据。"""
    request = routing_question(state)
    if request is None:
        return {"status": "routing_ready"}
    answer = _ask(request)
    if answer["action"] == "cancel":
        return _cancelled(state, "routing", answer)
    classification = dict(state["view_classification"])
    classification.update({
        name: answer[name] for name in ("family_id", "category_id", "subcategory_id")
    })
    if answer.get("package_type"):
        classification["package_type"] = answer["package_type"]
    from backend.agents.step.families.registry import image_family_catalog

    contract = image_family_catalog().get(answer["family_id"])
    if contract:
        classification["identified_features"] = list(contract["required_features"])
    classification["unresolved_fields"] = [
        item for item in classification.get("unresolved_fields", [])
        if str(item).split(":")[-1] not in _IDENTITY_FIELDS
    ]
    return {
        "view_classification": classification,
        "human_route": answer,
        "human_history": _history(state, "routing", answer),
        "status": "routing_confirmed",
    }


def _store_operator_dimensions(
    state: dict[str, Any], values: dict[str, dict[str, Any]], question_id: str,
    *, actor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add explicit operator evidence while preserving its distinct provenance."""
    from backend.agents.step.vision.schemas import FusedEvidence, FusedParameter

    fused = FusedEvidence.model_validate(state["fused_evidence"])
    parameters = {item.canonical_name: item for item in fused.parameters}
    before = {
        field: [item.model_dump(mode="json") for item in fused.parameters if item.canonical_name == field]
        for field in values
    }
    for field, slot in values.items():
        if not isinstance(slot, dict):
            slot = {"value": slot, "unit": expected_unit(field)}
        unit = str(slot["unit"])
        value = float(slot["value"])
        parameters[field] = FusedParameter(
            canonical_name=field,
            value=value,
            unit=unit,
            evidence_ids=[f"human_input:{question_id}:{field}"],
            token_ids=[],
            line_ids=[],
            token_bboxes=[],
            target_feature=f"operator_confirmed:{field}",
            ocr_confidence=0.0,
            semantic_confidence=0.0,
            raw_texts=[f"operator supplied {field}={value:g} {unit}"],
            evidence_kind="human_input",
        )
    # Derived slots depend on their source values. Drop them before the graph
    # recomputes deterministic family relations after this operator update.
    invalidated_derived_fields = {
        name for name, item in parameters.items() if item.evidence_kind == "derived"
    }
    parameters = {
        name: item for name, item in parameters.items()
        if item.evidence_kind != "derived"
    }
    def conflict_is_overridden(conflict: str) -> bool:
        if conflict in values or conflict in invalidated_derived_fields:
            return True
        return any(
            re.search(rf"(?<![\w]){re.escape(field)}(?![\w])", conflict)
            for field in {*values, *invalidated_derived_fields}
        )

    fused = fused.model_copy(update={
        "parameters": list(parameters.values()),
        "conflicting_fields": [
            name for name in fused.conflicting_fields if not conflict_is_overridden(name)
        ],
        "unit_conflicts": [
            name for name in fused.unit_conflicts
            if name not in values and name not in invalidated_derived_fields
        ],
        "missing_evidence_assignments": [
            name for name in fused.missing_evidence_assignments
            if name not in values and name not in invalidated_derived_fields
        ],
        "unresolved_fields": [
            name for name in fused.unresolved_fields
            if name not in values and name not in invalidated_derived_fields
        ],
    })
    entry = {
        "question_id": question_id,
        "values": values,
        "superseded_candidates": before,
        **(actor or {}),
    }
    return {
        "fused_evidence": fused.model_dump(mode="json"),
        "human_dimension_history": [*state.get("human_dimension_history", []), entry],
    }


_CONFLICT_FIELD_HINTS = {
    "pin_span": "请核对同一排引脚最外侧两只引脚的中心距。",
    "terminal_pitch": "请核对相邻引脚中心之间的间距。",
    "nominal_pin_count": "请核对器件引脚总数；每侧引脚数由总数和封装布局决定。",
    "total_height": "请核对从引脚落地面到封装顶部的总高度。",
    "housing_height": "请核对塑封本体自身的高度。",
    "body_standoff": "请核对引脚落地面到塑封本体底面的距离。",
    "body_length": "请核对沿同一排引脚排列方向的塑封本体长度。",
    "terminal_width": "请核对单只引脚沿排列方向的宽度。",
}


def _dimension_conflict_details(state: dict[str, Any]) -> list[str]:
    conflicts = [str(item) for item in (state.get("dimension_gate") or {}).get("conflicting_fields", [])]
    details: list[str] = []
    for conflict in conflicts:
        if "pin_span" in conflict and "pitch" in conflict:
            details.append("引脚中心距关系不一致：最外侧引脚中心距应等于（每侧引脚数 - 1）× 相邻引脚间距。")
        elif "total_height" in conflict and "housing_height" in conflict:
            details.append("高度关系不一致：总体高度应等于塑封本体高度 + 本体离板高度。")
        elif "body_length" in conflict and "pin_span" in conflict:
            details.append("本体长度与引脚排列不匹配：本体长度应能容纳同排首末引脚中心距及一只引脚的宽度；请核对俯视图顶部的本体长度标注。")
        else:
            details.append(f"参数关系不一致：{conflict}。请重新核对相关尺寸。")
    return list(dict.fromkeys(details))


def _dimension_question(state: dict[str, Any], fields: list[str], question_id: str) -> dict[str, Any]:
    from backend.agents.step.families.registry import image_family_catalog

    fused = state.get("fused_evidence") or {}
    contract = image_family_catalog().get(fused.get("family_id"), {})
    guidance = contract.get("parameter_guidance", {})
    existing: dict[str, list[dict[str, Any]]] = {}
    for parameter in fused.get("parameters", []):
        if parameter.get("canonical_name") in fields:
            existing.setdefault(parameter["canonical_name"], []).append({
                "value": parameter.get("value"), "unit": parameter.get("unit"),
                "evidence_kind": parameter.get("evidence_kind"),
            })
    field_specs = {
        field: {
            "unit": expected_unit(field),
            "description": guidance.get(field, field.replace("_", " ")),
            "reason": (
                "图纸未能唯一识别该参数" if field in (state.get("dimension_gate") or {}).get("missing_fields", [])
                else "图纸中的候选值存在冲突或置信度不足"
            ),
            "conflict_explanation": _CONFLICT_FIELD_HINTS.get(field, "请核对这个尺寸，并填写确认后的数值。"),
            "observed_candidates": existing.get(field, []),
        }
        for field in fields
    }
    package_count = parse_package_pin_count(str(state.get("package_type_hint") or ""))
    if package_count is not None and "nominal_pin_count" in field_specs:
        field_specs["nominal_pin_count"]["operator_package_hint"] = {
            "package_type": state.get("package_type_hint"),
            "pin_count": package_count,
        }
    return {
        "stage": "dimensions",
        "question_id": question_id,
        "question": (
            "尺寸之间存在冲突。请只核对下面列出的相关项目并重新填写；"
            "长度单位默认 mm，引脚数填写整数。"
            if (state.get("dimension_gate") or {}).get("conflicting_fields") else
            "尺寸门禁发现参数缺失或置信度不足。请填写下面需要补充的项目；长度单位默认 mm，引脚数填写整数。"
        ),
        "options": ["provide", "cancel"],
        "fields": field_specs,
        "conflict_details": _dimension_conflict_details(state),
        "intent": "fill_missing_or_resolve_conflicting_dimension_slots",
        "examples": [
            "本体高度=1.2 mm；引脚厚度=0.15 mm",
            "{\"values\": {\"housing_height\": {\"value\": 1.2, \"unit\": \"mm\"}}}",
        ],
        "validation_error": state.get("dimension_input_error"),
    }


def actionable_dimension_fields(state: dict[str, Any], *, conflicts_only: bool = False) -> list[str]:
    """Map gate fields and relationship conflicts back to real family slots."""
    from backend.agents.step.families.registry import image_family_catalog

    fused = state.get("fused_evidence") or {}
    contract = image_family_catalog().get(fused.get("family_id"), {})
    allowed = set(contract.get("required_parameters", []))
    gate = state.get("dimension_gate") or {}
    raw = list(gate.get("conflicting_fields", [])) if conflicts_only else [
        *gate.get("missing_fields", []), *gate.get("conflicting_fields", []),
        *gate.get("low_confidence_fields", []),
    ]
    fields: set[str] = set()
    for item in raw:
        name = str(item)
        if name in allowed:
            fields.add(name)
            continue
        if name.startswith("feature:"):
            continue
        # A deterministic chain violation is a relationship, not a slot name.
        fields.update(field for field in allowed if re.search(rf"(?<![\w]){re.escape(field)}(?![\w])", name))
        # Relationship aliases are not canonical parameter names.
        if "pin_span" in name and "pitch" in name:
            fields.update(field for field in ("pin_span", "terminal_pitch", "nominal_pin_count") if field in allowed)
        if "total_height" in name and "housing_height" in name:
            fields.update(field for field in ("total_height", "housing_height", "body_standoff") if field in allowed)
    return sorted(fields)


async def ask_missing_dimensions_node(state: dict[str, Any]) -> dict[str, Any]:
    """Ask an operator to fill dimension slots the drawing cannot resolve."""
    gate = state.get("dimension_gate") or {}
    fused = state.get("fused_evidence") or {}
    pin_count = parse_package_pin_count(str(state.get("package_type_hint") or ""))
    existing_count = next((
        item for item in fused.get("parameters", [])
        if item.get("canonical_name") == "nominal_pin_count"
    ), None)

    # An explicit operator supplied package such as TSSOP-16 provides a pin
    # count identity slot. This is audited as human package input, never OCR.
    if (
        pin_count is not None
        and existing_count is None
        and "nominal_pin_count" in gate.get("missing_fields", [])
    ):
        stored = _store_operator_dimensions(
            {**state, "fused_evidence": fused},
            {"nominal_pin_count": {"value": pin_count, "unit": "count"}},
            "human_package_type",
            actor={"source": "explicit_package_name"},
        )
        fused = stored["fused_evidence"]
        gate = {**gate, "missing_fields": [
            name for name in gate.get("missing_fields", []) if name != "nominal_pin_count"
        ]}
        state = {**state, "fused_evidence": fused, "dimension_gate": gate,
                 "human_dimension_history": [*state.get("human_dimension_history", []),
                                              *stored["human_dimension_history"][-1:]]}
    elif pin_count is not None and existing_count is not None and float(existing_count["value"]) != pin_count:
        conflicts = sorted(set([*gate.get("conflicting_fields", []), "nominal_pin_count"]))
        gate = {**gate, "conflicting_fields": conflicts}
        state = {**state, "dimension_gate": gate}
    fields = actionable_dimension_fields(
        state, conflicts_only=bool(gate.get("conflicting_fields"))
    )
    if not fields:
        updates: dict[str, Any] = {"status": "dimensions_supplied", "fused_evidence": fused}
        history = state.get("human_dimension_history", [])
        if history:
            from backend.agents.step.image_nodes import _write_json
            artifacts = dict(state.get("artifact_paths", {}))
            artifacts["human_dimension_history"] = _write_json(
                Path(state["output_dir"]) / "human_dimension_history.json", history
            )
            updates.update({
                "human_dimension_history": history,
                "artifact_paths": artifacts,
            })
        return updates

    question_id = uuid4().hex
    request = _dimension_question({**state, "fused_evidence": fused}, fields, question_id)
    answer = _ask(request)
    if answer["action"] == "cancel":
        return _cancelled(state, "dimensions", answer)
    actor = {key: answer[key] for key in ("user_id", "answered_at") if answer.get(key)}
    stored = _store_operator_dimensions(
        {**state, "fused_evidence": fused}, answer["values"], question_id, actor=actor,
    )
    dimension_history = [
        *state.get("human_dimension_history", []),
        *stored["human_dimension_history"][-1:],
    ]
    artifacts = dict(state.get("artifact_paths", {}))
    audit_path = Path(state["output_dir"]) / "human_dimension_history.json"
    from backend.agents.step.image_nodes import _write_json
    artifacts["human_dimension_history"] = _write_json(audit_path, dimension_history)
    return {
        "fused_evidence": stored["fused_evidence"],
        "human_dimension_history": dimension_history,
        "artifact_paths": artifacts,
        "human_dimension_rounds": int(state.get("human_dimension_rounds", 0)) + 1,
        "human_history": _history(state, "dimensions", answer),
        "dimension_input_error": None,
        "status": "dimensions_supplied",
    }


async def review_result_node(state: dict[str, Any]) -> dict[str, Any]:
    """候选产物验证后，按比较结果询问批准/拒绝；不回写建模证据。"""
    result = dict(state.get("result") or {})
    step_path = result.get("step_file") or state.get("artifact_paths", {}).get("step")
    previews = result.get("previews") or state.get("preview_paths") or {}
    if (
        not step_path or not Path(step_path).is_file()
        or Path(step_path).stat().st_size == 0
        or state.get("verification", {}).get("passed") is not True
        or not all(previews.get(view) and Path(previews[view]).is_file()
                   for view in ("isometric", "front", "top", "right"))
    ):
        error = "候选 STEP、四视图或几何验证不完整，无法进入人工审核"
        return {
            "status": "failed", "needs_review": False,
            "errors": [*state.get("errors", []), error],
            "result": {**result, "status": "failed", "errors": [error]},
        }
    comparison = state.get("golden_comparison") or {}
    modeling_assumptions = (
        result.get("modeling_assumptions") or state.get("feature_ir", {}).get("assumptions") or []
    )
    result["modeling_assumptions"] = modeling_assumptions
    needs_review = (
        comparison.get("status") != "matched"
        or bool(state.get("needs_review")) or bool(modeling_assumptions)
    )
    update: dict[str, Any] = {"needs_review": False}
    if needs_review:
        answer = _ask({
            "stage": "review",
            "question": "候选 STEP 需要人工审核，请查看预览和验证结果后批准或拒绝。",
            "options": ["approve", "reject"],
            "can_approve": True,
            "step_file": str(step_path),
            "previews": previews,
            "verification": state.get("verification", {}),
            "golden_comparison": comparison,
            "modeling_assumptions": modeling_assumptions,
        })
        final_status = "reviewed" if answer["action"] == "approve" else "rejected"
        update.update({
            "human_review": answer,
            "human_history": _history(state, "review", answer),
        })
        result["human_review"] = answer
    else:
        final_status = "completed"
    result.update({"status": final_status, "needs_review": False})
    result["human_history"] = update.get("human_history", state.get("human_history", []))
    try:
        (Path(state["output_dir"]) / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        return {"status": "failed", "errors": [*state.get("errors", []), str(exc)]}
    update.update({"status": final_status, "result": result})
    return update


def route_after_human_input(state: dict[str, Any]) -> str:
    return "cancelled" if state.get("status") == "cancelled" else "continue"
