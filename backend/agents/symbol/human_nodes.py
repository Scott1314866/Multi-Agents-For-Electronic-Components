"""符号生成工作流的人工询问、回答校验与审核节点。

源程序的 ``cli.py`` 有五个交互卡点，这里一一对应：

===================  ============================================  ============
卡点                  什么时候问                                    源程序位置
===================  ============================================  ============
``ask_device``        文件名认不出型号且有多个候选                   ``cli.py:158``
``ask_package``       手册里有多种封装                               ``cli.py:167``
``resolve_conflicts`` 双通道冲突未裁决                             ``cli.py:84-95``
``resolve_review_diffs`` 独立复核与提取不一致                      ``cli.py:97-119``
``confirm_output``    写入正式库之前                                 ``cli.py:188``
===================  ============================================  ============

外加一个 ``ask_check_questions``：源程序的 ``selfcheck`` **从未被 CLI 调用过**
（所以"100 个引脚全堆左边"的废符号也能报 PASS），迁移后它成为必经节点，
它提出的问题就在这里问。

纪律：**冲突与差异一律停下来问人，绝不猜**。复核差异中若两侧是 O/0 这类
易混字形，源程序把默认值改成"复核值"（``cli.py:103-108``）—— 这个判断留在
节点里做，提示词里把依据一并给操作员。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from langgraph.types import interrupt

#: 各阶段允许的动作与出错文案。
_STAGE_ACTIONS: dict[str, tuple[set[str], str]] = {
    "device": ({"choose", "provide", "cancel"}, "型号询问只接受 choose、provide 或 cancel"),
    "package": ({"choose", "provide", "cancel"}, "封装询问只接受 choose、provide 或 cancel"),
    "conflicts": ({"resolve", "cancel"}, "冲突裁决只接受 resolve 或 cancel"),
    "review_diffs": ({"resolve", "cancel"}, "复核差异裁决只接受 resolve 或 cancel"),
    "facts": ({"provide", "cancel"}, "参数补充只接受 provide 或 cancel"),
    "output": ({"confirm", "cancel"}, "输出确认只接受 confirm 或 cancel"),
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


def _validate_resolutions(answer: dict, request: dict) -> list[dict]:
    """校验一批裁决项：只能裁决当前列出的冲突，不能凭空造。"""
    raw = answer.get("resolutions")
    if not isinstance(raw, list) or not raw:
        raise ValueError("resolutions 必须是非空数组")
    allowed = {
        (str(item.get("pin_number")), str(item.get("field")))
        for item in (request.get("items") or [])
    }
    normalized: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("每条裁决必须是 JSON 对象")
        key = (str(entry.get("pin_number") or ""), str(entry.get("field") or ""))
        if key not in allowed:
            raise ValueError(f"当前没有待裁决的 {key[0]} / {key[1]}")
        if key in seen:
            raise ValueError(f"重复裁决 {key[0]} / {key[1]}")
        seen.add(key)
        value = _text(entry.get("value"), "value", required=True, limit=200)
        if key[1] == "side" and value not in {"left", "right", "top", "bottom", "unknown"}:
            raise ValueError("side 只能是 left、right、top、bottom 或 unknown")
        normalized.append(
            {
                "pin_number": key[0],
                "field": key[1],
                "value": value,
            }
        )
    if seen != allowed:
        raise ValueError("请裁决全部待处理项")
    return normalized


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
    options = request.get("options")
    if options is not None:
        if not isinstance(options, (list, tuple)) or action not in options:
            raise ValueError(f"当前问题不提供 {action} 操作，请从 options 中选择")

    normalized: dict[str, Any] = {"action": action}
    if stage in {"device", "package"}:
        if action == "choose":
            value = _text(answer.get("value"), "value", required=True, limit=200)
            candidates = request.get("candidates") or []
            if candidates and value not in candidates:
                raise ValueError("请从 candidates 中选择，或改用 provide 手工填写")
            normalized["value"] = value
        elif action == "provide":
            optional = stage == "package" and not request.get("candidates")
            normalized["value"] = _text(answer.get("value"), "value", required=not optional, limit=200)
    elif stage in {"conflicts", "review_diffs"}:
        if action == "resolve":
            normalized["resolutions"] = _validate_resolutions(answer, request)
    elif stage == "facts":
        if action == "cancel":
            normalized["comment"] = _text(answer.get("comment"), "comment", limit=2000)
            return normalized
        raw = answer.get("values")
        if not isinstance(raw, dict) or not raw:
            raise ValueError("values 必须是非空对象")
        values = {
            _text(key, "key", required=True, limit=100): _text(value, "value", limit=200)
            for key, value in raw.items()
        }
        questions = {str(item.get("key")): item for item in request.get("items") or []}
        if set(values) != set(questions):
            raise ValueError("请仅回答当前列出的全部问题")
        normalized["values"] = {
            key: value or str(questions[key].get("default") or "")
            for key, value in values.items()
        }
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
        # _actor 由 API 认证上下文注入，不接受客户端自报身份。
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


def _automatic(value: str, candidate: str, count: int, label: str) -> str | None:
    """候选唯一、或已被外部指定时，不必打扰人。

    Returns:
        直接采用的值；``None`` 表示需要问人。
    """
    if value:
        return value  # 用户填的优先级最高
    if count == 1 and candidate:
        return candidate
    return None


async def ask_device_node(state: dict[str, Any]) -> dict[str, Any]:
    """确定目标型号。

    优先级：**用户填的 > 文件名 > 表格单元格 > 文件名原文**（§11.6 第 1 条）。
    """
    resolved = str(state.get("device") or "")
    candidates = list(state.get("device_candidates") or [])
    picked = _automatic(resolved, candidates[0] if candidates else "", len(candidates), "型号")
    if picked:
        return {
            "device": picked,
            "device_candidates": candidates,
            "human_device": {"action": "auto", "value": picked, "source": "auto"},
            "human_history": _history(state, "device", {"action": "auto", "value": picked}),
            "status": "device_resolved",
        }
    if not candidates:
        # 一个候选都没有：让操作员直接填，这是源程序的逃生舱口。
        answer = _ask(
            {
                "stage": "device",
                "question": "没能自动识别型号，请手工输入。",
                "options": ["provide", "cancel"],
                "candidates": [],
                "fields": {"value": "必填，例如 STM32F105VCT6"},
            }
        )
    else:
        answer = _ask(
            {
                "stage": "device",
                "question": "手册里有多个型号，请指定目标型号。",
                "options": ["choose", "provide", "cancel"],
                "candidates": candidates,
                "fields": {"value": "choose 时必填"},
            }
        )
    if answer["action"] == "cancel":
        return _cancelled(state, "device", answer)
    return {
        "device": answer["value"],
        "human_device": answer,
        "human_history": _history(state, "device", answer),
        "status": "device_chosen",
    }


async def ask_package_node(state: dict[str, Any]) -> dict[str, Any]:
    """确定封装代码。允许留空（有些手册不需要指定封装）。"""
    resolved = str(state.get("package") or "")
    candidates = list(state.get("package_candidates") or [])
    picked = _automatic(resolved, candidates[0] if candidates else "", len(candidates), "封装")
    if picked:
        return {
            "package": picked,
            "human_package": {"action": "auto", "value": picked, "source": "auto"},
            "human_history": _history(state, "package", {"action": "auto", "value": picked}),
            "status": "package_resolved",
        }
    if not candidates:
        answer = _ask(
            {
                "stage": "package",
                "question": "没能自动识别封装，请手工输入（可留空）。",
                "options": ["provide", "cancel"],
                "candidates": [],
                "fields": {"value": "可留空"},
            }
        )
        if answer["action"] == "provide" and not answer.get("value"):
            answer["value"] = ""
    else:
        answer = _ask(
            {
                "stage": "package",
                "question": "手册里有多种封装，请指定封装代码。",
                "options": ["choose", "provide", "cancel"],
                "candidates": candidates,
                "fields": {"value": "choose 时必填"},
            }
        )
    if answer["action"] == "cancel":
        return _cancelled(state, "package", answer)
    return {
        "package": answer["value"],
        "human_package": answer,
        "human_history": _history(state, "package", answer),
        "status": "package_chosen",
    }


def _apply_pin_resolutions(state: dict, chosen: dict[tuple[str, str], str]) -> list[dict]:
    """Apply reviewed values to copies, preserving the stored evidence."""
    pins = deepcopy(state.get("merged_pins") or [])
    for pin in pins:
        number = str(pin.get("pin_number") or "")
        for field in ("name", "side", "type", "description"):
            if (number, field) in chosen:
                pin[field] = chosen[(number, field)]
    return pins


async def resolve_conflicts_node(state: dict[str, Any]) -> dict[str, Any]:
    """逐条裁决双通道冲突。**不猜** —— 源程序的核心纪律之一。"""
    conflicts = deepcopy(state.get("conflicts") or [])
    unresolved = [item for item in conflicts if item.get("resolved") is None]
    if not unresolved:
        return {"status": "conflicts_clear"}

    items = [
        {
            "pin_number": str(item.get("pin_number") or ""),
            "field": str(item.get("field") or ""),
            "values": item.get("values") or {},
            "pages": item.get("pages") or [],
        }
        for item in unresolved
    ]
    answer = _ask(
        {
            "stage": "conflicts",
            "question": f"有 {len(items)} 条双通道冲突需要裁决：请为每条指定采信的值。",
            "options": ["resolve", "cancel"],
            "items": items,
            "fields": {"resolutions": '[{"pin_number": "1", "field": "side", "value": "left"}]'},
        }
    )
    if answer["action"] == "cancel":
        return _cancelled(state, "conflicts", answer)

    chosen = {
        (item["pin_number"], item["field"]): item["value"]
        for item in answer["resolutions"]
    }
    for conflict in conflicts:
        key = (str(conflict.get("pin_number") or ""), str(conflict.get("field") or ""))
        if key in chosen:
            conflict["resolved"] = chosen[key]

    return {
        "conflicts": conflicts,
        "merged_pins": _apply_pin_resolutions(state, chosen),
        "human_conflicts": answer,
        "human_history": _history(state, "conflicts", answer),
        "status": "conflicts_resolved",
    }


async def resolve_review_diffs_node(state: dict[str, Any]) -> dict[str, Any]:
    """逐条裁决"提取 vs 复核"的差异。

    源程序在这里有一条特别的判断：若两侧是 O/0 这类**易混字形**，默认值改成
    "复核值"而不是"提取值"（``ocr_confusable``）。把默认值一并交给操作员，
    但**绝不自动改**。
    """
    from backend.agents.symbol.tools.review import ocr_confusable

    review = deepcopy(state.get("review") or {})
    mismatches = [item for item in review.get("mismatches") or [] if item.get("resolved") is None]
    if not mismatches:
        return {"status": "review_clear"}

    items = []
    for item in mismatches:
        expected = str(item.get("expected") or "")
        observed = str(item.get("observed") or "")
        items.append(
            {
                "pin_number": str(item.get("pin_number") or ""),
                "field": str(item.get("field") or ""),
                "expected": expected,
                "observed": observed,
                "suggested": observed if ocr_confusable(expected, observed) else expected,
                "confusable": ocr_confusable(expected, observed),
            }
        )
    answer = _ask(
        {
            "stage": "review_diffs",
            "question": f"独立复核与提取有 {len(items)} 处差异，请逐条指定采信值。",
            "options": ["resolve", "cancel"],
            "items": items,
            "fields": {
                "resolutions": '[{"pin_number": "1", "field": "name", "value": "ADDR"}]'
            },
        }
    )
    if answer["action"] == "cancel":
        return _cancelled(state, "review_diffs", answer)

    chosen = {
        (item["pin_number"], item["field"]): item["value"]
        for item in answer["resolutions"]
    }
    for item in review["mismatches"]:
        key = (str(item.get("pin_number") or ""), str(item.get("field") or ""))
        if key in chosen:
            item["resolved"] = chosen[key]

    return {
        "review": review,
        "merged_pins": _apply_pin_resolutions(state, chosen),
        "human_review_diffs": answer,
        "human_history": _history(state, "review_diffs", answer),
        "status": "review_diffs_resolved",
    }


async def ask_check_questions_node(state: dict[str, Any]) -> dict[str, Any]:
    """把自检提出的问题交给操作员。

    源程序的 ``selfcheck`` 把问题设计成了 ``Question``（``key`` / ``prompt`` /
    ``options`` / ``default``），几乎就是为 ``interrupt`` 准备的。
    """
    report = dict(state.get("check_report") or {})
    questions = list(report.get("questions") or [])
    if not questions:
        return {"status": "no_check_questions"}

    items = [
        {
            "key": str(item.get("key") or ""),
            "prompt": str(item.get("prompt") or ""),
            "options": list(item.get("options") or []),
            "default": str(item.get("default") or ""),
        }
        for item in questions
    ]
    answer = _ask(
        {
            "stage": "facts",
            "question": f"自检提出 {len(items)} 个需要确认的问题。",
            "options": ["provide", "cancel"],
            "items": items,
            "fields": {"values": '{"package": "LQFP100"}'},
        }
    )
    if answer["action"] == "cancel":
        return _cancelled(state, "facts", answer)

    # 参数写回后重跑；更换封装还需重选表格列和引脚图。
    report["answered"] = {**report.get("answered", {}), **answer["values"]}
    rounds = int(state.get("check_rounds") or 0) + 1
    package_changed = answer["values"].get("package", state.get("package", "")) != state.get("package", "")
    return {
        **{key: value for key, value in answer["values"].items() if key in {"device", "package"}},
        "check_report": report,
        "human_facts": answer,
        "human_history": _history(state, "facts", answer),
        "check_rounds": rounds,
        "status": "check_package_changed" if package_changed else "check_answered",
    }


async def confirm_output_node(state: dict[str, Any]) -> dict[str, Any]:
    """写入正式库之前的最后一道闸（源程序默认 N）。"""
    layout = state.get("layout") or {}
    answer = _ask(
        {
            "stage": "output",
            "question": "确认生成 .OLB / .DSN 到这个目录？",
            "options": ["confirm", "cancel"],
            "output_dir": state.get("output_dir") or "",
            "part_name": layout.get("part_name", ""),
            "summary": layout.get("summary", ""),
            "warnings": [
                *(state.get("warnings") or []),
                *(state.get("table_warnings") or []),
                *layout.get("warnings", []),
                *[item["title"] for item in (state.get("check_report") or {}).get("findings", []) if item.get("severity") == "warn"],
            ],
            "fields": {"comment": "可留空"},
        }
    )
    if answer["action"] == "cancel":
        return _cancelled(state, "output", answer)
    return {
        "human_output": answer,
        "human_history": _history(state, "output", answer),
        "status": "output_confirmed",
    }
