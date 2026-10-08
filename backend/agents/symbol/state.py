"""符号生成 Agent 的状态定义。

唯一必填输入是 ``pdf_path``。**大块中间产物一律落盘，state 里只留路径** ——
MinerU 的 ``content_list`` 与渲染出的页面图动辄几 MB，塞进 state 会把
PostgreSQL 的 checkpoint 撑爆。

约定与其他 Agent 一致：``TypedDict, total=False``，节点只返回自己写出的
增量，列表靠节点里显式拼接。
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class SymbolDrawingState(TypedDict, total=False):
    """datasheet PDF → OrCAD Capture 符号的工作流状态。"""

    task: str
    mode: str
    # ── 唯一必填输入 ────────────────────────────────────────────
    pdf_path: str
    original_filename: str
    device: str
    package: str
    strict_pages: bool
    #: 产物落盘目录；留空则用临时目录（源程序的 OUTPUT_DIR 是用户正式库，绝不自动写）
    output_dir: str

    # ── 人工回答（interrupt/resume 收下，与自动结果分开存）──────
    human_device: dict[str, Any]
    human_package: dict[str, Any]
    human_conflicts: dict[str, Any]
    human_review_diffs: dict[str, Any]
    human_facts: dict[str, Any]
    human_output: dict[str, Any]
    human_history: list[dict[str, Any]]
    needs_review: bool

    # ── 定位与解析 ──────────────────────────────────────────────
    #: ``pdf_locator.LocateResult`` 的 dict 形式（不含大字段）。
    locate: dict[str, Any]
    #: 送 MinerU 的页子集（1 起）。
    slim_page_numbers: list[int]
    #: MinerU 完整结果落盘后的目录；``content_list`` 从这里的 json 读。
    document_dir: str
    document_notes: list[str]
    pin_page: int
    #: 从引脚表解析出的引脚（``Pin`` 的 dict 形式）。
    table_pins: list[dict[str, Any]]
    table_warnings: list[str]
    #: 候选型号与封装；来源是文件名、表单元格与正文线索。
    device_candidates: list[str]
    package_candidates: list[str]
    package_hints: list[dict[str, Any]]

    # ── 视觉通道与合并 ──────────────────────────────────────────
    figure_sides: dict[str, list[str]]
    figure_page: int
    merged_pins: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    review: dict[str, Any]

    # ── 自检 ────────────────────────────────────────────────────
    check_report: dict[str, Any]
    check_rounds: int
    #: 降级说明（选页、缺视觉通道、跳过重复行等）。「降级要有痕迹」。
    warnings: list[str]
    #: 已渲染的引脚图页 ``[{"page": int, "path": str}]``。
    rendered_pages: list[dict[str, Any]]

    # ── 布局与产物 ──────────────────────────────────────────────
    layout: dict[str, Any]
    capture: dict[str, Any]
    artifact_paths: dict[str, str]

    # ── 内部输出设置，不属于必填接口 ─────────────────────────────
    work_dir: str
    result: dict[str, Any]
    status: str
    errors: list[str]
