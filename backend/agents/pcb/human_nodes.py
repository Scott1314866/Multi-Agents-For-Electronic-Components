"""PCB 封装工作流的人工询问、回答校验与审核节点。

问题由 LangGraph ``interrupt`` 持久化；只有显式 resume 才消费回答。
人工确认与自动派生结果分开存放，不互相冒充。

人工停点：

* ``spec_confirmation`` —— 参数校验通过后、写第一个几何对象之前，让人确认；
* ``image_parameters`` —— 图片识别后核对来源、补充尺寸/编号，替代 JSON 参数确认；
* ``review`` —— 三阶段跑完、判据结论出来后，让人决定是否采纳产物。

坏参数（铜箔间距不足等）**不走人工询问**，而是直接落到
``stopped_invalid_spec`` 终态：这类问题不该让人用"确认"掩盖过去。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

#: 各阶段允许的动作与出错文案。
_STAGE_ACTIONS: dict[str, tuple[set[str], str]] = {
    "image_parameters": ({"submit", "clarify", "cancel"}, "图片参数核对只接受 submit、clarify 或 cancel"),
    "spec_confirmation": ({"confirm", "cancel"}, "参数确认只接受 confirm 或 cancel"),
    "review": ({"approve", "reject"}, "审核只接受 approve 或 reject"),
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
    """校验阶段特有的回答，并剔除客户端提交的额外字段。"""
    if not isinstance(answer, dict):
        raise ValueError("answer 必须是 JSON 对象")
    stage = request.get("stage")
    if stage not in _STAGE_ACTIONS:
        raise ValueError("未知的人工询问阶段")
    allowed, error = _STAGE_ACTIONS[stage]
    action = _text(answer.get("action"), "action", required=True)
    if action not in allowed:
        raise ValueError(error)
    # 已带 options 的问题必须严格按当前选项回答（旧 checkpoint 可能未带）。
    options = request.get("options")
    if options is not None:
        if not isinstance(options, (list, tuple)) or action not in options:
            raise ValueError(f"当前问题不提供 {action} 操作，请从 options 中选择")
    # 产物没过判据时不能批准 —— 与 step 的 review 同一条规矩。
    if stage == "review" and action == "approve" and not request.get("can_approve", False):
        raise ValueError("产物未通过验证，不能批准")

    normalized: dict[str, Any] = {"action": action}
    normalized["comment"] = _text(answer.get("comment"), "comment", limit=2000)
    if stage == "image_parameters" and action == "clarify":
        from backend.agents.pcb.clarification import validate_clarification,apply_clarification
        normalized['values']=validate_clarification(request,answer)
        apply_clarification(request,request,normalized)
    if stage == "image_parameters" and action == "submit":
        from backend.agents.pcb.image_input import validate_image_spec
        patch = answer.get("spec", {})
        if not isinstance(patch, dict):
            raise ValueError("spec 必须是参数对象，可只提交需要补充或修正的字段")
        if request.get("unsupported_reason"):
            raise ValueError("该图片包含当前不支持的几何，请取消并换用可支持的尺寸图")
        merged = {**request.get("spec", {}), **patch}
        policy = answer.get("pad_layout_policy") or (request.get('human_clarification_facts') or {}).get('pad_layout_policy')
        if policy is not None:
            if policy not in {"independent", "source"} or not request.get("grid"):
                raise ValueError("pad_layout_policy 必须是有网格来源的 independent 或 source")
            from backend.agents.pcb.image_input import FootprintGrid, pads_from_grid
            grid = FootprintGrid.model_validate(request["grid"])
            pads = pads_from_grid(grid, split_bridges=policy == "independent")
            reviewed = merged.get("pads")
            if "pads" in patch and (not isinstance(reviewed, list) or len(reviewed) != len(pads)):
                raise ValueError("焊盘核对表的行数与图纸网格不一致，请保留全部焊盘后重新提交")
            if isinstance(reviewed, list) and len(reviewed) == len(pads):
                for index, original in enumerate(reviewed):
                    if not isinstance(original, dict):
                        raise ValueError("焊盘参数必须是对象，请核对焊盘表")
                    # Preserve operator IDs and edits. The selected policy changes
                    # only the vertical geometry of the source's bridged columns.
                    calculated = pads[index]
                    pads[index] = {**calculated, **original}
                    if index % len(grid.bottom_numbers) in grid.bridged_columns:
                        for field in ("y", "size_y"):
                            pads[index][field] = calculated[field]
            merged.update(pads=pads, pin_numbering=[p["number"] for p in pads])
            normalized["pad_layout_policy"] = policy
        spec = validate_image_spec(merged)
        from backend.agents.pcb.pin_reference import pin_reference_issues
        pin_issues = pin_reference_issues([p.model_dump() for p in spec.pads], request.get('pin_reference'))
        if pin_issues:
            raise ValueError('焊盘脚号与来源引脚图不一致：' + '；'.join(pin_issues[:5]))
        from backend.agents.pcb.derive import check_clearances
        issues = check_clearances(spec)
        if issues:
            raise ValueError("焊盘参数未通过间距校验，请修正后重新提交：" + "；".join(issues[:3]))
        normalized["spec"] = spec.model_dump()
        normalized["corrected_fields"] = sorted(patch)
        adjustments = []
        source_pads = (request.get("source_spec") or request.get("spec") or {}).get("pads") or []
        patched_pads = patch.get("pads") or [p.model_dump() for p in spec.pads]
        for index, pad in enumerate(spec.pads):
            if index >= len(source_pads) or index >= len(patched_pads):
                continue
            for field in ("size_x", "size_y"):
                source_value = source_pads[index].get(field)
                requested_value = patched_pads[index].get(field)
                generated_value = getattr(pad, field)
                if source_value is not None and requested_value is not None and float(source_value) != generated_value and float(requested_value) == generated_value:
                    adjustments.append({"row": index + 1, "number": pad.number, "field": field,
                                        "source_value": source_value, "generated_value": generated_value,
                                        "unit": "mm", "source": "human_input"})
        normalized["manufacturing_adjustments"] = adjustments
    return normalized


def _ask(request: dict) -> dict:
    """无效回答会重新询问；API 在 resume 前用同一套校验。"""
    while True:
        raw_answer = interrupt(request)
        try:
            answer = validate_human_answer(request, raw_answer)
        except ValueError as exc:
            request = {**request, "validation_error": str(exc)}
            continue
        # _actor 由 API 认证上下文注入，不接受客户端自行指定身份。
        actor = raw_answer.get("_actor", {})
        if isinstance(actor, dict):
            answer.update(
                {
                    key: str(actor[key])
                    for key in ("user_id", "answered_at")
                    if actor.get(key)
                }
            )
        return answer


def _history(state: dict, stage: str, answer: dict) -> list[dict]:
    return [*state.get("human_history", []), {"stage": stage, **answer}]


def _cancelled(state: dict, stage: str, answer: dict) -> dict:
    return {
        "status": "cancelled",
        "needs_review": False,
        "human_history": _history(state, stage, answer),
        "result": {
            "status": "cancelled",
            "stage": stage,
            "reason": answer.get("comment", ""),
        },
    }


async def confirm_spec_node(state: dict[str, Any]) -> dict[str, Any]:
    """写第一个几何对象之前，让操作员确认参数。

    这一步对应源程序的"确认生成到某目录？[y/N]"闸门（默认 N），
    是产物落地前的最后一道人工关口。
    """
    if state.get("input_kind") == "image" and state.get("human_image_parameters"):
        return {"human_spec_confirmation": state["human_image_parameters"], "status": "spec_confirmed"}
    answer = _ask(
        {
            "stage": "spec_confirmation",
            "question": "参数校验通过。确认按此参数生成 Allegro 封装？",
            "options": ["confirm", "cancel"],
            "spec_name": state.get("spec", {}).get("name", ""),
            "summary": state.get("derived_summary", ""),
            "warnings": state.get("warnings", []),
            "work_dir": state.get("work_dir", ""),
            "fields": {"comment": "可留空；如需说明改动缘由请填写"},
        }
    )
    if answer["action"] == "cancel":
        return _cancelled(state, "spec_confirmation", answer)
    return {
        "human_spec_confirmation": answer,
        "human_history": _history(state, "spec_confirmation", answer),
        "status": "spec_confirmed",
    }


async def confirm_image_parameters_node(state: dict[str, Any]) -> dict[str, Any]:
    """Review partial extraction before PackageSpec requires every dimension."""
    reading = state.get("image_reading") or {}
    unsupported = reading.get("unsupported_reason", "")
    from backend.agents.pcb.clarification import plan_questions,apply_clarification
    questions=plan_questions(state)
    request={
        "stage": "image_parameters",
        "question": "请核对图片参数及来源，补充缺失尺寸/编号后提交生成。所有尺寸使用毫米。",
        "options": ["cancel"] if unsupported else (["clarify", "submit", "cancel"] if questions else ["submit", "cancel"]),
        "spec": state.get("spec", {}),
        "issues": state.get("extraction_issues", []),
        "evidence": reading.get("evidence", {}),
        "grid": reading.get("grid"),
        "source_units": reading.get("source_units", "unknown"),
        "unsupported_reason": unsupported,
        "warnings": state.get("warnings", []),
        "questions": questions,
        "human_clarification_facts": state.get('human_clarification_facts',{}),
        "human_manufacturing_adjustments": state.get('human_manufacturing_adjustments',[]),
        "source_spec": state.get('image_source_spec',{}),
        "pin_reference": reading.get('pin_reference'),
        "human_history": state.get('human_history',[]),
        "fields": {"spec": "需要补充或修正的 PackageSpec 字段；pads 包含完整编号、中心坐标和铜箔尺寸",
                   "pad_layout_policy": "网格含连通盘时需明确：independent=按普通盘长度拆分，source=保留连通铜箔",
                   "comment": "可留空"},
    }
    answer = _ask(request)
    if answer["action"] == "cancel":
        return _cancelled(state, "image_parameters", answer)
    if answer['action']=='clarify':
        changes=apply_clarification(state,request,answer)
        pending=plan_questions({**state,**changes})
        return {**changes,'extraction_issues':[q['title'] for q in pending],
                'human_history':_history(state,'image_clarification',{**answer,'questions':[q for q in questions if q['id'] in answer['values']]}),
                'status':'image_clarified'}
    return {"spec": answer["spec"], "human_image_parameters": answer,
            "extraction_issues": [],
            "human_history": _history(state, "image_parameters", answer), "status": "image_parameters_confirmed"}


def route_image_confirmation(state: dict) -> str:
    if state.get('status')=='image_clarified':return 'clarify'
    return 'cancelled' if state.get('status')=='cancelled' else 'continue'


async def review_result_node(state: dict[str, Any]) -> dict[str, Any]:
    """产物与判据都出来后，让操作员决定是否采纳。"""
    verdict = state.get("verdict") or {}
    answer = _ask(
        {
            "stage": "review",
            "question": "封装已生成完毕，请核验判据结果与产物后决定是否采纳。",
            "options": ["approve", "reject"],
            "can_approve": bool(verdict.get("passed")),
            "verdict": verdict,
            "artifacts": state.get("artifact_paths", {}),
            "summary": state.get("derived_summary", ""),
            "fields": {"comment": "reject 时建议说明原因"},
        }
    )
    approved = answer["action"] == "approve"
    return {
        "human_review": answer,
        "needs_review": not approved,
        "human_history": _history(state, "review", answer),
        "status": "reviewed" if approved else "rejected",
        "result": {
            "status": "reviewed" if approved else "rejected",
            "spec_name": state.get("spec", {}).get("name", ""),
            "verdict": verdict,
            "artifacts": state.get("artifact_paths", {}),
            "comment": answer.get("comment", ""),
        },
    }
