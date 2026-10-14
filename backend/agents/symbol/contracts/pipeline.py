"""从 datasheet 提取引脚时的纯逻辑辅助（对应 ``app/pipeline.py`` 的可验证部分）。

源文件是密文。这里只重建**能独立验证的纯函数**；真正的编排放在
:mod:`backend.agents.symbol.nodes` —— 那样每个碰外部世界的步骤（联网、渲染、
子进程）都能单独暂停、单独重试、单独进人工。

被重建的两个函数都有可执行规格：

* :func:`guess_device` —— ``tests/regress.py:334-341`` 给了 5 条用例；
* :func:`best_table` —— §11.5 缺陷 A：源程序原来写死取第一个候选页，
  STM32 上因此取到第 23 页（引脚**图**）而不是第 26 页（引脚**表**），
  而 23/24/25 页分别是 BGA100/LQFP100/LQFP64 三张图 —— 认错封装等于白画。
"""

from __future__ import annotations

from pathlib import Path
import re

from backend.agents.symbol.contracts.extract import (
    Extraction,
    extract_pins_from_table,
)
from backend.agents.symbol.contracts.pkgname import _PACKAGE_PREFIXES

#: 一张表至少要有这么多引脚才算"引脚表"（与 ``selfcheck.MIN_PINS`` 同源）。
MIN_TABLE_PINS = 2

#: 纯十六进制任务号（Web 上传时的落盘名），永远不是型号。
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8,}$")

#: 通用词，永远不是型号。
_GENERIC_TOKENS = {
    "datasheet", "data", "sheet", "manual", "spec", "specification",
    "规格书", "手册", "数据", "芯片",
}

#: 型号特征：字母与数字并存。
_HAS_LETTER = re.compile(r"[A-Za-z]")
_HAS_DIGIT = re.compile(r"\d")


def guess_device(filename: str) -> str:
    """从**原始文件名**里取最长的型号 token；认不出返回空串。

    型号来源的优先级是「用户填的 > 文件名 > 表格单元格 > 文件名原文」，本函数
    只管第二档。取最长是经验：``C8350_单片机(MCU-MPU-SOC)_STM32F105VCT6_规格书``
    里 ``C8350``、``WJ94202`` 也像型号，但真正的型号最长。

    纯十六进制任务号（``9b88e7418e27``）与 ``datasheet`` 之类的通用词一律
    认不出，**返回空串而不是抛异常** —— 调用方按假值处理，回落到让用户填。

    Args:
        filename: 原始文件名（可含路径，只看 basename 的主干）。

    Returns:
        大写化的型号，或空串。
    """
    stem = Path(str(filename or "")).stem
    best = ""
    for token in re.split(r"[^A-Za-z0-9]+", stem):
        if not token:
            continue
        if token.lower() in _GENERIC_TOKENS:
            continue
        if _JOB_ID_RE.match(token.lower()):
            continue
        if not (_HAS_LETTER.search(token) and _HAS_DIGIT.search(token)):
            continue
        if len(token) > len(best):
            best = token
    return best.upper()


def best_table(
    content_list: list[dict],
    pages: list[int] | None = None,
    *,
    package: str = "",
    device: str = "",
) -> tuple[int, Extraction] | None:
    """逐页试表，返回匹配封装的第一张引脚表，并合并同编号的相邻续表。

    Args:
        content_list: MinerU 的 ``content_list``；每块的 ``page_idx`` 是**原 PDF 的
            0 起页号**（瘦身路径已在 ``mineru`` 层还原过）。
        pages: 候选页（1 起）。给空则全试。
        package: 目标封装代码/型号，用于挑列。
        device: 目标型号；封装不匹配表头时，用匹配的型号列。

    Returns:
        ``(页码, Extraction)``；一张表都没解析出引脚时返回 ``None``，
        由调用方决定是"退回全档重跑"还是"停下来问人"。
    """
    wanted = {int(page) for page in (pages or [])}
    tables = []
    for block in content_list or []:
        if str(block.get("type") or "").lower() != "table":
            continue
        html = block.get("table_body") or ""
        if not html:
            continue
        page = int(block.get("page_idx", -1)) + 1  # 0 起 → 1 起
        if page < 1:
            continue
        extraction = extract_pins_from_table(html, page=page, device=device, package=package)
        captions = block.get("table_caption") or []
        caption = captions if isinstance(captions, str) else " ".join(str(c) for c in captions)
        context = caption + " " + " ".join(extraction.headers)
        styles = {style for style in _PACKAGE_PREFIXES if re.search(r"\b" + style + r"\b", context, re.I)}
        selected = next((style for style in _PACKAGE_PREFIXES if re.match(r"^" + style + r"(?:\b|\d)", package, re.I)), "")
        if selected and styles and selected not in styles:
            continue
        tables.append((page, extraction, caption))

    for index, (page, extraction, caption) in enumerate(tables):
        if wanted and page not in wanted:
            continue
        if len(extraction.pins) < MIN_TABLE_PINS:
            continue
        # Candidate-page selection locates the start, not the extent, of a table.
        # Only explicit same-table continuations with the same columns are merged.
        identity = re.search(r"\bTable\s+(\d+)\b", caption, re.I)
        last_page = page
        for next_page, continuation, next_caption in tables[index + 1:]:
            if next_page > last_page + 1:
                break
            next_identity = re.search(r"\bTable\s+(\d+)\b", next_caption, re.I)
            same_id = identity and next_identity and identity[1] == next_identity[1]
            if not (same_id and re.search(r"\bcontinu(?:ed|ation)\b", next_caption, re.I)
                    and [h.split("\n")[-1].strip().casefold() for h in extraction.headers]
                    == [h.split("\n")[-1].strip().casefold() for h in continuation.headers]):
                break
            current = {pin.pin_number: pin for pin in extraction.pins}
            for pin in continuation.pins:
                if pin.pin_number not in current:
                    extraction.pins.append(pin)
                    current[pin.pin_number] = pin
                elif (pin.name, pin.type, pin.description) != (
                        current[pin.pin_number].name, current[pin.pin_number].type,
                        current[pin.pin_number].description):
                    # Keep contradictory duplicates for downstream self-check to block.
                    extraction.pins.append(pin)
                    extraction.warnings.append(f"续表引脚 {pin.pin_number} 与前页冲突，请核对")
            extraction.warnings.extend(continuation.warnings)
            last_page = next_page
        return page, extraction
    return None
