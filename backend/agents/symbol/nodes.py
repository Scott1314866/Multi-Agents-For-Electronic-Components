"""符号生成工作流的主节点与路由。

链路（对应源程序的 ``pipeline.discover`` + ``pipeline.run``，但拆成了可单独
暂停、单独重试的单元）：

    validate_input → locate_pages → parse_document → pick_table → discover_names
      → [ask_device] → [ask_package] → render_pages → vision_extract
      → merge_channels → review_pins → [resolve_conflicts] → [resolve_review_diffs]
      → self_check → [ask_check_questions] → build_layout → [confirm_output]
      → generate_capture → finalize

源的纪律照搬：

* **双通道职责划分** —— 文本通道给编号/名称/类型/描述，视觉通道只给图形结构；
* **降级要有痕迹** —— 选页、选列、跳过的行都写进 ``warnings`` / ``notes``；
* **停止不是失败** —— 证据不足走专用终态，绝不产出占位符号。
"""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from typing import Any

from backend.agents.symbol import config
from backend.agents.symbol.contracts.extract import FigureInfo
from backend.agents.symbol.contracts.layout import build_layout, describe, preview
from backend.agents.symbol.contracts.merge import merge_channels
from backend.agents.symbol.contracts.models import Conflict, Evidence, Pin
from backend.agents.symbol.contracts.pdf_locator import figure_pages, locate, slim_pages
from backend.agents.symbol.contracts.pipeline import best_table, guess_device
from backend.agents.symbol.contracts.selfcheck import check as run_selfcheck
from backend.agents.symbol.tools.capture import generate as capture_generate
from backend.agents.symbol.tools.mineru import MineruOfflineError
from backend.agents.symbol.tools.render import render_page
from backend.agents.symbol.tools.review import review_pins
from backend.agents.symbol.tools.vision import VisionClient
from backend.agents.symbol.workers import capture_node, network_node, render_node
from backend.core.logger import get_logger

logger = get_logger(__name__)

#: 视觉通道最多找几张引脚图（源程序的用量预算，直接决定花多少钱）。
MAX_FIGURE_PAGES = 3

#: 送 MinerU 的页数上限（超了会被整个拒收）。
SLIM_PAGE_LIMIT = 190

#: 自检最多问几轮，避免人工问答死循环。
MAX_CHECK_ROUNDS = 3

_REVIEW_PROMPT = """你在核查一张元器件手册的引脚图。

【页面】第 {page} 页
【器件】{device}

请只看**图形结构**：每个引脚号出现在哪条边上、在那条边上排第几。
不要读密集小字表格里的引脚名与描述 —— 那是另一个通道的职责。

只输出 JSON（不要 Markdown 代码块）：
{{
  "pins": [
    {{"pin_number": "1", "side": "left"}}
  ],
  "notes": "读不清的地方"
}}

side 只能是 left / right / top / bottom / unknown。"""


def _errors(state: dict[str, Any], message: str) -> list[str]:
    return [*state.get("errors", []), message]


def _warnings(state: dict[str, Any], *messages: str) -> list[str]:
    """追加降级说明。**降级要有痕迹** —— 源程序的三条纪律之一。"""
    return [*state.get("warnings", []), *messages]


def _work_dir(state: dict[str, Any]) -> Path:
    return Path(state["work_dir"])


def _dicts_to_pins(items: list[dict] | None) -> list[Pin]:
    """state 里的引脚是 dict（要能进 msgpack checkpoint）。"""
    pins: list[Pin] = []
    for item in items or []:
        payload = dict(item)
        payload["evidence"] = [Evidence(**e) for e in (payload.get("evidence") or [])]
        pins.append(Pin(**payload))
    return pins


def _pins_to_dicts(pins: list[Pin]) -> list[dict]:
    return [asdict(pin) for pin in pins]


def _dicts_to_conflicts(items: list[dict] | None) -> list[Conflict]:
    return [Conflict(**item) for item in (items or [])]


# ── 输入与定位 ────────────────────────────────────────────────────


