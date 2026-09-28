"""STEP 工作流的人工询问、回答校验与审核节点。

问题由 LangGraph interrupt 持久化。只有显式 resume 才消费回答；
人工信息只用于封装身份和审核，不充当 OCR 尺寸证据。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from langgraph.types import interrupt

from backend.agents.step.families.taxonomy import (
    FAMILY_LOCATIONS,
    resolve_family_selection,
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
    action = _text(answer.get("action"), "action", required=True)
    stage = request.get("stage")
    stage_actions = {
        "package": ({"provide", "auto", "cancel"}, "封装询问只接受 provide、auto 或 cancel"),
        "routing": ({"confirm", "change", "cancel"}, "路由询问只接受 confirm、change 或 cancel"),
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
