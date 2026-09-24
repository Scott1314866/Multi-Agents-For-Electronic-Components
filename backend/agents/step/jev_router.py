"""使用 Jev Choice 对工程图进行 CAD 模板类别路由。

Jev 只消费 OCR/Qwen 已形成的紧凑结构化证据，不读取原图、不提取尺寸，
也不生成 Feature IR。API 未配置、调用失败或置信门禁未通过时，由调用方
保留现有 Qwen 几何 Family，避免外部路由服务破坏已有建模能力。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from backend.agents.step.families.taxonomy import (
    CAPACITOR_CHIP_FAMILY_ID,
    DSUB_CONNECTOR_FAMILY_ID,
    GULLWING_IC_FAMILY_ID,
    PIN_HEADER_FAMILY_ID,
    QUAD_GULLWING_IC_FAMILY_ID,
    QFN_UFQFPN_FAMILY_ID,
    RESISTOR_CHIP_FAMILY_ID,
    resolve_family_selection,
)


JEV_CHOICE_CRITERIA = {
    "resistor": "01 电阻；仅用于明确的电阻物料。",
    "capacitor": "02 电容；仅用于明确的电容或 MLCC 物料。",
    "inductor": "03 电感；用于明确的电感物料。",
    "diode": "04 二极管；用于明确的二极管物料。",
    "transistor": "05 晶体管；用于明确的分立晶体管物料。",
    "connector": (
        "06 连接器；CN 是硬件接口物料规格书，MT 是结构物料规格书。"
    ),
    "ic": (
        "07 集成芯片；包括 ADC、MCU、逻辑芯片、存储器、运放及典型 IC 封装。"
    ),
    "misc": "08 功率器及不能归入前七类的 Misc 物料。",
    "unknown": "当前证据不足，无法可靠选择 CAD 建模模板类别。",
}

MIN_TOP_PROBABILITY = 0.70
MIN_CONFIDENCE = 0.60
MIN_MARGIN = 0.20

_IDENTITY_TEXT_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")
_EXPLICIT_COUNT_RE = re.compile(
    r"\b(\d{1,3})\s*[-–—]?\s*(?:lead|pin|circuit)s?\b", re.IGNORECASE
)
_QUAD_PACKAGE_RE = re.compile(r"\b(?:L?QFP|TQFP)\s*\d*\b|quad\s+flat", re.I)
_QFN_UFQFPN_PACKAGE_RE = re.compile(
    r"\b(?:U?F?QFPN|QFN|UQFNP)(?:\s*[-–—]?\s*\d+)?\b", re.I
)
_GULLWING_PACKAGE_RE = re.compile(
    r"\b(?:SOT\s*[-–—]?\s*\d+|SOIC|TSSOP|SSOP|MSOP|PSOP)\b|small\s+outline",
    re.I,
)
_DSUB_PACKAGE_RE = re.compile(r"\bD\s*[-–—]?\s*SUB\b|FDB\s*0?9", re.I)


def build_jev_state(
    ocr_tokens: list[dict[str, Any]],
    classification: dict[str, Any],
) -> dict[str, Any]:
    """构造 Jev 所需的紧凑结构化状态。

    Args:
        ocr_tokens: Agent State 中的全部 OCR token。
        classification: Qwen 第一阶段视图和几何 Family 分类。

    Returns:
        不含尺寸数值答案、bbox、原图和 Golden 数据的 Jev state。
    """
    reliable_texts: list[str] = []
    pin_counts: list[int] = []
    for token in ocr_tokens:
        if float(token.get("confidence", 0.0)) < 0.80:
            continue
        text = str(token.get("text", "")).strip()
        if not text:
            continue
        pin_counts.extend(int(match.group(1)) for match in _EXPLICIT_COUNT_RE.finditer(text))
        if _IDENTITY_TEXT_RE.search(text) and len(text) <= 120:
            reliable_texts.append(text)
    identity_texts = list(dict.fromkeys(reliable_texts))[:24]
    return {
        "task": "select_one_of_eight_component_categories",
        "drawing_text": identity_texts,
        "package_type": str(classification.get("package_type", "")),
        # 下划线字段仅供本地映射和失败回退，调用 Jev 时会被剔除，避免模型
        # 直接复述 Qwen 已给出的几何 Family。
        "_fallback_geometric_family": str(classification.get("family_id", "")),
        "_fallback_category": str(classification.get("category_id", "")),
        "_fallback_subcategory": classification.get("subcategory_id"),
        "pin_count_candidates": sorted(set(pin_counts)),
        "identified_views": sorted(set(
            str(item) for item in classification.get("identified_views", {}).values()
        )),
        "detected_features": list(classification.get("identified_features", []))[:20],
    }


async def call_jev_choice(
    state: dict[str, Any],
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> dict[str, Any]:
    """调用 TypeSafe System One API 执行一次 Choice 决策。"""
    endpoint = base_url.rstrip("/")
    if not endpoint.endswith("/systemone"):
        endpoint = f"{endpoint}/systemone"
    public_state = {
        name: value for name, value in state.items() if not name.startswith("_")
    }
    payload = {
        "state": public_state,
        "model": model,
        "questions": {
            "component_family": {
                "type": "choice",
                "instructions": (
                    "根据封装结构、引脚布局、器件标识和几何特征，选择最适合"
                    "匹配图片目录的一级物料类别。证据不足时选择 unknown。"
                ),
                "criteria": JEV_CHOICE_CRITERIA,
            }
        },
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        )
        response.raise_for_status()
    body = response.json()
    answer = body.get("answers", {}).get("component_family", {})
    choice = str(answer.get("choice", ""))
    probabilities = {
        str(name): float(value)
        for name, value in dict(answer.get("probabilities", {})).items()
    }
    if choice not in JEV_CHOICE_CRITERIA or set(probabilities) != set(JEV_CHOICE_CRITERIA):
        raise ValueError("Jev 返回了 Choice 合同之外的类别")
    if not probabilities or choice != max(probabilities, key=probabilities.get):
        raise ValueError("Jev Choice 与概率分布不一致")
    confidence = float(answer.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0 or any(
        not 0.0 <= value <= 1.0 for value in probabilities.values()
    ):
        raise ValueError("Jev 返回了无效概率或置信度")
    if abs(sum(probabilities.values()) - 1.0) > 0.02:
        raise ValueError("Jev 概率分布之和不为 1")
    return {
        "provider": "typesafe",
        "model": str(body.get("model", model)),
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities,
        "usage": body.get("usage", {}),
    }


def evaluate_jev_gate(decision: dict[str, Any]) -> dict[str, Any]:
    """按固定阈值判断 Jev 结果能否自动路由。"""
    probabilities = {
        str(name): float(value)
        for name, value in decision.get("probabilities", {}).items()
    }
    choice = str(decision.get("choice", "unknown"))
    ordered = sorted(probabilities.values(), reverse=True)
    top_probability = float(probabilities.get(choice, 0.0))
    second_probability = float(ordered[1]) if len(ordered) > 1 else 0.0
    margin = top_probability - second_probability
    confidence = float(decision.get("confidence", 0.0))
    auto_route = (
        choice != "unknown"
        and top_probability >= MIN_TOP_PROBABILITY
        and confidence >= MIN_CONFIDENCE
        and margin >= MIN_MARGIN
    )
    return {
        "auto_route": auto_route,
        "top_probability": top_probability,
        "second_probability": second_probability,
        "margin": margin,
        "thresholds": {
            "top_probability": MIN_TOP_PROBABILITY,
            "confidence": MIN_CONFIDENCE,
            "margin": MIN_MARGIN,
        },
    }


def resolve_geometric_family(
    category: str,
    jev_state: dict[str, Any],
    available_family_ids: set[str],
) -> str | None:
    """把 Jev 业务类别解析为项目现有的几何 Family。"""
    current = str(jev_state.get("_fallback_geometric_family", ""))
    searchable_text = " ".join([
        str(jev_state.get("package_type", "")),
        *[str(item) for item in jev_state.get("drawing_text", [])],
    ])
    normalized = resolve_family_selection(
        current,
        category_id=str(jev_state.get("_fallback_category", "")),
        subcategory_id=jev_state.get("_fallback_subcategory"),
        identity_texts=[searchable_text],
    )
    current = str(normalized.get("family_id") or current)
    if category == "resistor":
        target = RESISTOR_CHIP_FAMILY_ID if current in {
            "two_terminal_chip", RESISTOR_CHIP_FAMILY_ID,
            CAPACITOR_CHIP_FAMILY_ID,
        } else ""
    elif category == "capacitor":
        target = CAPACITOR_CHIP_FAMILY_ID if current in {
            "two_terminal_chip", RESISTOR_CHIP_FAMILY_ID,
            CAPACITOR_CHIP_FAMILY_ID,
        } else ""
    elif category == "connector":
        if current == PIN_HEADER_FAMILY_ID:
            target = PIN_HEADER_FAMILY_ID
        else:
            target = DSUB_CONNECTOR_FAMILY_ID if (
                current in {"dsub_connector", DSUB_CONNECTOR_FAMILY_ID}
                or _DSUB_PACKAGE_RE.search(searchable_text)
            ) else ""
    elif category == "ic":
        symbols = {
            re.sub(r"[^A-Za-z0-9]", "", str(item)).upper()
            for item in jev_state.get("drawing_text", [])
        }
        qfn_signature = (
            current == QFN_UFQFPN_FAMILY_ID
            or bool(_QFN_UFQFPN_PACKAGE_RE.search(searchable_text))
            or ({"D2", "E2"}.issubset(symbols) and len(
                symbols & {"A3", "B", "L", "L2"}
            ) >= 2)
        )
        if qfn_signature:
            target = QFN_UFQFPN_FAMILY_ID
        elif _QUAD_PACKAGE_RE.search(searchable_text):
            target = QUAD_GULLWING_IC_FAMILY_ID
        elif _GULLWING_PACKAGE_RE.search(searchable_text):
            target = GULLWING_IC_FAMILY_ID
        elif current in {GULLWING_IC_FAMILY_ID, QUAD_GULLWING_IC_FAMILY_ID}:
            target = current
        else:
            target = ""
    else:
        target = ""
    return target if target in available_family_ids else None
