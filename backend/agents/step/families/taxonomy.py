"""数模模板目录、层级 ID 与旧 Family 迁移规则。"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Iterable


RESISTOR_CHIP_FAMILY_ID = "resistor/two_terminal_chip"
CAPACITOR_CHIP_FAMILY_ID = "capacitor/two_terminal_chip"
GULLWING_IC_FAMILY_ID = "ic/gullwing_ic"
QUAD_GULLWING_IC_FAMILY_ID = "ic/quad_gullwing_ic"
QFN_UFQFPN_FAMILY_ID = "ic/qfn_ufqfpn"
DSUB_CONNECTOR_FAMILY_ID = "connector/cn/dsub_connector"
PIN_HEADER_FAMILY_ID = "connector/cn/pin_header"
INDUCTOR_FAMILY_ID = "inductor"
DIODE_FAMILY_ID = "diode"
TRANSISTOR_FAMILY_ID = "transistor"
CONNECTOR_MT_FAMILY_ID = "connector/mt"
MISC_FAMILY_ID = "misc"

LEGACY_FAMILY_ALIASES: dict[str, str] = {
    "gullwing_ic": GULLWING_IC_FAMILY_ID,
    "quad_gullwing_ic": QUAD_GULLWING_IC_FAMILY_ID,
    "dsub_connector": DSUB_CONNECTOR_FAMILY_ID,
    "pin_header": PIN_HEADER_FAMILY_ID,
    "ufqfpn": QFN_UFQFPN_FAMILY_ID,
    "ic/ufqfpn": QFN_UFQFPN_FAMILY_ID,
}

TWO_TERMINAL_CANDIDATES: tuple[str, str] = (
    RESISTOR_CHIP_FAMILY_ID,
    CAPACITOR_CHIP_FAMILY_ID,
)

_RESISTOR_RE = re.compile(r"\bresistors?\b|电阻", re.IGNORECASE)
_CAPACITOR_RE = re.compile(r"\bcapacitors?\b|\bmlcc\b|电容", re.IGNORECASE)


# 插入顺序就是图片中的展示顺序。序号只属于元数据，不进入 Python 包名。
_CATEGORY_CATALOG: dict[str, dict[str, Any]] = {
    "resistor": {
        "code": "01",
        "name_zh": "电阻",
        "name_en": "Resistor",
        "package": "resistor",
        "families": [RESISTOR_CHIP_FAMILY_ID],
        "subcategories": {},
    },
    "capacitor": {
        "code": "02",
        "name_zh": "电容",
        "name_en": "Capacitor",
        "package": "capacitor",
        "families": [CAPACITOR_CHIP_FAMILY_ID],
        "subcategories": {},
    },
    "inductor": {
        "code": "03",
        "name_zh": "电感",
        "name_en": "Inductor",
        "package": "inductor",
        "families": [INDUCTOR_FAMILY_ID],
        "subcategories": {},
    },
    "diode": {
        "code": "04",
        "name_zh": "二极管",
        "name_en": "Diodes",
        "package": "diode",
        "families": [DIODE_FAMILY_ID],
        "subcategories": {},
    },
    "transistor": {
        "code": "05",
        "name_zh": "晶体管",
        "name_en": "Transistor",
        "package": "transistor",
        "families": [TRANSISTOR_FAMILY_ID],
        "subcategories": {},
    },
    "connector": {
        "code": "06",
        "name_zh": "连接器",
        "name_en": "Connector",
        "package": "connector",
        "families": [],
        "subcategories": {
            "cn": {
                "code": "01_CN",
                "name_zh": "硬件接口物料规格书",
                "name_en": "CN",
                "package": "connector.cn",
                "families": [DSUB_CONNECTOR_FAMILY_ID, PIN_HEADER_FAMILY_ID],
            },
            "mt": {
                "code": "02_MT",
                "name_zh": "结构物料规格书",
                "name_en": "MT",
                "package": "connector.mt",
                "families": [CONNECTOR_MT_FAMILY_ID],
            },
        },
    },
    "ic": {
        "code": "07",
        "name_zh": "集成芯片",
        "name_en": "IC",
        "package": "ic",
        "families": [
            GULLWING_IC_FAMILY_ID,
            QUAD_GULLWING_IC_FAMILY_ID,
            QFN_UFQFPN_FAMILY_ID,
        ],
        "subcategories": {},
    },
    "misc": {
        "code": "08",
        "name_zh": "功率器",
        "name_en": "Misc",
        "package": "misc",
        "families": [MISC_FAMILY_ID],
        "subcategories": {},
    },
}


def component_category_catalog() -> dict[str, dict[str, Any]]:
    """返回完整八类目录；调用方不能修改内部注册状态。"""
    return deepcopy(_CATEGORY_CATALOG)


def _family_locations() -> dict[str, tuple[str, str | None]]:
    locations: dict[str, tuple[str, str | None]] = {}
    for category_id, category in _CATEGORY_CATALOG.items():
        for family_id in category["families"]:
            locations[family_id] = (category_id, None)
        for subcategory_id, subcategory in category["subcategories"].items():
            for family_id in subcategory["families"]:
                locations[family_id] = (category_id, subcategory_id)
    return locations


FAMILY_LOCATIONS = _family_locations()


def taxonomy_for_family_id(family_id: str) -> tuple[str, str | None]:
    """返回完整层级 Family 所属的一级和可选二级目录。"""
    try:
        return FAMILY_LOCATIONS[family_id]
    except KeyError as exc:
        raise ValueError(f"未注册的层级器件族：{family_id or '<empty>'}") from exc


def classification_catalog(
    family_contracts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """构造供分类模型使用的目录视图，同时标注模板是否已实现。"""
    catalog = component_category_catalog()
    for category in catalog.values():
        category["templates"] = {
            family_id: {
                "implemented": family_id in family_contracts,
                **family_contracts.get(family_id, {}),
            }
            for family_id in category.pop("families")
        }
        for subcategory in category["subcategories"].values():
            subcategory["templates"] = {
                family_id: {
                    "implemented": family_id in family_contracts,
                    **family_contracts.get(family_id, {}),
                }
                for family_id in subcategory.pop("families")
            }
    return catalog


def resolve_family_selection(
    family_id: str,
    *,
    category_id: str = "",
    subcategory_id: str | None = None,
    identity_texts: Iterable[str] = (),
) -> dict[str, Any]:
    """规范化新旧 Family，并集中执行层级一致性和人工跟进规则。"""
    raw_family_id = str(family_id or "").strip()
    normalized_category = str(category_id or "").strip().casefold()
    normalized_subcategory = (
        str(subcategory_id).strip().casefold() if subcategory_id else None
    )
    texts = " \n ".join(str(text) for text in identity_texts)

    if raw_family_id == "two_terminal_chip":
        resistor = normalized_category == "resistor" or bool(_RESISTOR_RE.search(texts))
        capacitor = normalized_category == "capacitor" or bool(_CAPACITOR_RE.search(texts))
        if resistor and not capacitor:
            raw_family_id = RESISTOR_CHIP_FAMILY_ID
        elif capacitor and not resistor:
            raw_family_id = CAPACITOR_CHIP_FAMILY_ID
        else:
            return {
                "status": "needs_human_follow_up",
                "category_id": normalized_category,
                "subcategory_id": normalized_subcategory,
                "family_id": "",
                "candidates": list(TWO_TERMINAL_CANDIDATES),
                "reason": "旧 two_terminal_chip 无法确定属于电阻还是电容。",
                "follow_up_question": "该两端片式器件属于电阻还是电容？",
            }
    else:
        raw_family_id = LEGACY_FAMILY_ALIASES.get(raw_family_id, raw_family_id)

    if raw_family_id:
        location = FAMILY_LOCATIONS.get(raw_family_id)
        if location is None:
            return {
                "status": "stopped_unsupported_template",
                "category_id": normalized_category,
                "subcategory_id": normalized_subcategory,
                "family_id": "",
                "candidates": [],
                "reason": f"未注册的模板：{raw_family_id}",
            }
        expected_category, expected_subcategory = location
        if normalized_category and normalized_category != expected_category:
            return {
                "status": "invalid_hierarchy",
                "category_id": normalized_category,
                "subcategory_id": normalized_subcategory,
                "family_id": "",
                "candidates": [raw_family_id],
                "reason": (
                    f"模板 {raw_family_id} 不属于一级分类 {normalized_category}。"
                ),
            }
        if normalized_subcategory and normalized_subcategory != expected_subcategory:
            return {
                "status": "invalid_hierarchy",
                "category_id": expected_category,
                "subcategory_id": normalized_subcategory,
                "family_id": "",
                "candidates": [raw_family_id],
                "reason": (
                    f"模板 {raw_family_id} 不属于二级分类 {normalized_subcategory}。"
                ),
            }
        return {
            "status": "resolved",
            "category_id": expected_category,
            "subcategory_id": expected_subcategory,
            "family_id": raw_family_id,
            "candidates": [],
            "reason": "",
        }

    if normalized_category not in _CATEGORY_CATALOG:
        return {
            "status": "stopped_unsupported_template",
            "category_id": normalized_category,
            "subcategory_id": normalized_subcategory,
            "family_id": "",
            "candidates": [],
            "reason": f"未注册的一级分类：{normalized_category or '<empty>'}",
        }
    category = _CATEGORY_CATALOG[normalized_category]
    if normalized_category == "connector":
        if normalized_subcategory not in category["subcategories"]:
            return {
                "status": "needs_human_follow_up",
                "category_id": "connector",
                "subcategory_id": None,
                "family_id": "",
                "candidates": ["connector/cn", "connector/mt"],
                "reason": "连接器必须确定 CN 或 MT 二级分类。",
                "follow_up_question": "该连接器资料属于 CN 还是 MT？",
            }
    return {
        "status": "stopped_unsupported_template",
        "category_id": normalized_category,
        "subcategory_id": normalized_subcategory,
        "family_id": "",
        "candidates": [],
        "reason": "分类已识别，但尚无可执行几何模板。",
    }
