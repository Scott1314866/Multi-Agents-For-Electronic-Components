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
) -> tuple[int, Extraction] | None:
    """逐页试表，返回第一张**真正解析得出引脚**的表。

    Args:
        content_list: MinerU 的 ``content_list``；每块的 ``page_idx`` 是**原 PDF 的
            0 起页号**（瘦身路径已在 ``mineru`` 层还原过）。
        pages: 候选页（1 起）。给空则全试。
        package: 目标封装代码/型号，用于挑列。

    Returns:
        ``(页码, Extraction)``；一张表都没解析出引脚时返回 ``None``，
        由调用方决定是"退回全档重跑"还是"停下来问人"。
    """
    wanted = {int(page) for page in (pages or [])}
    for block in content_list or []:
        if str(block.get("type") or "").lower() != "table":
            continue
        html = block.get("table_body") or ""
        if not html:
            continue
        page = int(block.get("page_idx", -1)) + 1  # 0 起 → 1 起
        if page < 1:
            continue
        if wanted and page not in wanted:
            continue
        extraction = extract_pins_from_table(html, page=page, device=package)
        if len(extraction.pins) >= MIN_TABLE_PINS:
            return page, extraction
    return None