async def validate_input_node(state: dict[str, Any]) -> dict[str, Any]:
    """校验输入 PDF 存在且带 ``%PDF-`` 签名。"""
    try:
        raw = str(state.get("pdf_path") or "").strip()
        if not raw:
            raise ValueError("缺少 pdf_path：请给出手册 PDF 的路径")
        path = Path(raw)
        if not path.is_file():
            raise ValueError(f"PDF 不存在：{path}")
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError(f"不是 PDF 文件（缺少 %PDF- 签名）：{path}")

        work_dir = Path(state.get("work_dir") or tempfile.mkdtemp(prefix="symbol-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        return {
            "pdf_path": str(path.resolve()),
            "work_dir": str(work_dir),
            "status": "input_valid",
        }
    except Exception as exc:
        logger.warning("symbol.validate_input.failed", error=str(exc))
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def locate_pages_node(state: dict[str, Any]) -> dict[str, Any]:
    """定位引脚段与封装段，并算出送 MinerU 的页子集。

    三级定位（书签 → 目录 → 关键词锚点 ±2 页 → 比例法兜底）实测把深度处理量
    从 57 页降到约 8 页。**不用"前 10%/后 10%"那种比例启发式** ——
    §8.1 记录了它翻车的现场：57 页手册的前 10% 恰好卡在 Pin Configuration 上。
    """
    try:
        pdf = state["pdf_path"]
        strict = bool(state.get("strict_pages"))
        located = locate(pdf)
        plan = slim_pages(located, pdf, limit=SLIM_PAGE_LIMIT, strict=strict)
        return {
            "locate": located.to_dict(),
            "slim_page_numbers": list(plan.pages),
            "document_notes": list(plan.notes),
            "status": "pages_located",
        }
    except Exception as exc:
        logger.error("symbol.locate.failed", exc_info=True)
        return {"errors": _errors(state, f"页面定位失败：{exc}"), "status": "failed"}


# ── 文本通道 ──────────────────────────────────────────────────────


@network_node
def _parse_with_mineru(pdf: str, pages: list[int] | None):
    from backend.agents.symbol.tools.mineru import MineruClient

    client = MineruClient()
    return client.parse(pdf, pages=pages)


async def parse_document_node(state: dict[str, Any]) -> dict[str, Any]:
    """跑 MinerU 文本通道。

    ``content_list`` 落盘，state 里只留目录 —— 它动辄几 MB，进 checkpoint 会爆。
    """
    try:
        result = await _parse_with_mineru(
            state["pdf_path"], state.get("slim_page_numbers") or None
        )
        doc_dir = _work_dir(state) / "mineru"
        doc_dir.mkdir(parents=True, exist_ok=True)
        (doc_dir / "content_list.json").write_text(
            json.dumps(result.content_list, ensure_ascii=False), encoding="utf-8"
        )
        return {
            "document_dir": str(doc_dir),
            "document_notes": [
                *(state.get("document_notes") or []),
                *(getattr(result, "notes", None) or []),
            ],
            "status": "document_parsed",
        }
    except MineruOfflineError as exc:
        logger.warning("symbol.mineru.offline", error=str(exc))
        return {
            "errors": _errors(state, str(exc)),
            "status": "stopped_offline",
        }
    except Exception as exc:
        logger.error("symbol.parse_document.failed", exc_info=True)
        return {"errors": _errors(state, f"文档解析失败：{exc}"), "status": "failed"}


async def pick_table_node(state: dict[str, Any]) -> dict[str, Any]:
    """从 ``content_list`` 里挑出真正的引脚表。

    §11.5 缺陷 A 的修法：**逐页试**，取第一张真能解析出引脚的表 —— 源程序
    原来写死取候选页里的第一页，STM32 上因此拿到引脚**图**页而非引脚**表**页。
    """
    try:
        doc_dir = Path(state["document_dir"])
        content = json.loads((doc_dir / "content_list.json").read_text(encoding="utf-8"))
        located = state.get("locate") or {}
        targets = (located.get("targets") or {}).get("pins") or []

        found = best_table(content, targets, package=str(state.get("package") or ""))
        if found is None and targets:
            # 候选页都没表 —— 退回全档（源程序的 _retry_full_document）。
            found = best_table(content, None, package=str(state.get("package") or ""))
            if found is not None:
                logger.info("symbol.table.fallback_full_document")
        if found is None:
            return {
                "status": "stopped_no_pin_table",
                "errors": _errors(state, "在候选页里没有解析出引脚表"),
                "result": {
                    "status": "stopped_no_pin_table",
                    "reason": "没有找到可解析的引脚表；可尝试人工指定引脚页后重跑",
                },
            }

        page, extraction = found
        return {
            "pin_page": page,
            "table_pins": _pins_to_dicts(extraction.pins),
            "table_warnings": list(extraction.warnings),
            "status": "table_picked",
        }
    except Exception as exc:
        logger.error("symbol.pick_table.failed", exc_info=True)
        return {"errors": _errors(state, f"引脚表选取失败：{exc}"), "status": "failed"}


async def discover_names_node(state: dict[str, Any]) -> dict[str, Any]:
    """汇总型号与封装的候选。

    型号来源优先级：**用户填的 > 文件名 > 表格单元格 > 文件名原文**。
    封装候选来自正文线索（``pkgname.hints_from_text``）。
    """
    try:
        from backend.agents.symbol.contracts.pkgname import hints_from_text

        doc_dir = Path(state["document_dir"])
        content = json.loads((doc_dir / "content_list.json").read_text(encoding="utf-8"))

        hints = hints_from_text(content)
        packages = [hint.label for hint in hints if hint.label]

        devices: list[str] = []
        from_name = guess_device(Path(state["pdf_path"]).name)
        if from_name:
            devices.append(from_name)
        for hint in hints:
            if hint.label and hint.label not in packages:
                packages.append(hint.label)

        return {
            "device_candidates": devices,
            "package_candidates": packages,
            "package_hints": [
                {
                    "style": hint.style,
                    "pin_count": hint.pin_count,
                    "page": hint.page,
                    "evidence": hint.evidence,
                    "source": hint.source,
                }
                for hint in hints
            ],
            "status": "names_discovered",
        }
    except Exception as exc:
        logger.error("symbol.discover_names.failed", exc_info=True)
        return {"errors": _errors(state, f"型号/封装候选发现失败：{exc}"), "status": "failed"}


# ── 视觉通道 ──────────────────────────────────────────────────────


@render_node
def _render_page(pdf: str, page: int, out_dir: str):
    return render_page(pdf, page, out_dir=Path(out_dir))


@network_node
def _ask_vision(images: list[str], prompt: str) -> dict:
    return VisionClient().ask_json(images, prompt)


async def render_pages_node(state: dict[str, Any]) -> dict[str, Any]:
    """渲染引脚图所在的页（最多 ``MAX_FIGURE_PAGES`` 张）。

    §8.2 ①「喂整页，不喂裁图」—— 图注（``Top View`` / ``RUG Package``）与表头
    是区分依据，裁图极易张冠李戴。
    """
    try:
        pdf = state["pdf_path"]
        located = state.get("locate") or {}
        package = str(state.get("package") or "")
        candidates = list((located.get("targets") or {}).get("pins") or [])
        try:
            ranked = figure_pages(pdf, candidates, package) or candidates
        except Exception:
            ranked = candidates
        wanted = ranked[:MAX_FIGURE_PAGES] or [state.get("pin_page") or 1]

        out_dir = _work_dir(state) / "pages"
        rendered = []
        for page in wanted:
            if not page:
                continue
            shot = await _render_page(pdf, int(page), str(out_dir))
            rendered.append({"page": int(page), "path": str(shot.path)})
        if not rendered:
            return {
                "rendered_pages": [],
                "warnings": _warnings(
                    state, "没有渲染出任何引脚图页：引脚侧别将全部未定，自检会报错"
                ),
                "status": "pages_rendered",
            }
        return {"rendered_pages": rendered, "status": "pages_rendered"}
    except Exception as exc:
        logger.error("symbol.render.failed", exc_info=True)
        return {"errors": _errors(state, f"页面渲染失败：{exc}"), "status": "failed"}


async def vision_extract_node(state: dict[str, Any]) -> dict[str, Any]:
    """视觉通道：只读图形结构（哪个脚在哪条边上）。"""
    try:
        device = str(state.get("device") or "")
        pages = list(state.get("rendered_pages") or [])
        if not pages:
            # 视觉通道缺失不中断：文本通道的引脚仍然可用，但侧别全部未定，
            # 由自检的 sides_unknown / all_pins_one_side 报出来给人定夺。
            return {
                "figure_sides": {},
                "figure_page": 0,
                "warnings": _warnings(state, "视觉通道缺失：引脚侧别全部未定"),
                "status": "figure_extracted",
            }

        payload = await _ask_vision(
            [item["path"] for item in pages],
            _REVIEW_PROMPT.format(page=pages[0]["page"], device=device or "（未指定）"),
        )
        figure = FigureInfo.from_vision(payload, page=int(pages[0]["page"]))
        return {
            "figure_sides": {side: list(nums) for side, nums in figure.pin_sides.items()},
            "figure_page": figure.page,
            "status": "figure_extracted",
        }
    except Exception as exc:
        logger.error("symbol.vision.failed", exc_info=True)
        return {"errors": _errors(state, f"视觉通道失败：{exc}"), "status": "failed"}


# ── 合并与复核 ────────────────────────────────────────────────────


async def merge_channels_node(state: dict[str, Any]) -> dict[str, Any]:
    """以引脚号为主键 join 两个通道。

    两边都有 → 采纳，``side`` 取视觉通道；只有一边有 → 记冲突，**不猜**。
    """
    try:
        text_pins = _dicts_to_pins(state.get("table_pins"))
        sides = state.get("figure_sides") or {}
        figure = FigureInfo(
            pin_sides={side: list(nums) for side, nums in sides.items()},
            page=int(state.get("figure_page") or 0),
        )
        merged = merge_channels(text_pins, figure, page=int(state.get("pin_page") or 0))
        return {
            "merged_pins": _pins_to_dicts(merged.pins),
            "conflicts": [asdict(item) for item in merged.conflicts],
            "status": "channels_merged",
        }
    except Exception as exc:
        logger.error("symbol.merge.failed", exc_info=True)
        return {"errors": _errors(state, f"双通道合并失败：{exc}"), "status": "failed"}


@network_node
def _review(image_path: str, device: str, pins: list[Pin], page: int):
    return review_pins(VisionClient(), image_path, device=device, pins=pins, page=page)


async def review_pins_node(state: dict[str, Any]) -> dict[str, Any]:
    """④ 独立复核：**不带前序上下文**的第二次调用。

    源程序 §8.2 ④：用一次没有前序上下文的独立模型调用重读比对，避免自我确认
    偏差。这一步的差异只列出来，**一律不自动改**（§11.6 第 2 条）。
    """
    try:
        pages = list(state.get("rendered_pages") or [])
        if not pages:
            return {"status": "review_skipped", "review": {"mismatches": [], "passed": True}}
        pins = _dicts_to_pins(state.get("merged_pins"))
        result = await _review(
            pages[0]["path"],
            str(state.get("device") or ""),
            pins,
            int(state.get("pin_page") or 0),
        )
        return {
            "review": {
                "passed": bool(result.passed),
                "mismatches": [asdict(item) for item in result.mismatches],
                "missing": list(result.missing),
                "extra": list(result.extra),
                "notes": result.notes,
            },
            "status": "reviewed",
        }
    except Exception as exc:
        # 复核是"第二意见"，拿不到不构成任务失败 —— 但必须留痕，
        # 让人知道这次的结果**没有**经过独立复核。
        logger.warning("symbol.review.skipped", error=str(exc))
        return {
            "review": {
                "passed": True,
                "skipped": True,
                "reason": str(exc),
                "mismatches": [],
                "missing": [],
                "extra": [],
                "notes": "",
            },
            "warnings": _warnings(state, f"独立复核未能完成：{exc}"),
            "status": "reviewed",
        }


# ── 自检（源程序从未在 CLI 里调用它，迁移后成了必经节点）──────────


async def self_check_node(state: dict[str, Any]) -> dict[str, Any]:
    """纯函数自检：8 组检查、finding code、score。

    **这一步在源程序的 CLI 路径里被漏掉了** —— 结果就是"100 个引脚全堆左边"
    的废符号也能一路打印 ``CAPTURE RESULT: PASS``（``selfcheck.py`` 开头记录的
    正是这个事故）。迁移后它必须是必经节点。

    注意：``pins`` 传的是**布局之前**的那份 —— ``build_layout`` 会把
    ``side="unknown"`` 原地改写成 ``left``，传布局后的进去，"全部侧别未定"
    这条 error 就永远不触发了。
    """
    try:
        from backend.agents.symbol.contracts.pkgname import PackageHint

        pins = _dicts_to_pins(state.get("merged_pins"))
        conflicts = _dicts_to_conflicts(state.get("conflicts"))
        # state 里存的是 dict，自检要的是对象 —— 这里还原。
        package_hints = [
            PackageHint(**item) for item in (state.get("package_hints") or [])
        ]

        report = run_selfcheck(
            pins,
            package=str(state.get("package") or ""),
            device=str(state.get("device") or ""),
            layout=None,  # 布局前的自检；布局后的复检在 capture 之后
            review=None,
            conflicts=conflicts,
            table_warnings=list(state.get("table_warnings") or []),
            capture_passed=None,
            files=None,
            package_names=list(state.get("package_candidates") or []),
            package_hints=package_hints,
            sides_evaluated=bool(state.get("figure_sides")),
            pdf_stem=Path(state.get("pdf_path") or "").stem,
        )
        payload = report.to_dict()
        # 人工已回答过的问题不再重复问。
        answered = set((state.get("check_report") or {}).get("answered", {}))
        if answered:
            payload["questions"] = [
                item for item in (payload.get("questions") or [])
                if item.get("key") not in answered
            ]
        logger.info(
            "symbol.self_check",
            verdict=payload.get("verdict"),
            score=payload.get("score"),
            findings=len(payload.get("findings") or []),
        )
        return {
            "check_report": payload,
            "status": "checked" if payload.get("ok") else "check_failed",
        }
    except Exception as exc:
        logger.error("symbol.self_check.failed", exc_info=True)
        return {"errors": _errors(state, f"自检失败：{exc}"), "status": "failed"}


# ── 布局与产物 ────────────────────────────────────────────────────


async def build_layout_node(state: dict[str, Any]) -> dict[str, Any]:
    """几何计算。

    喂的是**副本**：``build_layout`` 会把 ``side="unknown"`` 原地改写成
    ``left``（源程序为此三处注释反复强调"喂副本进去"）。
    """
    try:
        pins = _dicts_to_pins(state.get("merged_pins"))
        copies = [Pin(**{**asdict(pin)}) for pin in pins]  # 深拷一层，护住原始侧别
        layout = build_layout(copies, part_name=str(state.get("device") or ""))
        return {
            "layout": {
                "part_name": layout.part_name,
                "body_width": layout.body_width,
                "body_height": layout.body_height,
                "half_height": layout.half_height,
                "warnings": list(layout.warnings),
                "summary": describe(layout),
                "preview": preview(layout),
                "pins": [asdict(pin) for pin in layout.pins],
            },
            "status": "layout_built",
        }
    except Exception as exc:
        logger.error("symbol.build_layout.failed", exc_info=True)
        return {"errors": _errors(state, f"几何计算失败：{exc}"), "status": "failed"}


@capture_node
def _run_capture(layout_payload: dict, out_dir: str):
    from backend.agents.symbol.contracts.layout import PlacedPin, SymbolLayout

    layout = SymbolLayout(
        part_name=layout_payload["part_name"],
        pins=[PlacedPin(**item) for item in layout_payload.get("pins") or []],
        body_width=layout_payload["body_width"],
        body_height=layout_payload["body_height"],
        half_height=layout_payload["half_height"],
        warnings=list(layout_payload.get("warnings") or []),
    )
    return capture_generate(layout, out_dir=Path(out_dir))


async def generate_capture_node(state: dict[str, Any]) -> dict[str, Any]:
    """渲染 TCL 并执行，产出 ``.OLB`` 

    找不到 ``tclsh.exe`` 是**环境缺失**（``stopped_no_toolchain``），不是失败：
    ``<part>_gen.tcl`` 已经留在工作目录，换台有 Cadence 的机器就能跑。
    """
    try:
        layout_payload = state.get("layout") or {}
        if not layout_payload:
            raise ValueError("还没有算好的布局")
        out_dir = Path(state.get("output_dir") or (_work_dir(state) / "artifacts"))
        result = await _run_capture(layout_payload, str(out_dir))
        return {
            "capture": {
                "passed": result.passed,
                "olb": str(result.olb),
                "dsn": str(result.dsn),
                "script": str(result.script),
                "log_tail": result.log[-2000:],
            },
            "status": "capture_ok" if result.passed else "capture_failed",
        }
    except FileNotFoundError as exc:
        logger.warning("symbol.toolchain_missing", error=str(exc))
        return {"errors": _errors(state, str(exc)), "status": "stopped_no_toolchain"}
    except Exception as exc:
        logger.error("symbol.capture.failed", exc_info=True)
        return {"errors": _errors(state, f"Capture 生成失败：{exc}"), "status": "failed"}


async def finalize_node(state: dict[str, Any]) -> dict[str, Any]:
    """收尾：汇总产物与判据结论。"""
    layout = state.get("layout") or {}
    capture = state.get("capture") or {}
    report = state.get("check_report") or {}
    artifacts = {
        "tcl": capture.get("script", ""),
        "olb": capture.get("olb", ""),
        "dsn": capture.get("dsn", ""),
    }
    artifacts = {key: value for key, value in artifacts.items() if value}
    return {
        "artifact_paths": artifacts,
        "status": "completed",
        "result": {
            "status": "completed",
            "device": state.get("device", ""),
            "package": state.get("package", ""),
            "pin_page": state.get("pin_page"),
            "summary": layout.get("summary", ""),
            "verdict": report.get("verdict"),
            "score": report.get("score"),
            "confidence": report.get("confidence"),
            "artifacts": artifacts,
            "warnings": list(state.get("table_warnings") or []),
        },
    }


# ── 终点 ──────────────────────────────────────────────────────────


def _stop_result(state: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {
        "needs_review": True,
        "status": status,
        "result": {
            "status": status,
            "device": state.get("device", ""),
            "reason": reason,
            "check_report": state.get("check_report", {}),
            "warnings": list(state.get("table_warnings") or []),
        },
    }


async def stopped_no_pin_table_node(state: dict[str, Any]) -> dict[str, Any]:
    """找不到引脚表：**不产出占位符号**。"""
    return _stop_result(state, "stopped_no_pin_table", "没有找到可解析的引脚表")


async def stopped_no_figure_node(state: dict[str, Any]) -> dict[str, Any]:
    """找不到引脚图：侧别未知，但文本通道的引脚仍可用，交人决定。"""
    return _stop_result(state, "stopped_no_figure", "没有找到可渲染的引脚图页")


async def stopped_offline_node(state: dict[str, Any]) -> dict[str, Any]:
    """MinerU 离线（缓存未命中且不允许上传）。"""
    return _stop_result(state, "stopped_offline", "MinerU 离线且本地无缓存，无法解析文档")


async def stopped_no_toolchain_node(state: dict[str, Any]) -> dict[str, Any]:
    """没有 tclsh：TCL 已生成，等有 Cadence 的机器。"""
    payload = _stop_result(state, "stopped_no_toolchain", "本机没有 tclsh.exe")
    payload["result"]["generated_files"] = {"tcl": (state.get("capture") or {}).get("script", "")}
    return payload


async def failed_node(state: dict[str, Any]) -> dict[str, Any]:
    """技术失败的收敛点。"""
    logger.error("symbol.failed", errors=state.get("errors", []))
    return {
        "needs_review": False,
        "status": "failed",
        "result": {
            "status": "failed",
            "device": state.get("device", ""),
            "errors": list(state.get("errors", [])),
        },
    }


# ── 路由 ──────────────────────────────────────────────────────────


def _status(state: dict[str, Any]) -> str:
    return str(state.get("status") or "")


def route_continue_or_failed(state: dict[str, Any]) -> str:
    """普通节点：技术失败进 failed，其余按各自的中断/停止点名分流。"""
    status = _status(state)
    if status == "failed":
        return "failed"
    if status.startswith("stopped"):
        return status
    return "continue"


def route_after_human_input(state: dict[str, Any]) -> str:
    return "cancelled" if _status(state) == "cancelled" else "continue"


def route_after_self_check(state: dict[str, Any]) -> str:
    """自检之后：有未答问题就问，没有就往下走。"""
    if _status(state) == "failed":
        return "failed"
    report = state.get("check_report") or {}
    rounds = int(state.get("check_rounds") or 0)
    if (report.get("questions") or []) and rounds < MAX_CHECK_ROUNDS:
        return "ask"
    return "continue"


def route_after_check_answer(state: dict[str, Any]) -> str:
    if _status(state) == "cancelled":
        return "cancelled"
    # 回答过就回到自检重跑，让 apply_answer 的效果体现出来。
    return "recheck"


def route_after_conflicts(state: dict[str, Any]) -> str:
    if _status(state) == "cancelled":
        return "cancelled"
    return "failed" if _status(state) == "failed" else "continue"


def route_after_layout(state: dict[str, Any]) -> str:
    if _status(state) == "failed":
        return "failed"
    return "continue"


def route_after_capture(state: dict[str, Any]) -> str:
    status = _status(state)
    if status == "stopped_no_toolchain":
        return "stopped"
    if status in {"failed", "capture_failed"}:
        return "failed"
    return "continue"
