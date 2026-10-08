"""PCB 封装工作流的人工询问、回答校验与审核节点。

问题由 LangGraph ``interrupt`` 持久化；只有显式 resume 才消费回答。
人工确认与自动派生结果分开存放，不互相冒充。

两个停点：

* ``spec_confirmation`` —— 参数校验通过后、写第一个几何对象之前，让人确认；
* ``review`` —— 三阶段跑完、判据结论出来后，让人决定是否采纳产物。

坏参数（铜箔间距不足等）**不走人工询问**，而是直接落到
``stopped_invalid_spec`` 终态：这类问题不该让人用"确认"掩盖过去。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

#: 各阶段允许的动作与出错文案。
_STAGE_ACTIONS: dict[str, tuple[set[str], str]] = {
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
