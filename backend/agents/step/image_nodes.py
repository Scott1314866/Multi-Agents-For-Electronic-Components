"""单张工程图图片到 STEP 的 LangGraph 编排节点。

本模块只组织 vision、family、IR 和 CAD 服务；OCR、OpenCV 和证据融合算法
分别位于 ``vision/``，避免把图像实现堆入节点文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any, TypeVar

from pydantic import BaseModel

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agents.step.families.registry import (
    create_evidence_feature_ir,
    derive_family_parameters,
    image_family_catalog,
    reconcile_image_family_classification,
    template_category_catalog,
)
from backend.agents.step.families.ic import gullwing_ic, qfn_ufqfpn
from backend.agents.step.families.taxonomy import (
    resolve_family_selection,
    taxonomy_for_family_id,
)
from backend.agents.step.ir.executor import build_feature_model
from backend.agents.step.jev_router import (
    build_jev_state,
    call_jev_choice,
    evaluate_jev_gate,
    resolve_geometric_family,
)
from backend.agents.step.cad_utils import (
    load_cadquery,
    prepare_reference_step,
    read_step_metrics,
    render_shape_to_png,
    safe_file_stem,
)
from backend.agents.step.reference_search import (
    extract_manufacturer_names,
    extract_product_identifiers,
    search_public_step_candidates,
)
from backend.agents.step.prompts import (
    IMAGE_VIEW_CLASSIFICATION_SYSTEM_PROMPT,
    IMAGE_VIEW_CLASSIFICATION_USER_PROMPT,
    IMAGE_VIEW_SEMANTIC_SYSTEM_PROMPT,
    IMAGE_VIEW_SEMANTIC_USER_PROMPT,
)
from backend.agents.step.vision import (
    DimensionGroup,
    FusedEvidence,
    QwenSemanticResult,
    QwenViewClassificationResult,
    QwenViewSemanticResult,
    VisualEvidenceBundle,
    assign_evidence_to_views,
    build_table_dimension_groups,
    build_dimension_groups,
    build_region_summaries,
    detect_geometry,
    detect_dimension_table_regions,
    ensure_region_coverage,
    extract_ocr_tokens,
    infer_document_unit_context,
    fuse_evidence,
    merge_view_semantics,
    normalize_line_segments,
    image_as_data_url,
    parse_dimension_expression,
    parse_explicit_count_expression,
    parse_table_nominal_expression,
    preprocess_image,
    read_image,
    retrieve_view_evidence,
    retrieve_dimension_group_evidence,
    render_region_crop,
    render_region_overview,
    split_view_regions,
    validate_fused_dimensions,
)
from backend.agents.step.vision.line_detector import link_tokens_to_lines
from backend.agents.step.vision.semantic_review import find_semantic_collisions
from backend.agents.step.vision.schemas import (
    ArrowEvidence,
    GeometryLine,
    OCRToken,
    TokenLineLink,
    ViewRegion,
)
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger
from backend.config import get_settings
from backend.agents.step.workers import cad_node, vision_node


logger = get_logger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_REFERENCE_STEPS = {
    "connector/cn/dsub_connector": (
        PROJECT_ROOT
        / "output"
        / "drawing_to_step"
        / "golden_set"
        / "dsub9_right_angle_female"
        / "FDB0902-F0DB300K6KA-REF.step"
    ),
}

GOLDEN_EXPECTED_FEATURE_TYPES = {
    "connector/cn/dsub_connector": {
        "dsub_shell_frame",
        "dsub_rear_housing",
        "dsub_right_angle_contacts",
        "dsub_mounting_hardware",
    },
    "ic/gullwing_ic": {"drafted_body_loft", "gullwing_lead_array"},
    "ic/quad_gullwing_ic": {"molded_body_box", "quad_gullwing_lead_array"},
    "ic/qfn_ufqfpn": {"molded_body_box", "quad_no_lead_terminal_array"},
    "resistor/two_terminal_chip": {"chip_body_box", "end_cap_pair"},
    "capacitor/two_terminal_chip": {"chip_body_box", "end_cap_pair"},
}


def _errors(state: dict[str, Any], message: str) -> list[str]:
    """追加一条节点错误。"""
    return [*state.get("errors", []), message]


def _write_json(path: Path, payload: Any) -> str:
    """保存 UTF-8 JSON 证据文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


def _response_text(response: Any) -> str:
    """兼容字符串和 LangChain 消息响应。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


def _parse_model_json(text: str, schema: type[_SchemaT]) -> _SchemaT:
    """从模型响应中读取 JSON，并按指定 Pydantic schema 严格校验。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Qwen 未返回 JSON object")
    payload = json.loads(cleaned[start:end + 1])
    forbidden_numeric_keys = {
        "value", "nominal_value", "minimum_value", "maximum_value", "unit", "dimension"
    }
    normalized_ambiguities = []
    for ambiguity in payload.get("ambiguities", []):
        if isinstance(ambiguity, str):
            normalized_ambiguities.append(ambiguity)
        elif isinstance(ambiguity, dict) and not (
            set(ambiguity) & forbidden_numeric_keys
        ):
            normalized_ambiguities.append(
                json.dumps(ambiguity, ensure_ascii=False, sort_keys=True)
            )
        else:
            normalized_ambiguities.append(ambiguity)
    payload["ambiguities"] = normalized_ambiguities
    if schema is QwenViewClassificationResult:
        identified_views = payload.get("identified_views", {})
        if isinstance(identified_views, dict):
            payload["identified_views"] = {
                region_id: (
                    "composite_outline"
                    if isinstance(view_type, list) and len(view_type) > 1
                    else str(view_type[0])
                    if isinstance(view_type, list) and view_type
                    else str(view_type)
                )
                for region_id, view_type in identified_views.items()
            }
    if schema is QwenViewSemanticResult and "confidence" not in payload:
        # 视图级总置信度不是尺寸值。模型偶尔会完整返回各 assignment 的
        # 置信度却遗漏汇总字段，此时采用其中最低值作为保守聚合；没有任何
        # assignment 时使用 0，避免把不完整响应伪装成高置信度结果。
        assignment_confidences = [
            float(item["confidence"])
            for item in payload.get("assignments", [])
            if isinstance(item, dict) and "confidence" in item
        ]
        payload["confidence"] = (
            min(assignment_confidences) if assignment_confidences else 0.0
        )
    return schema.model_validate(payload)


def _default_output_dir(image_path: Path) -> Path:
    """根据输入文件名生成确定性输出目录。"""
    return PROJECT_ROOT / "output" / "drawing_to_step" / "image_agent" / safe_file_stem(image_path.stem)


@vision_node
def load_image_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：校验唯一图片输入并记录哈希，不读取 Golden Reference。

    Args:
        state: 必须包含 ``image_path``；``output_dir`` 为内部可选设置。

    Returns:
        ``image_meta``、``output_dir`` 和 ``image_loaded`` 状态。

    失败状态:
        文件不存在或不可解码时返回 ``failed``。
    """
    try:
        image_path = Path(str(state["image_path"])).resolve()
        image = read_image(image_path)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        output_dir = Path(state.get("output_dir") or _default_output_dir(image_path)).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        return {
            "image_path": str(image_path),
            "output_dir": str(output_dir),
            "image_meta": {
                "sha256": digest,
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "channels": int(image.shape[2]),
            },
            "status": "image_loaded",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


@vision_node
def preprocess_image_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：执行灰度化、两倍放大、纠偏、二值化和连通区域清理。

    Args:
        state: 包含 ``image_path``、``output_dir``。

    Returns:
        ``preprocessing`` 路径和统计信息。

    失败状态:
        OpenCV 处理失败时返回 ``failed``。
    """
    try:
        image = read_image(Path(state["image_path"]))
        preprocessing = preprocess_image(image, Path(state["output_dir"]) / "vision")
        return {"preprocessing": preprocessing, "status": "image_preprocessed"}
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


@vision_node
def extract_all_evidence_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：提取不带视图语义的全量 OCR、原始线段和箭头证据。

    Args:
        state: 包含预处理图片路径和图片哈希。

    Returns:
        ``all_ocr_tokens``、``raw_line_segments``、归一化后的
        ``all_line_segments``、``all_arrows`` 和邻接关系。

    失败状态:
        OCR、OpenCV 或证据归一化失败时返回 ``failed``。
    """
    try:
        output_dir = Path(state["output_dir"])
        cache_path = output_dir / "all_evidence.json"
        payload = None
        cache_source: Path | None = None
        if cache_path.is_file():
            candidate = json.loads(cache_path.read_text(encoding="utf-8"))
            if (
                candidate.get("schema_version") == "3.0-view-retrieval"
                and candidate.get("image_sha256") == state["image_meta"]["sha256"]
            ):
                payload = candidate
                cache_source = cache_path
        if payload is None:
            legacy_path = output_dir / "visual_evidence.json"
            if legacy_path.is_file():
                legacy = VisualEvidenceBundle.model_validate_json(
                    legacy_path.read_text(encoding="utf-8")
                )
                if legacy.image_sha256 == state["image_meta"]["sha256"]:
                    cache_source = legacy_path
                    tokens = [
                        token.model_copy(update={"source_region_id": "unassigned"})
                        for token in legacy.ocr_tokens
                    ]
                    raw_lines = [
                        line.model_copy(update={"source_region_id": "unassigned"})
                        for line in legacy.lines
                    ]
                    arrows = legacy.arrows
                else:
                    tokens, raw_lines, arrows = [], [], []
            else:
                tokens, raw_lines, arrows = [], [], []
            if not tokens:
                tokens = extract_ocr_tokens(
                    Path(state["preprocessing"]["gray_path"]), []
                )
                raw_lines, arrows, _ = detect_geometry(
                    Path(state["preprocessing"]["binary_path"]), tokens, []
                )
            normalized_lines = normalize_line_segments(raw_lines)
            links = link_tokens_to_lines(tokens, normalized_lines)
            payload = {
                "schema_version": "3.0-view-retrieval",
                "image_sha256": state["image_meta"]["sha256"],
                "all_ocr_tokens": [item.model_dump() for item in tokens],
                "raw_line_segments": [item.model_dump() for item in raw_lines],
                "all_line_segments": [item.model_dump() for item in normalized_lines],
                "all_arrows": [item.model_dump() for item in arrows],
                "token_line_links": [item.model_dump() for item in links],
            }
        if cache_source is not None:
            logger.ocr_scan_result(
                scan_round="cache",
                source=str(cache_source),
                rotation_deg=None,
                items=[
                    {
                        "token_id": item.get("token_id", ""),
                        "text": item.get("text", ""),
                        "confidence": item.get("confidence", 0.0),
                        "bbox": item.get("bbox"),
                        "rotation_deg": item.get("rotation_deg", 0),
                        "accepted": True,
                    }
                    for item in payload["all_ocr_tokens"]
                ],
                event="ocr.cache_loaded",
            )
        return {
            "all_ocr_tokens": payload["all_ocr_tokens"],
            "raw_line_segments": payload["raw_line_segments"],
            "all_line_segments": payload["all_line_segments"],
            "all_arrows": payload["all_arrows"],
            "token_line_links": payload["token_line_links"],
            "status": "all_evidence_extracted",
        }
    except Exception as exc:
        logger.error("step.image.extract_all.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def store_evidence_locally_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：把完整证据保存到本地，State 继续保留可检索副本。

    Args:
        state: 包含全量 OCR、原始/归一化线段、箭头和图片哈希。

    Returns:
        证据清单路径和 ``evidence_index``。

    失败状态:
        证据写盘失败时返回 ``failed``。
    """
    try:
        output_dir = Path(state["output_dir"])
        payload = {
            "schema_version": "3.0-view-retrieval",
            "image_sha256": state["image_meta"]["sha256"],
            "all_ocr_tokens": state["all_ocr_tokens"],
            "raw_line_segments": state["raw_line_segments"],
            "all_line_segments": state["all_line_segments"],
            "all_arrows": state["all_arrows"],
            "token_line_links": state["token_line_links"],
        }
        path = output_dir / "all_evidence.json"
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["all_evidence"] = _write_json(path, payload)
        index = {
            "tokens": {item["token_id"]: item for item in state["all_ocr_tokens"]},
            "lines": {item["line_id"]: item for item in state["all_line_segments"]},
            "arrows": {item["arrow_id"]: item for item in state["all_arrows"]},
        }
        return {
            "evidence_index": index,
            "artifact_paths": artifacts,
            "status": "evidence_stored",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def detect_views_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：本地切分候选区域，并让 Qwen 仅根据区域摘要识别视图类型。

    Args:
        state: 包含完整证据和预处理二值图。

    Returns:
        带区域归属的 ``visual_evidence``、``view_classification`` 和待处理视图队列。

    失败状态:
        视图切分或第一阶段 Qwen 输出无效时返回 ``failed``。
    """
    try:
        regions = split_view_regions(Path(state["preprocessing"]["binary_path"]))
        tokens = infer_document_unit_context([
            OCRToken.model_validate(item) for item in state["all_ocr_tokens"]
        ])
        regions = ensure_region_coverage(
            regions,
            tokens,
            (
                int(state["preprocessing"]["width"]),
                int(state["preprocessing"]["height"]),
            ),
        )
        lines = [GeometryLine.model_validate(item) for item in state["all_line_segments"]]
        tokens, lines = assign_evidence_to_views(tokens, lines, regions)
        links = link_tokens_to_lines(tokens, lines)
        evidence = VisualEvidenceBundle(
            image_sha256=state["image_meta"]["sha256"],
            image_size=(
                int(state["preprocessing"]["width"]),
                int(state["preprocessing"]["height"]),
            ),
            regions=regions,
            ocr_tokens=tokens,
            lines=lines,
            arrows=[ArrowEvidence.model_validate(item) for item in state["all_arrows"]],
            token_line_links=links,
        )
        catalog = template_category_catalog()
        output_dir = Path(state["output_dir"])
        overview_path = render_region_overview(
            Path(state["preprocessing"]["binary_path"]),
            regions,
            output_dir / "vision" / "region_overview.png",
        )
        prompt = IMAGE_VIEW_CLASSIFICATION_USER_PROMPT.format(
            family_catalog=json.dumps(catalog, ensure_ascii=False),
            region_summaries=json.dumps(
                build_region_summaries(evidence), ensure_ascii=False
            ),
        )
        if state.get("package_type_hint"):
            prompt += (
                "\n人工提供的封装身份提示（仅用于身份判断，不作为尺寸证据；"
                "与图纸冲突时请在 ambiguities 中说明）：\n"
                + json.dumps(state["package_type_hint"], ensure_ascii=False)
            )
        classification = _parse_model_json(
            await _invoke_qwen(
                IMAGE_VIEW_CLASSIFICATION_SYSTEM_PROMPT,
                prompt,
                image_path=overview_path,
            ),
            QwenViewClassificationResult,
        )
        classification = reconcile_image_family_classification(
            classification, tokens
        )
        valid_ids = {item.region_id for item in regions}
        identified = {
            region_id: view_type
            for region_id, view_type in classification.identified_views.items()
            if region_id in valid_ids
        }
        table_region_ids = detect_dimension_table_regions(tokens, regions)
        for region_id in table_region_ids:
            if _is_excluded_geometry_view(identified.get(region_id, "")):
                continue
            identified[region_id] = "dimension_table"
        if (
            any(region.region_id == "region_document" for region in regions)
            and not identified.get("region_document")
        ):
            document_numeric_count = sum(
                1
                for token in tokens
                if token.source_region_id == "region_document"
                and token.confidence >= 0.80
                and (
                    (parsed := parse_dimension_expression(token.text)).nominal_value
                    is not None
                    or parsed.maximum_value is not None
                )
            )
            if document_numeric_count >= 6:
                identified["region_document"] = "composite_outline"
        pending = [
            region_id
            for region_id, view_type in identified.items()
            if _is_geometry_view(view_type)
        ]
        if not pending:
            # Qwen 偶尔会把线条稀疏或浅色的几何区域全部标成非建模信息。
            # 此处只依据区域内真实数字 token 和几何线密度恢复候选，不判断
            # 任何尺寸语义，也不产生数值。后续仍须由逐视图 Qwen 和门禁确认。
            recovered: list[str] = []
            for region in regions:
                # Never promote an explicitly excluded footprint/electrical
                # region merely because it contains many numbers and lines.
                if _is_excluded_geometry_view(identified.get(region.region_id, "")):
                    continue
                numeric_count = sum(
                    1
                    for token in tokens
                    if token.source_region_id == region.region_id
                    and token.confidence >= 0.80
                    and (
                        (parsed := parse_dimension_expression(token.text)).nominal_value
                        is not None
                        or parsed.maximum_value is not None
                    )
                )
                line_count = sum(
                    1 for line in lines if line.source_region_id == region.region_id
                )
                minimum_numeric = 6 if region.region_id == "region_document" else 2
                if numeric_count >= minimum_numeric and line_count >= 4:
                    identified[region.region_id] = "composite_outline"
                    recovered.append(region.region_id)
            if recovered:
                pending = recovered
                classification = classification.model_copy(update={
                    "ambiguities": [
                        *classification.ambiguities,
                        "Qwen 未返回几何视图：根据区域数字尺寸和几何线密度恢复候选 "
                        + ", ".join(recovered),
                    ]
                })
        classification = classification.model_copy(
            update={"identified_views": identified}
        )
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["view_region_overview"] = str(overview_path)
        artifacts["visual_evidence"] = _write_json(
            output_dir / "visual_evidence.json", evidence.model_dump()
        )
        artifacts["view_classification"] = _write_json(
            output_dir / "view_classification.json", classification.model_dump()
        )
        return {
            "all_regions": [item.model_dump() for item in regions],
            "all_ocr_tokens": [item.model_dump() for item in tokens],
            "all_line_segments": [item.model_dump() for item in lines],
            "token_line_links": [item.model_dump() for item in links],
            "visual_evidence": evidence.model_dump(),
            "view_classification": classification.model_dump(),
            "pending_view_ids": pending,
            "per_view_semantics": [],
            "artifact_paths": artifacts,
            "status": "views_detected",
        }
    except Exception as exc:
        logger.error("step.image.detect_views.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def jev_route_template_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：让 Jev Choice 根据结构化证据选择 CAD 模板业务类别。

    Args:
        state: 包含 OCR token 和 Qwen 第一阶段几何分类；不向 Jev 发送图片、
            bbox、尺寸答案、Feature IR 或 Golden Reference。

    Returns:
        ``jev_decision`` 审计结果，以及门禁通过时更新后的
        ``view_classification.family_id``。

    失败状态:
        API 未配置、调用异常、低置信或类别无法映射时均安全回退 Qwen Family，
        不返回技术失败，也不阻断原有流程。
    """
    classification = dict(state["view_classification"])
    selection = resolve_family_selection(
        str(classification.get("family_id") or ""),
        category_id=str(classification.get("category_id") or ""),
        subcategory_id=classification.get("subcategory_id"),
        identity_texts=[
            str(item.get("text") or "")
            for item in state.get("all_ocr_tokens", [])
            if float(item.get("confidence", 0.0)) >= 0.80
        ],
    )
    if selection["status"] == "resolved":
        classification.update({
            "category_id": selection["category_id"],
            "subcategory_id": selection["subcategory_id"],
            "family_id": selection["family_id"],
        })
    elif selection["status"] == "needs_human_follow_up":
        classification.update({
            "category_id": selection["category_id"],
            "subcategory_id": selection["subcategory_id"],
            "family_id": "",
            "unresolved_fields": list(dict.fromkeys([
                *classification.get("unresolved_fields", []),
                "human_follow_up:family_id",
            ])),
            "ambiguities": list(dict.fromkeys([
                *classification.get("ambiguities", []),
                selection["reason"],
            ])),
        })
    compact_state = build_jev_state(
        state.get("all_ocr_tokens", []), classification
    )
    public_jev_state = {
        name: value
        for name, value in compact_state.items()
        if not name.startswith("_")
    }
    settings = get_settings()
    api_key = str(settings.typesafe_api_key or "").strip()
    report: dict[str, Any] = {
        "status": "fallback",
        "input_state": public_jev_state,
        "original_family_id": classification.get("family_id", ""),
        "resolved_family_id": classification.get("family_id", ""),
        "original_category_id": classification.get("category_id", ""),
        "resolved_category_id": classification.get("category_id", ""),
        "resolved_subcategory_id": classification.get("subcategory_id"),
        "route_source": "qwen_fallback",
    }
    try:
        if state.get("human_route"):
            # 人工已在前置节点确认分类，Jev 不得覆盖该选择。
            report.update({
                "status": "human_confirmed",
                "route_source": "human_confirmation",
                "reason": "human_selection_preserved",
                "human_selection": state["human_route"],
            })
        elif state.get("package_type_hint") and state.get("status") == "routing_ready":
            # 前置节点已检查人工封装与高置信分类一致；无需重复询问，
            # 也不能由后续外部路由重新改变这一封装身份。
            report.update({
                "status": "package_constrained",
                "route_source": "requested_package",
                "reason": "recognized_package_matches_human_request",
                "requested_package": state["package_type_hint"],
            })
        elif not api_key:
            report["reason"] = "typesafe_api_key_not_configured"
        else:
            decision = await call_jev_choice(
                compact_state,
                api_key=api_key,
                base_url=str(settings.typesafe_base_url),
                model=str(settings.typesafe_model),
            )
            gate = evaluate_jev_gate(decision)
            report.update({"decision": decision, "gate": gate})
            target_family = (
                resolve_geometric_family(
                    decision["choice"],
                    compact_state,
                    set(image_family_catalog()),
                )
                if gate["auto_route"]
                else None
            )
            if gate["auto_route"] and target_family:
                contract = image_family_catalog()[target_family]
                category_id, subcategory_id = taxonomy_for_family_id(target_family)
                classification.update({
                    "category_id": category_id,
                    "subcategory_id": subcategory_id,
                    "family_id": target_family,
                    "identified_features": list(contract["required_features"]),
                    "unresolved_fields": list(contract["required_parameters"]),
                    "ambiguities": [
                        *classification.get("ambiguities", []),
                        "Jev Choice 通过路由门禁："
                        f"{decision['choice']} -> {target_family}",
                    ],
                })
                report.update({
                    "status": "auto_routed",
                    "resolved_family_id": target_family,
                    "resolved_category_id": category_id,
                    "resolved_subcategory_id": subcategory_id,
                    "route_source": "jev_choice",
                })
            elif not gate["auto_route"]:
                report["reason"] = "confidence_gate_not_passed"
            else:
                report["reason"] = "category_has_no_supported_geometric_family"
    except Exception as exc:
        report["reason"] = "jev_call_failed"
        report["error"] = str(exc)
        logger.warning("step.image.jev.fallback", error=str(exc))

    output_dir = Path(state["output_dir"])
    artifacts = dict(state.get("artifact_paths", {}))
    artifacts["jev_decision"] = _write_json(
        output_dir / "jev_decision.json", report
    )
    artifacts["view_classification"] = _write_json(
        output_dir / "view_classification.json", classification
    )
    logger.info(
        "step.image.jev.route_completed",
        route_source=report["route_source"],
        original_family=report["original_family_id"],
        resolved_family=report["resolved_family_id"],
        reason=report.get("reason", "auto_routed"),
    )
    return {
        "jev_decision": report,
        "view_classification": classification,
        "artifact_paths": artifacts,
        "status": "template_routed",
    }


@vision_node
def build_dimension_groups_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：将当前全量 token、线段和箭头确定性归并为尺寸组。

    Args:
        state: 包含已分配视图的 OCR token、线段和邻接关系。

    Returns:
        ``dimension_groups`` 及其本地审计文件。

    失败状态:
        Schema 或尺寸组构建异常时返回 ``failed``。
    """
    try:
        geometry_view_ids = {
            region_id
            for region_id, view_type in state["view_classification"]["identified_views"].items()
            if _is_geometry_view(view_type)
        }
        all_tokens = [
            OCRToken.model_validate(item) for item in state["all_ocr_tokens"]
        ]
        tokens = [
            token for token in all_tokens
            if token.source_region_id in geometry_view_ids
        ]
        identity_target_view = (
            "region_document"
            if "region_document" in geometry_view_ids
            else min(geometry_view_ids)
            if geometry_view_ids
            else None
        )
        if identity_target_view is not None:
            retained_ids = {token.token_id for token in tokens}
            tokens.extend(
                token.model_copy(update={"source_region_id": identity_target_view})
                for token in all_tokens
                if token.token_id not in retained_ids
                and parse_explicit_count_expression(token.text) is not None
            )
        lines = [
            GeometryLine.model_validate(item)
            for item in state["all_line_segments"]
            if item.get("source_region_id") in geometry_view_ids
        ]
        table_region_ids = {
            region_id
            for region_id, view_type in state["view_classification"]["identified_views"].items()
            if view_type == "dimension_table"
        }
        ordinary_tokens = [
            token for token in tokens if token.source_region_id not in table_region_ids
        ]
        ordinary_lines = [
            line for line in lines if line.source_region_id not in table_region_ids
        ]
        links = link_tokens_to_lines(ordinary_tokens, ordinary_lines)
        groups, links = build_dimension_groups(
            ordinary_tokens,
            ordinary_lines,
            links,
        )
        table_groups, tokens = build_table_dimension_groups(
            tokens, lines, table_region_ids
        )
        groups.extend(table_groups)
        existing_identity_tokens = {
            token_id
            for group in groups
            if group.evidence_type == "identity_text"
            for token_id in group.token_ids
        }
        for token in tokens:
            if (
                token.source_region_id in table_region_ids
                and token.token_id not in existing_identity_tokens
                and parse_explicit_count_expression(token.text) is not None
            ):
                groups.append(DimensionGroup(
                    dimension_id=f"identity_{len(groups) + 1:04d}",
                    view_id=token.source_region_id,
                    evidence_type="identity_text",
                    bbox=token.bbox,
                    token_ids=[token.token_id],
                    orientation="horizontal",
                    confidence=token.confidence,
                ))
        table_links = link_tokens_to_lines(
            [token for token in tokens if token.source_region_id in table_region_ids],
            [line for line in lines if line.source_region_id in table_region_ids],
        )
        links.extend(table_links)
        payload = [item.model_dump() for item in groups]
        path = Path(state["output_dir"]) / "dimension_groups.json"
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["dimension_groups"] = _write_json(path, payload)
        evidence = VisualEvidenceBundle.model_validate(state["visual_evidence"])
        evidence = evidence.model_copy(update={
            "ocr_tokens": tokens,
            "token_line_links": links,
        })
        artifacts["visual_evidence"] = _write_json(
            Path(state["output_dir"]) / "visual_evidence.json", evidence.model_dump()
        )
        return {
            "dimension_groups": payload,
            "all_ocr_tokens": [item.model_dump() for item in tokens],
            "token_line_links": [item.model_dump() for item in links],
            "visual_evidence": evidence.model_dump(),
            "artifact_paths": artifacts,
            "status": "dimension_groups_built",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def retrieve_view_evidence_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：只检索当前视图所需的一小批 token 和完整尺寸组。

    Args:
        state: 包含完整证据、尺寸组和 ``pending_view_ids``。

    Returns:
        ``current_view_id`` 与 ``current_prompt_evidence``。

    失败状态:
        队列为空或视图引用无效时返回 ``failed``。
    """
    try:
        pending = list(state.get("pending_view_ids", []))
        if not pending:
            raise RuntimeError("没有待处理的工程视图")
        region_id = pending[0]
        view_type = state["view_classification"]["identified_views"].get(region_id, "")
        is_table = view_type == "dimension_table"
        group_limit = 16 if is_table else 24
        payload = retrieve_dimension_group_evidence(
            VisualEvidenceBundle.model_validate(state["visual_evidence"]),
            [DimensionGroup.model_validate(item) for item in state["dimension_groups"]],
            region_id,
            # 尺寸表每行只保留行名、可选符号和 NOM/BSC 值。48 个 token
            # 可覆盖常见 15 行参数表，仍远小于 State 中的完整 OCR 集合。
            max_tokens=48 if is_table else 40,
            max_dimension_groups=group_limit,
        )
        region = next(
            item
            for item in VisualEvidenceBundle.model_validate(
                state["visual_evidence"]
            ).regions
            if item.region_id == region_id
        )
        crop_path = render_region_crop(
            Path(state["preprocessing"]["binary_path"]),
            region,
            Path(state["output_dir"]) / "vision" / f"{region_id}_crop.png",
        )
        return {
            "current_view_id": region_id,
            "current_view_image_path": str(crop_path),
            "current_prompt_evidence": payload,
            "status": "view_evidence_retrieved",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def qwen_analyze_one_view_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：让 Qwen 仅分析当前视图的尺寸组并引用现有证据 ID。

    Args:
        state: 包含 ``current_view_id``、当前证据子集和视图分类。

    Returns:
        严格过滤后的 ``current_view_result``。

    失败状态:
        Qwen 调用或 Schema 校验失败时返回 ``failed``。
    """
    try:
        region_id = state["current_view_id"]
        view_type = state["view_classification"]["identified_views"][region_id]
        evidence = state["current_prompt_evidence"]
        token_ids = {item["token_id"] for item in evidence["ocr_tokens"]}
        numeric_token_ids: set[str] = set()
        for token in evidence["ocr_tokens"]:
            parsed = (
                parse_table_nominal_expression(token["text"])
                if str(token.get("value_role", "")).startswith("table_")
                else parse_dimension_expression(token["text"])
            )
            if parsed.nominal_value is not None or parsed.maximum_value is not None:
                numeric_token_ids.add(token["token_id"])
        table_value_token_ids = {
            item["token_id"]
            for item in evidence["ocr_tokens"]
            if str(item.get("value_role", "")).startswith("table_")
        }
        identity_value_token_ids = {
            token_id
            for group in evidence.get("dimension_groups", [])
            if group.get("evidence_type") == "identity_text"
            for token_id in group.get("token_ids", [])
        }
        numeric_token_ids.update(identity_value_token_ids)
        explicit_counts = [
            count
            for token in evidence["ocr_tokens"]
            if (count := parse_explicit_count_expression(token["text"])) is not None
        ]
        pin_index_token_ids: set[str] = set()
        if explicit_counts:
            count = max(explicit_counts)
            markers = {1, count, count // 2, count // 2 + 1}
            if count % 4 == 0:
                markers.update({count // 4, count // 4 + 1, 3 * count // 4, 3 * count // 4 + 1})
            pin_index_token_ids = {
                token["token_id"]
                for token in evidence["ocr_tokens"]
                if token["text"].strip().isdigit()
                and int(token["text"].strip()) in markers
            }
        line_ids = {item["line_id"] for item in evidence["lines"]}
        all_token_index = {
            item["token_id"]: item for item in state.get("all_ocr_tokens", [])
        }
        gauge_plane_tokens = [
            item
            for item in state.get("all_ocr_tokens", [])
            if item.get("source_region_id") == region_id
            and "gauge plane" in str(item.get("text", "")).casefold()
        ]

        def is_gauge_plane_value(assignment: Any) -> bool:
            """识别 Gage/Gauge Plane 距离，防止误当相邻引脚节距。"""
            if assignment.canonical_name != "terminal_pitch" or not gauge_plane_tokens:
                return False
            for token_id in assignment.token_ids:
                token = all_token_index.get(token_id)
                if token is None:
                    continue
                x1, y1, x2, y2 = token["bbox"]
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                for label in gauge_plane_tokens:
                    lx1, ly1, lx2, ly2 = label["bbox"]
                    lcx, lcy = (lx1 + lx2) / 2.0, (ly1 + ly2) / 2.0
                    if ((cx - lcx) ** 2 + (cy - lcy) ** 2) ** 0.5 <= 260:
                        return True
            return False
        family_id = state["view_classification"]["family_id"]
        family_contract = _view_family_contract(
            image_family_catalog().get(family_id, {}), view_type
        )
        prompt = IMAGE_VIEW_SEMANTIC_USER_PROMPT.format(
            region_id=region_id,
            view_type=view_type,
            family_id=family_id,
            family_contract=json.dumps(family_contract, ensure_ascii=False),
            view_evidence=json.dumps(evidence, ensure_ascii=False),
        )
        if family_id == qfn_ufqfpn.FAMILY_ID and view_type == "dimension_table":
            item = qfn_ufqfpn.semantics_from_table(
                evidence,
                region_id=region_id,
                view_type=view_type,
            )
        else:
            item = _parse_model_json(
                await _invoke_qwen(
                    IMAGE_VIEW_SEMANTIC_SYSTEM_PROMPT,
                    prompt,
                    image_path=Path(state["current_view_image_path"]),
                ),
                QwenViewSemanticResult,
            )
        if (
            family_id != qfn_ufqfpn.FAMILY_ID
            and len(evidence.get("dimension_groups", [])) >= 8
            and (len(item.assignments) < 10 or bool(item.unresolved_fields))
        ):
            # 证据密集视图若单轮覆盖明显不足，允许 Qwen 在相同裁剪图和相同
            # 证据子集上复核一次。复核只能增加/纠正证据 ID 绑定，不能引入
            # 新 token、line 或任何尺寸数值。
            retry_prompt = (
                prompt
                + "\n\n上一轮结果如下：\n"
                + json.dumps(item.model_dump(), ensure_ascii=False)
                + "\n请复核证据密集视图中尚未覆盖或误绑定的合同字段，尤其区分"
                  "外包络、本体轮廓与 Gage Plane 引脚细节。返回完整 JSON；"
                  "仍然只能引用输入中已有 token_id 和 line_id。"
            )
            retry_item = _parse_model_json(
                await _invoke_qwen(
                    IMAGE_VIEW_SEMANTIC_SYSTEM_PROMPT,
                    retry_prompt,
                    image_path=Path(state["current_view_image_path"]),
                ),
                QwenViewSemanticResult,
            )
            retry_names = {
                assignment.canonical_name for assignment in retry_item.assignments
            }
            # 复核轮次的职责是纠正同字段误绑定，因此只要复核返回了某个
            # canonical_name，就替换首轮该字段的全部候选；否则错误候选与
            # 修正候选会在确定性融合时形成伪冲突。
            combined_assignments = [
                assignment
                for assignment in item.assignments
                if assignment.canonical_name not in retry_names
            ]
            seen_assignments = {
                (
                    assignment.canonical_name,
                    tuple(assignment.token_ids),
                    tuple(assignment.line_ids),
                )
                for assignment in combined_assignments
            }
            for assignment in retry_item.assignments:
                key = (
                    assignment.canonical_name,
                    tuple(assignment.token_ids),
                    tuple(assignment.line_ids),
                )
                if key not in seen_assignments:
                    combined_assignments.append(assignment)
                    seen_assignments.add(key)
            item = retry_item.model_copy(update={
                "identified_features": list(dict.fromkeys([
                    *item.identified_features,
                    *retry_item.identified_features,
                ])),
                "assignments": combined_assignments,
                "ambiguities": list(dict.fromkeys([
                    *item.ambiguities,
                    *retry_item.ambiguities,
                ])),
                "confidence": min(item.confidence, retry_item.confidence),
            })
            focus_names = sorted({
                *item.unresolved_fields,
                *[
                    assignment.canonical_name
                    for assignment in item.assignments
                    if assignment.confidence < 0.85
                ],
            })
            if focus_names:
                focus_prompt = (
                    prompt
                    + "\n\n当前复核结果：\n"
                    + json.dumps(item.model_dump(), ensure_ascii=False)
                    + "\n本轮只检查这些未解决或低置信字段："
                    + json.dumps(focus_names, ensure_ascii=False)
                    + "。如相关字段存在内外轮廓颠倒，必须同时返回二者的纠正绑定；"
                      "两侧鸥翼封装中 overall_width 是引脚最外端包络，body_width "
                      "是其内侧塑封轮廓。不要输出任何数值。"
                )
                focus_item = _parse_model_json(
                    await _invoke_qwen(
                        IMAGE_VIEW_SEMANTIC_SYSTEM_PROMPT,
                        focus_prompt,
                        image_path=Path(state["current_view_image_path"]),
                    ),
                    QwenViewSemanticResult,
                )
                focus_returned_names = {
                    assignment.canonical_name
                    for assignment in focus_item.assignments
                }
                focused_assignments = [
                    assignment
                    for assignment in item.assignments
                    if assignment.canonical_name not in focus_returned_names
                ]
                focused_assignments.extend(focus_item.assignments)
                item = focus_item.model_copy(update={
                    "identified_features": list(dict.fromkeys([
                        *item.identified_features,
                        *focus_item.identified_features,
                    ])),
                    "assignments": focused_assignments,
                    "ambiguities": list(dict.fromkeys([
                        *item.ambiguities,
                        *focus_item.ambiguities,
                    ])),
                    "confidence": min(item.confidence, focus_item.confidence),
                })
        context_token_ids = {
            token_id
            for group in evidence.get("dimension_groups", [])
            for token_id in group.get("context_token_ids", [])
        }
        group_lines_by_token = {
            token_id: list(dict.fromkeys([
                *group.get("dimension_line_ids", []),
                *group.get("extension_line_ids", []),
            ]))
            for group in evidence.get("dimension_groups", [])
            for token_id in group.get("token_ids", [])
        }
        item = item.model_copy(update={
            "assignments": [
                assignment.model_copy(update={
                    "token_ids": [
                        token_id
                        for token_id in assignment.token_ids
                        if token_id not in context_token_ids
                    ],
                    "line_ids": (
                        list(dict.fromkeys(
                            line_id
                            for token_id in assignment.token_ids
                            for line_id in group_lines_by_token.get(token_id, [])
                        ))
                        if assignment.canonical_name.endswith("_deg")
                        and any(
                            parse_dimension_expression(
                                all_token_index[token_id]["text"]
                            ).maximum_value is not None
                            for token_id in assignment.token_ids
                            if token_id in all_token_index
                        )
                        else assignment.line_ids
                    ),
                })
                for assignment in item.assignments
                if any(
                    token_id not in context_token_ids
                    for token_id in assignment.token_ids
                )
            ]
        })
        if family_id == gullwing_ic.FAMILY_ID:
            # 当前任务的视图类型只能来自第一阶段分类结果。逐视图 Qwen 偶尔会
            # 在返回 JSON 中自行改写 ``view_type``；若直接用该值执行 Family
            # 校正，同一张图会随机跳过 top/side 的确定性证据规则。先覆盖为
            # 调度器选定的 region/view，确保重复运行得到相同的证据映射。
            item = item.model_copy(update={
                "region_id": region_id,
                "view_type": view_type,
            })
            item = gullwing_ic.reconcile_view_semantics(item, evidence)
        elif family_id == qfn_ufqfpn.FAMILY_ID:
            item = item.model_copy(update={
                "region_id": region_id,
                "view_type": view_type,
            })
            item = qfn_ufqfpn.reconcile_view_semantics(item, evidence)
        accepted = [
            assignment
            for assignment in item.assignments
            if set(assignment.token_ids).issubset(token_ids)
            and bool(set(assignment.token_ids) & numeric_token_ids)
            and not is_gauge_plane_value(assignment)
            and (
                assignment.canonical_name in {"circuit_count", "nominal_pin_count"}
                or not (set(assignment.token_ids) & pin_index_token_ids)
            )
            and (
                (
                    bool(assignment.line_ids)
                    and set(assignment.line_ids).issubset(line_ids)
                )
                or (
                    not assignment.line_ids
                    and bool(
                        set(assignment.token_ids)
                        & (table_value_token_ids | identity_value_token_ids)
                    )
                )
            )
        ]
        rejected = [
            assignment.canonical_name
            for assignment in item.assignments
            if assignment not in accepted
        ]
        if family_id == gullwing_ic.FAMILY_ID:
            terminal_names = {
                "terminal_width", "terminal_thickness", "terminal_length"
            }
            terminal_assignments = [
                assignment
                for assignment in accepted
                if assignment.canonical_name in terminal_names
            ]
            pitch_assignment = next((
                assignment
                for assignment in accepted
                if assignment.canonical_name == "terminal_pitch"
            ), None)

            def assignment_center(assignment: Any) -> tuple[float, float]:
                boxes = [
                    all_token_index[token_id]["bbox"]
                    for token_id in assignment.token_ids
                    if token_id in all_token_index
                ]
                return (
                    sum((box[0] + box[2]) / 2.0 for box in boxes) / len(boxes),
                    sum((box[1] + box[3]) / 2.0 for box in boxes) / len(boxes),
                )

            def assignment_maximum(assignment: Any) -> float:
                values = []
                for token_id in assignment.token_ids:
                    token = all_token_index.get(token_id)
                    if token is None:
                        continue
                    parsed = parse_dimension_expression(token["text"])
                    value = (
                        parsed.maximum_value
                        if parsed.maximum_value is not None
                        else parsed.nominal_value
                    )
                    if value is not None:
                        values.append(float(value))
                return max(values) if values else float("inf")

            unique_terminal_evidence = {
                tuple(sorted(assignment.token_ids))
                for assignment in terminal_assignments
            }
            if pitch_assignment is not None and terminal_assignments:
                pitch_center = assignment_center(pitch_assignment)
                width_assignment = min(
                    terminal_assignments,
                    key=lambda assignment: (
                        (assignment_center(assignment)[0] - pitch_center[0]) ** 2
                        + (assignment_center(assignment)[1] - pitch_center[1]) ** 2
                    ),
                )
                detail_assignments = [
                    assignment
                    for assignment in terminal_assignments
                    if assignment is not width_assignment
                ]
                corrected = {id(width_assignment): "terminal_width"}
                if (
                    len(detail_assignments) == 2
                    and len(unique_terminal_evidence) == 3
                ):
                    thickness_assignment = min(
                        detail_assignments, key=assignment_maximum
                    )
                    length_assignment = max(
                        detail_assignments, key=assignment_maximum
                    )
                    corrected.update({
                        id(thickness_assignment): "terminal_thickness",
                        id(length_assignment): "terminal_length",
                    })
                accepted = [
                    assignment.model_copy(update={
                        "canonical_name": corrected[id(assignment)],
                        "target_feature": f"family_constraint:{corrected[id(assignment)]}",
                    })
                    if id(assignment) in corrected
                    else assignment
                    for assignment in accepted
                ]
                item = item.model_copy(update={
                    "ambiguities": [
                        *item.ambiguities,
                        "gullwing_terminal_semantics:按 pitch 邻近度与 Gage Plane 尺寸关系复核",
                    ]
                })
        item = item.model_copy(update={
            "region_id": region_id,
            "view_type": view_type,
            "assignments": accepted,
            "ambiguities": [
                *item.ambiguities,
                *[f"{name}:证据ID不属于当前视图" for name in rejected],
            ],
        })
        return {
            "current_view_result": item.model_dump(),
            "status": "one_view_analyzed",
        }
    except Exception as exc:
        logger.error("step.image.analyze_view.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def save_view_result_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：保存当前视图结果，并推进待处理视图队列。

    Args:
        state: 包含当前视图结果、已有结果和待处理队列。

    Returns:
        更新后的 ``per_view_semantics`` 和 ``pending_view_ids``。

    失败状态:
        写盘失败时返回 ``failed``。
    """
    try:
        results = [*state.get("per_view_semantics", []), state["current_view_result"]]
        pending = list(state.get("pending_view_ids", []))[1:]
        region_id = state["current_view_id"]
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts[f"view_semantics_{region_id}"] = _write_json(
            Path(state["output_dir"]) / f"view_semantics_{region_id}.json",
            state["current_view_result"],
        )
        return {
            "per_view_semantics": results,
            "pending_view_ids": pending,
            "current_view_id": "",
            "current_view_image_path": "",
            "current_prompt_evidence": {},
            "current_view_result": {},
            "artifact_paths": artifacts,
            "status": "view_result_saved",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def merge_view_results_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：确定性合并全部视图结果，不再次调用 Qwen。

    Args:
        state: 包含第一阶段视图分类和全部逐视图结果。

    Returns:
        合并后的 ``semantic_result``。

    失败状态:
        Schema 不一致时返回 ``failed``。
    """
    try:
        merged = merge_view_semantics(
            QwenViewClassificationResult.model_validate(state["view_classification"]),
            [
                QwenViewSemanticResult.model_validate(item)
                for item in state.get("per_view_semantics", [])
            ],
        )
        payload = merged.model_dump()
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["qwen_semantics"] = _write_json(
            Path(state["output_dir"]) / "qwen_semantics.json", payload
        )
        return {
            "semantic_result": payload,
            "artifact_paths": artifacts,
            "status": "view_results_merged",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


@vision_node
def extract_visual_evidence_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：运行视图切分、PaddleOCR、Canny/Hough 和证据邻接分析。

    Args:
        state: 包含预处理灰度图、二值图和输入图片哈希。

    Returns:
        ``visual_evidence`` 及落盘 JSON 路径。

    失败状态:
        OCR 依赖缺失、模型加载或几何检测失败时返回 ``failed``。
    """
    try:
        preprocessing = state["preprocessing"]
        evidence_path = Path(state["output_dir"]) / "visual_evidence.json"
        if evidence_path.is_file():
            cached = VisualEvidenceBundle.model_validate_json(
                evidence_path.read_text(encoding="utf-8")
            )
            if cached.image_sha256 == state["image_meta"]["sha256"]:
                artifacts = dict(state.get("artifact_paths", {}))
                artifacts["visual_evidence"] = str(evidence_path)
                return {
                    "visual_evidence": cached.model_dump(),
                    "artifact_paths": artifacts,
                    "status": "visual_evidence_extracted",
                }
        regions = split_view_regions(Path(preprocessing["binary_path"]))
        tokens = extract_ocr_tokens(Path(preprocessing["gray_path"]), regions)
        _write_json(
            Path(state["output_dir"]) / "ocr_tokens.json",
            [token.model_dump() for token in tokens],
        )
        lines, arrows, links = detect_geometry(
            Path(preprocessing["binary_path"]), tokens, regions
        )
        evidence = VisualEvidenceBundle(
            image_sha256=state["image_meta"]["sha256"],
            image_size=(int(preprocessing["width"]), int(preprocessing["height"])),
            regions=regions,
            ocr_tokens=tokens,
            lines=lines,
            arrows=arrows,
            token_line_links=links,
        )
        payload = evidence.model_dump()
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["visual_evidence"] = _write_json(evidence_path, payload)
        return {
            "visual_evidence": payload,
            "artifact_paths": artifacts,
            "status": "visual_evidence_extracted",
        }
    except Exception as exc:
        logger.error("step.image.evidence.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


def _is_excluded_geometry_view(view_type: str) -> bool:
    normalized = view_type.casefold().replace("-", "_").replace(" ", "_")
    return any(item in normalized for item in (
        "title", "revision", "note", "electrical", "ordering", "footprint",
        "land_pattern", "pcb", "layout", "电气", "焊盘", "推荐布局",
    ))


def _is_geometry_view(view_type: str) -> bool:
    """判断第一阶段标签是否属于需要提取尺寸的几何视图。"""
    normalized = view_type.casefold().replace("-", "_").replace(" ", "_")
    if _is_excluded_geometry_view(view_type):
        return False
    if "dimension_table" in normalized:
        return True
    included = (
        "front", "side", "top", "bottom", "rear", "back",
        "isometric", "detail", "composite", "outline", "package",
        "主视", "侧视", "俯视", "后视", "底视", "综合", "外形",
    )
    return any(item in normalized for item in included)


async def _invoke_qwen(
    system_prompt: str,
    user_prompt: str,
    *,
    image_path: Path | None = None,
) -> str:
    """执行一次有 JSON 输出约束的 Qwen 调用，可附带区域总览图。"""
    llm = get_llm("drawing_extract").bind(
        max_tokens=1800,
        response_format={"type": "json_object"},
        extra_body={"enable_thinking": False},
    )
    content: Any = user_prompt
    if image_path is not None:
        content = [
            {"type": "text", "text": user_prompt},
            {
                "type": "image_url",
                "image_url": {"url": image_as_data_url(image_path)},
            },
        ]
    response = await asyncio.wait_for(
        llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),
        ]),
        timeout=90.0,
    )
    return _response_text(response)


def _view_family_contract(family_contract: dict[str, Any], view_type: str) -> dict[str, Any]:
    """按当前视图裁剪字段合同，仅减少 Prompt，不提供任何尺寸答案。"""
    normalized = view_type.casefold()
    if "dimension_table" in normalized:
        return family_contract
    family_view_groups = family_contract.get("view_parameter_groups", {})
    if family_view_groups:
        selected = next(
            (
                set(fields)
                for key, fields in family_view_groups.items()
                if key.casefold() in normalized
            ),
            set(family_contract.get("required_parameters", []))
            | set(family_contract.get("optional_parameters", [])),
        )
        return {
            "required_parameters": [
                name
                for name in family_contract.get("required_parameters", [])
                if name in selected
            ],
            "optional_parameters": [
                name for name in family_contract.get("optional_parameters", [])
                if name in selected
            ],
            "required_features": family_contract.get("required_features", []),
            "parameter_guidance": {
                name: guidance
                for name, guidance in family_contract.get(
                    "parameter_guidance", {}
                ).items()
                if name in selected
            },
        }
    fields_by_view = {
        "top": {
            "nominal_pin_count", "terminal_pitch", "pin_span",
            "overall_length", "overall_width", "body_length", "body_width",
            "terminal_span", "terminal_length", "lead_projection", "terminal_width",
            "terminal_thickness", "mold_draft_angle_top_deg",
            "mold_draft_angle_bottom_deg", "lead_angle_deg",
        },
        "front": {
            "circuit_count", "overall_width", "front_plate_height",
            "front_shell_top_width", "front_shell_bottom_width", "front_shell_height",
            "mounting_center_span", "mounting_hole_diameter", "mounting_outer_diameter",
            "contact_pitch", "contact_column_span", "contact_row_spacing",
            "contact_outer_diameter", "contact_inner_diameter",
        },
        "side": {
            "plate_thickness", "front_projection_depth", "shell_wall_thickness",
            "rear_body_depth", "rear_housing_height", "signal_pin_width",
            "pin_tail_length", "overall_height", "total_height", "housing_height",
            "body_standoff", "terminal_length", "terminal_thickness", "lead_projection",
            "lead_angle_deg",
            "mold_draft_angle_top_deg", "mold_draft_angle_bottom_deg",
        },
        "pcb": {
            "circuit_count", "contact_pitch", "contact_column_span",
            "contact_row_spacing", "signal_pin_width", "mounting_center_span",
            "mounting_hole_diameter",
        },
    }
    selected = next(
        (fields for key, fields in fields_by_view.items() if key in normalized),
        set(family_contract.get("required_parameters", []))
        | set(family_contract.get("optional_parameters", [])),
    )
    available = set(family_contract.get("required_parameters", []))
    if available and not (available & set().union(*fields_by_view.values())):
        selected = available
    return {
        "required_parameters": [
            name for name in family_contract.get("required_parameters", []) if name in selected
        ],
        "optional_parameters": [
            name for name in family_contract.get("optional_parameters", []) if name in selected
        ],
        "required_features": family_contract.get("required_features", []),
        "parameter_guidance": {
            name: guidance
            for name, guidance in family_contract.get("parameter_guidance", {}).items()
            if name in selected
        },
    }


async def qwen_assign_semantics_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：分两轮调用 Qwen，并由确定性程序合并逐视图语义。

    Args:
        state: 包含完整 ``visual_evidence``；不传原图或 Golden Reference。

    Returns:
        ``view_classification``、``per_view_semantics`` 和合并后的
        ``semantic_result``。完整证据仍保留在 State 中，每次模型调用只看到
        当前任务需要的区域摘要或单视图证据子集。

    失败状态:
        Qwen 调用失败或输出 schema 外数值字段时返回 ``failed``；无证据的
        assignment 会被确定性丢弃并交由尺寸门禁安全停止。
    """
    try:
        evidence = VisualEvidenceBundle.model_validate(state["visual_evidence"])
        catalog = image_family_catalog()
        classify_prompt = IMAGE_VIEW_CLASSIFICATION_USER_PROMPT.format(
            family_catalog=json.dumps(
                template_category_catalog(), ensure_ascii=False
            ),
            region_summaries=json.dumps(
                build_region_summaries(evidence), ensure_ascii=False
            ),
        )
        classification = _parse_model_json(
            await _invoke_qwen(
                IMAGE_VIEW_CLASSIFICATION_SYSTEM_PROMPT, classify_prompt
            ),
            QwenViewClassificationResult,
        )
        classification = reconcile_image_family_classification(
            classification, evidence.ocr_tokens
        )
        valid_region_ids = {region.region_id for region in evidence.regions}
        invalid_regions = sorted(set(classification.identified_views) - valid_region_ids)
        if invalid_regions:
            classification = classification.model_copy(update={
                "identified_views": {
                    region_id: view_type
                    for region_id, view_type in classification.identified_views.items()
                    if region_id in valid_region_ids
                },
                "ambiguities": [
                    *classification.ambiguities,
                    f"Qwen 引用了不存在的区域：{', '.join(invalid_regions)}",
                ],
            })

        output_dir = Path(state["output_dir"])
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["view_classification"] = _write_json(
            output_dir / "view_classification.json", classification.model_dump()
        )
        family_contract = catalog.get(classification.family_id, {})
        per_view: list[QwenViewSemanticResult] = []
        for region_id, view_type in classification.identified_views.items():
            if not _is_geometry_view(view_type):
                continue
            view_evidence = retrieve_view_evidence(evidence, region_id)
            token_ids = {
                item["token_id"] for item in view_evidence["ocr_tokens"]
            }
            numeric_token_ids = {
                item["token_id"]
                for item in view_evidence["ocr_tokens"]
                if (
                    parse_table_nominal_expression(item["text"])
                    if item.get("value_role") == "table_nominal"
                    else parse_dimension_expression(item["text"])
                ).nominal_value is not None
            }
            table_value_token_ids = {
                item["token_id"]
                for item in view_evidence["ocr_tokens"]
                if item.get("value_role") == "table_nominal"
            }
            line_ids = {item["line_id"] for item in view_evidence["lines"]}
            if not token_ids or not line_ids:
                per_view.append(QwenViewSemanticResult(
                    region_id=region_id,
                    view_type=view_type,
                    unresolved_fields=[f"{region_id}:no_linked_dimension_evidence"],
                    confidence=0.0,
                ))
                continue
            prompt = IMAGE_VIEW_SEMANTIC_USER_PROMPT.format(
                region_id=region_id,
                view_type=view_type,
                family_id=classification.family_id,
                family_contract=json.dumps(family_contract, ensure_ascii=False),
                view_evidence=json.dumps(view_evidence, ensure_ascii=False),
            )
            item = _parse_model_json(
                await _invoke_qwen(IMAGE_VIEW_SEMANTIC_SYSTEM_PROMPT, prompt),
                QwenViewSemanticResult,
            )
            rejected = []
            accepted = []
            for assignment in item.assignments:
                if (
                    set(assignment.token_ids).issubset(token_ids)
                    and bool(set(assignment.token_ids) & numeric_token_ids)
                    and (
                        (
                            bool(assignment.line_ids)
                            and set(assignment.line_ids).issubset(line_ids)
                        )
                        or (
                            not assignment.line_ids
                            and bool(set(assignment.token_ids) & table_value_token_ids)
                        )
                    )
                ):
                    accepted.append(assignment)
                else:
                    rejected.append(assignment.canonical_name)
            item = item.model_copy(update={
                "region_id": region_id,
                "view_type": view_type,
                "assignments": accepted,
                "ambiguities": [
                    *item.ambiguities,
                    *[
                        f"{name}:引用了当前视图之外或缺失的证据 ID"
                        for name in rejected
                    ],
                ],
            })
            per_view.append(item)
            artifacts[f"view_semantics_{region_id}"] = _write_json(
                output_dir / f"view_semantics_{region_id}.json", item.model_dump()
            )

        semantics = merge_view_semantics(classification, per_view)
        payload = semantics.model_dump()
        path = output_dir / "qwen_semantics.json"
        artifacts["qwen_semantics"] = _write_json(path, payload)
        return {
            "view_classification": classification.model_dump(),
            "per_view_semantics": [item.model_dump() for item in per_view],
            "semantic_result": payload,
            "artifact_paths": artifacts,
            "status": "semantics_assigned",
        }
    except Exception as exc:
        logger.error("step.image.semantics.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def fuse_evidence_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：用本地解析器恢复数值，并与 Qwen 证据引用确定性融合。

    Args:
        state: 包含 ``visual_evidence`` 和 ``semantic_result``。

    Returns:
        ``fused_evidence`` 和融合证据文件。

    失败状态:
        Schema 或融合执行异常时返回 ``failed``。
    """
    try:
        evidence = VisualEvidenceBundle.model_validate(state["visual_evidence"])
        semantics = QwenSemanticResult.model_validate(state["semantic_result"])
        collisions = find_semantic_collisions(evidence, semantics)
        fused = fuse_evidence(evidence, semantics)
        if collisions:
            fused = fused.model_copy(update={"conflicting_fields": sorted({
                *fused.conflicting_fields,
                *(name for item in collisions for name in item["names"]),
            })})
        fused = derive_family_parameters(fused)
        payload = fused.model_dump()
        path = Path(state["output_dir"]) / "fused_evidence.json"
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["fused_evidence"] = _write_json(path, payload)
        return {
            "fused_evidence": payload,
            "semantic_collisions": collisions,
            "artifact_paths": artifacts,
            "status": "evidence_fused",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def validate_dimension_chain_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：用通用几何关系验证尺寸链，并把冲突写回融合证据。

    Args:
        state: 包含确定性融合后的参数。

    Returns:
        ``dimension_chain_result`` 和追加冲突后的 ``fused_evidence``。

    失败状态:
        Schema 处理异常时返回 ``failed``；尺寸链不一致由后续门禁安全停止。
    """
    try:
        fused = FusedEvidence.model_validate(state["fused_evidence"])
        values = {item.canonical_name: item.value for item in fused.parameters}
        conflicts: list[str] = []
        checked_relationships = 0
        inequalities = (
            ("front_shell_top_width", "overall_width"),
            ("front_shell_bottom_width", "overall_width"),
            ("rear_housing_width", "overall_width"),
            ("contact_inner_diameter", "contact_outer_diameter"),
        )
        for smaller, larger in inequalities:
            if smaller in values and larger in values and values[smaller] >= values[larger]:
                conflicts.append(f"{smaller}>={larger}")
            if smaller in values and larger in values:
                checked_relationships += 1
        required = {"circuit_count", "contact_pitch", "contact_column_span"}
        if required.issubset(values):
            checked_relationships += 1
            count = int(round(values["circuit_count"]))
            larger_row_count = (count + 1) // 2
            expected_span = max(0, larger_row_count - 1) * values["contact_pitch"]
            if abs(expected_span - values["contact_column_span"]) > 0.10:
                conflicts.append("contact_column_span!=pitch*(row_count-1)")
        gullwing_required = {
            "nominal_pin_count", "terminal_pitch", "pin_span",
            "total_height", "housing_height", "body_standoff",
            "overall_width", "body_width",
        }
        if fused.family_id == "ic/gullwing_ic" and gullwing_required.issubset(values):
            checked_relationships += 3
            count = int(round(values["nominal_pin_count"]))
            if count < 2 or count % 2:
                conflicts.append("nominal_pin_count:not_even")
            else:
                expected_span = (count // 2 - 1) * values["terminal_pitch"]
                if abs(expected_span - values["pin_span"]) > 0.05:
                    conflicts.append("pin_span!=pitch*(pins_per_side-1)")
            if values["body_width"] >= values["overall_width"]:
                conflicts.append("body_width>=overall_width")
            expected_height = values["housing_height"] + values["body_standoff"]
            if abs(expected_height - values["total_height"]) > 0.05:
                conflicts.append("total_height!=housing_height+body_standoff")
        quad_required = {
            "nominal_pin_count", "terminal_pitch", "terminal_span",
            "total_height", "housing_height", "body_standoff",
            "overall_length", "overall_width", "body_length", "body_width",
            "terminal_length", "lead_projection",
        }
        if fused.family_id == "ic/quad_gullwing_ic" and quad_required.issubset(values):
            checked_relationships += 6
            count = int(round(values["nominal_pin_count"]))
            if count < 4 or count % 4:
                conflicts.append("nominal_pin_count:not_divisible_by_4")
            else:
                expected_span = (count // 4 - 1) * values["terminal_pitch"]
                if abs(expected_span - values["terminal_span"]) > 0.05:
                    conflicts.append("terminal_span!=pitch*(pins_per_side-1)")
            if values["body_length"] >= values["overall_length"]:
                conflicts.append("body_length>=overall_length")
            if values["body_width"] >= values["overall_width"]:
                conflicts.append("body_width>=overall_width")
            projections = (
                (values["overall_length"] - values["body_length"]) / 2.0,
                (values["overall_width"] - values["body_width"]) / 2.0,
            )
            if any(abs(value - values["lead_projection"]) > 0.05 for value in projections):
                conflicts.append("lead_projection!=half_overall_body_difference")
            if not 0.0 < values["terminal_length"] <= values["lead_projection"]:
                conflicts.append("terminal_length:outside_lead_projection")
            if values["housing_height"] + values["body_standoff"] > values["total_height"] + 0.05:
                conflicts.append("housing_height+body_standoff>total_height")
        qfn_required = {
            "nominal_pin_count", "body_length", "body_width", "total_height",
            "body_standoff", "terminal_pitch", "terminal_length",
            "terminal_width", "terminal_height",
        }
        if fused.family_id == "ic/qfn_ufqfpn" and qfn_required.issubset(values):
            checked_relationships += 3
            count = int(round(values["nominal_pin_count"]))
            if count < 4 or count % 4:
                conflicts.append("nominal_pin_count:not_divisible_by_4")
            if values["body_standoff"] >= values["total_height"]:
                conflicts.append("body_standoff>=total_height")
            if values["terminal_height"] > values["total_height"]:
                conflicts.append("terminal_height>total_height")
            if (
                "exposed_pad_length" in values
                and values["exposed_pad_length"] >= values["body_length"]
            ):
                conflicts.append("exposed_pad_length>=body_length")
            if (
                "exposed_pad_width" in values
                and values["exposed_pad_width"] >= values["body_width"]
            ):
                conflicts.append("exposed_pad_width>=body_width")
        chip_required = {
            "body_length", "body_width", "body_height", "terminal_length"
        }
        if fused.family_id in {
            "resistor/two_terminal_chip", "capacitor/two_terminal_chip"
        } and chip_required.issubset(values):
            checked_relationships += 2
            if values["terminal_length"] * 2.0 >= values["body_length"]:
                conflicts.append("2*terminal_length>=body_length")
            if min(values[name] for name in chip_required) <= 0.0:
                conflicts.append("two_terminal_chip:non_positive_dimension")
        payload = fused.model_dump()
        payload["conflicting_fields"] = sorted(set([
            *payload.get("conflicting_fields", []), *conflicts
        ]))
        result = {
            "passed": not conflicts,
            "conflicts": conflicts,
            "checked_relationships": checked_relationships,
        }
        _write_json(
            Path(state["output_dir"]) / "dimension_chain.json", result
        )
        return {
            "fused_evidence": payload,
            "dimension_chain_result": result,
            "status": "dimension_chain_validated",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def validate_dimensions_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：在 Feature IR 前检查证据 ID、bbox、置信度、单位和尺寸链冲突。

    Args:
        state: 包含融合证据和 Qwen 候选器件族。

    Returns:
        ``dimension_gate`` 以及继续或安全停止状态。

    失败状态:
        不满足任一关键条件时返回 ``stopped_insufficient_extraction``。
    """
    fused = FusedEvidence.model_validate(state["fused_evidence"])
    contract = image_family_catalog().get(fused.family_id)
    if "human_follow_up:family_id" in fused.unresolved_fields:
        gate = {
            "passed": False,
            "status": "needs_human_follow_up",
            "missing_fields": ["family_id"],
            "conflicting_fields": [],
            "low_confidence_fields": [],
            "evidence_report": "两端片式器件无法确定属于电阻还是电容。",
            "candidates": [
                "resistor/two_terminal_chip",
                "capacitor/two_terminal_chip",
            ],
            "follow_up_question": "该两端片式器件属于电阻还是电容？",
        }
    elif contract is None:
        gate = {
            "passed": False,
            "status": "stopped_unsupported_template",
            "missing_fields": ["implemented_template"],
            "conflicting_fields": [],
            "low_confidence_fields": [],
            "evidence_report": f"尚未实现的数模模板：{fused.family_id or fused.category_id or '<empty>'}",
        }
    else:
        gate = validate_fused_dimensions(
            fused,
            required_fields=tuple(contract["required_parameters"]),
            required_features=tuple(contract["required_features"]),
        ).model_dump()
    _write_json(Path(state["output_dir"]) / "dimension_gate.json", gate)
    return {
        "dimension_gate": gate,
        "status": "dimensions_valid" if gate["passed"] else gate["status"],
    }


async def classify_family_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：在尺寸门禁通过后确认 Qwen 识别的白名单器件族。

    Args:
        state: 包含已通过门禁的融合证据。

    Returns:
        白名单 ``family_id``。

    失败状态:
        器件族未注册时返回 ``failed``。
    """
    family_id = str(state["fused_evidence"].get("family_id") or "")
    if family_id not in image_family_catalog():
        return {
            "errors": _errors(state, f"未注册的图片建模器件族：{family_id}"),
            "status": "failed",
        }
    return {
        "category_id": str(state["fused_evidence"].get("category_id") or ""),
        "subcategory_id": state["fused_evidence"].get("subcategory_id"),
        "family_id": family_id,
        "status": "family_classified",
    }


async def create_feature_ir_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：把通过门禁的参数规划为全量带证据来源的 Feature IR。

    Args:
        state: 包含 ``family_id``、融合证据和输入图片哈希。

    Returns:
        ``feature_ir`` 及其 JSON 文件。

    失败状态:
        任一参数缺证据、缺单位或 Family 规划失败时返回 ``failed``。
    """
    try:
        feature_ir = create_evidence_feature_ir(
            state["family_id"],
            state["fused_evidence"],
            source_image_sha256=state["image_meta"]["sha256"],
        )
        path = Path(state["output_dir"]) / "feature_ir.json"
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["feature_ir"] = _write_json(path, feature_ir)
        return {
            "feature_ir": feature_ir,
            "artifact_paths": artifacts,
            "needs_review": bool(state.get("needs_review") or feature_ir.get("assumptions")),
            "status": "feature_ir_created",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


def _bounding_box_sizes(bounding_box: dict[str, Any]) -> dict[str, float]:
    """计算包围盒三个轴的长度。"""
    return {
        axis: float(bounding_box[f"{axis}max"]) - float(bounding_box[f"{axis}min"])
        for axis in "xyz"
    }


def _reference_candidate_is_compatible(
    candidate: dict[str, Any],
    metrics: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]]:
    """仅用自有 Feature IR 验证公开 STEP 是否可直接使用或受限缩放。"""
    target_sizes = _bounding_box_sizes(expected["bounding_box"])
    source_sizes = _bounding_box_sizes(metrics["bounding_box"])
    bbox_delta = {
        axis: source_sizes[axis] - target_sizes[axis] for axis in "xyz"
    }
    invariant_bbox_delta = [
        source - target
        for source, target in zip(
            sorted(source_sizes.values()), sorted(target_sizes.values())
        )
    ]
    exact_geometry = max(
        (abs(value) for value in invariant_bbox_delta), default=0.0
    ) <= 0.05
    exact_solid_count = expected.get("solid_count")
    topology_compatible = (
        metrics["solid_count"] == int(exact_solid_count)
        if exact_solid_count is not None
        else metrics["solid_count"] >= int(expected.get("minimum_solid_count", 1))
    )
    scale_factors = {
        axis: target_sizes[axis] / source_sizes[axis]
        if source_sizes[axis] > 1e-9
        else 0.0
        for axis in "xyz"
    }
    validation = {
        "is_valid": bool(metrics["is_valid"]),
        "solid_count": int(metrics["solid_count"]),
        "face_count": int(metrics["face_count"]),
        "edge_count": int(metrics["edge_count"]),
        "source_bbox_size_mm": source_sizes,
        "target_bbox_size_mm": target_sizes,
        "bbox_size_delta_mm": bbox_delta,
        "orientation_invariant_bbox_delta_mm": invariant_bbox_delta,
        "scale_factors": scale_factors,
        "topology_compatible": topology_compatible,
    }
    if not metrics["is_valid"]:
        return False, "invalid_step", validation
    if candidate.get("match_type") == "exact" and exact_geometry:
        return True, "exact", validation
    scalable = topology_compatible and all(
        0.80 <= factor <= 1.25 for factor in scale_factors.values()
    )
    if scalable:
        return True, "similar", validation
    return False, "incompatible_geometry", validation


async def search_reference_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：在尺寸语义分析前按厂商、料号和封装搜索公开 STEP。

    Args:
        state: 包含完整 OCR token、Jev 路由结果和输出目录。

    Returns:
        产品标识、厂商名称和早期检索审计报告；不创建或修改 Feature IR。

    失败状态:
        网络不可用或无公开直链均视为可回退状态，继续图纸证据链。
    """
    identifiers = extract_product_identifiers(state.get("all_ocr_tokens", []))
    if not identifiers:
        # Jev 输入文字来自同一批 confidence>=0.80 的 OCR token。部分长链运行中
        # 全量 token 可能未保留到检索节点，此处只复用已审计文字，不生成料号。
        identity_texts = state.get("jev_decision", {}).get(
            "input_state", {}
        ).get("drawing_text", [])
        identifiers = extract_product_identifiers([
            {
                "text": text,
                "confidence": 1.0,
                "bbox": [0, index, 0, index],
            }
            for index, text in enumerate(identity_texts)
        ])
    manufacturers = extract_manufacturer_names(state.get("all_ocr_tokens", []))
    package_type = str(
        state.get("jev_decision", {}).get("input_state", {}).get("package_type")
        or state.get("view_classification", {}).get("package_type")
        or ""
    )
    output_dir = Path(state["output_dir"])
    report = await search_public_step_candidates(
        identifiers=identifiers,
        package_type=package_type,
        output_dir=output_dir,
        manufacturers=manufacturers,
    )
    exact_candidates = [
        candidate
        for candidate in report.get("candidates", [])
        if candidate.get("match_type") == "exact"
    ]
    report["stage"] = "early_discovery"
    report["early_exact_candidate"] = exact_candidates[0] if exact_candidates else None
    report["policy"] = {
        "exact_product": "read_back_then_direct_output",
        "similar_product": "defer_until_feature_ir",
        "not_found": "local_feature_ir_builder",
        "dimension_feedback": "forbidden",
    }
    output_dir = Path(state["output_dir"])
    artifacts = dict(state.get("artifact_paths", {}))
    artifacts["reference_step_search"] = _write_json(
        output_dir / "reference_step_search.json", report
    )
    logger.info(
        "step.image.reference.early_search_completed",
        query=report.get("query", ""),
        manufacturers=manufacturers,
        identifiers=identifiers,
        candidate_count=len(report.get("candidates", [])),
        exact_found=bool(exact_candidates),
    )
    return {
        "product_identifiers": identifiers,
        "manufacturer_names": manufacturers,
        "reference_step_search": report,
        "artifact_paths": artifacts,
        "status": "reference_step_searched",
    }


@cad_node
def prepare_exact_reference_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：回读验证精确产品 STEP，成功后直接形成候选产物。

    Args:
        state: 包含早期检索报告、Jev Family 和标准输出目录。

    Returns:
        STEP 路径、基础拓扑验证、模型来源及检索审计报告。

    失败状态:
        文件无效或无法回读时返回可回退状态，继续 Qwen/Feature IR 链路。
    """
    report = dict(state.get("reference_step_search", {}))
    selected = report.get("early_exact_candidate")
    if not selected:
        return {"status": "reference_step_unavailable"}
    step_path = Path(state["output_dir"]) / (
        f"{safe_file_stem(Path(state['image_path']).stem)}.step"
    )
    try:
        source_path = Path(selected["local_path"])
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, step_path)
        cq = load_cadquery()
        metrics = read_step_metrics(cq, step_path)
        passed = bool(
            metrics["is_valid"]
            and metrics["solid_count"] >= 1
            and metrics["face_count"] >= 4
            and metrics["edge_count"] >= 4
            and metrics["volume_mm3"] > 0.0
        )
        verification = {
            "passed": passed,
            "is_valid": metrics["is_valid"],
            "solid_count": metrics["solid_count"],
            "face_count": metrics["face_count"],
            "edge_count": metrics["edge_count"],
            "volume_mm3": metrics["volume_mm3"],
            "bounding_box": metrics["bounding_box"],
            "model_source": "web_exact",
            "scope": "identity_and_brep_only",
        }
        if not passed:
            raise RuntimeError("精确产品 STEP 未通过 OpenCascade 基础实体校验")
        report["selected_candidate"] = selected
        report["preparation"] = {
            "status": "prepared",
            "mode": "direct_copy_after_brep_validation",
            "validation": verification,
        }
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["step"] = str(step_path)
        artifacts["reference_step_search"] = _write_json(
            Path(state["output_dir"]) / "reference_step_search.json", report
        )
        family_id = str(
            state.get("jev_decision", {}).get("resolved_family_id")
            or state.get("view_classification", {}).get("family_id")
            or ""
        )
        return {
            "family_id": family_id,
            "reference_step_search": report,
            "artifact_paths": artifacts,
            "verification": verification,
            "model_source": "web_exact",
            "cadquery_version": str(getattr(cq, "__version__", "unknown")),
            "status": "step_verified",
        }
    except Exception as exc:
        step_path.unlink(missing_ok=True)
        report["preparation"] = {"status": "fallback", "error": str(exc)}
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["reference_step_search"] = _write_json(
            Path(state["output_dir"]) / "reference_step_search.json", report
        )
        logger.warning("step.image.reference.exact_fallback", error=str(exc))
        return {
            "reference_step_search": report,
            "artifact_paths": artifacts,
            "status": "reference_step_unusable",
        }


@cad_node
def validate_reference_step_candidates_node(
    state: dict[str, Any],
) -> dict[str, Any]:
    """作用：Feature IR 完成后验证早期检索到的相似 STEP 是否兼容。

    Args:
        state: 包含早期候选和只来源于图纸证据的 Feature IR。

    Returns:
        带几何兼容性结论和可选 ``selected_candidate`` 的检索报告。

    失败状态:
        无兼容候选时正常回退本地参数化 Builder，不视为技术失败。
    """
    report = dict(state.get("reference_step_search", {}))
    expected = state["feature_ir"]["expected_geometry"]
    compatible_candidates: list[dict[str, Any]] = []
    cq = None
    for candidate in report.get("candidates", []):
        try:
            cq = cq or load_cadquery()
            metrics = read_step_metrics(cq, Path(candidate["local_path"]))
            compatible, use_mode, validation = _reference_candidate_is_compatible(
                candidate, metrics, expected
            )
            candidate["validation"] = validation
            candidate["use_mode"] = use_mode
            if compatible:
                compatible_candidate = dict(candidate)
                compatible_candidate["match_type"] = use_mode
                compatible_candidates.append(compatible_candidate)
        except Exception as exc:
            candidate["validation"] = {
                "is_valid": False,
                "error": str(exc),
            }
    compatible_candidates.sort(
        key=lambda item: 0 if item.get("match_type") == "exact" else 1
    )
    selected = compatible_candidates[0] if compatible_candidates else None
    report["selected_candidate"] = selected
    report["stage"] = "feature_ir_compatibility_validation"
    report["policy"] = {
        "dimensions_source": "feature_ir_from_drawing_evidence_only",
        # TODO: 这几个数值超参数, 可能后期需要微调
        "exact_bbox_tolerance_mm": 0.05,
        "similar_scale_range": [0.80, 1.25],
        "fallback": "local_feature_ir_builder",
    }
    output_dir = Path(state["output_dir"])
    artifacts = dict(state.get("artifact_paths", {}))
    artifacts["reference_step_search"] = _write_json(
        output_dir / "reference_step_search.json", report
    )
    logger.info(
        "step.image.reference.validation_completed",
        query=report.get("query", ""),
        identifier_count=len(state.get("product_identifiers", [])),
        candidate_count=len(report.get("candidates", [])),
        selected_mode=(selected or {}).get("match_type", "local_fallback"),
    )
    return {
        "reference_step_search": report,
        "artifact_paths": artifacts,
        "status": "reference_step_candidates_validated",
    }


@cad_node
def prepare_reference_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：复制相同产品 STEP，或按 Feature IR 尺寸适配相似封装 STEP。

    Args:
        state: 包含已验证候选、目标 Feature IR 和标准输出目录。

    Returns:
        标准 STEP 产物路径、模型来源和更新后的检索审计报告。

    失败状态:
        候选回读或受限变换失败时返回可回退状态，由 Graph 转入原 Builder。
    """
    report = dict(state.get("reference_step_search", {}))
    selected = report.get("selected_candidate")
    if not selected:
        return {"status": "reference_step_unavailable"}
    step_path = Path(state["output_dir"]) / (
        f"{safe_file_stem(Path(state['image_path']).stem)}.step"
    )
    try:
        cq = load_cadquery()
        match_type = str(selected.get("match_type", "similar"))
        prepare_reference_step(
            cq,
            Path(selected["local_path"]),
            step_path,
            state["feature_ir"]["expected_geometry"]["bounding_box"],
            adapt_dimensions=match_type != "exact",
        )
        metrics = read_step_metrics(cq, step_path)
        compatible, _, validation = _reference_candidate_is_compatible(
            {"match_type": "exact"}, metrics, state["feature_ir"]["expected_geometry"]
        )
        if not compatible:
            raise RuntimeError("公开 STEP 适配后未通过 Feature IR 几何约束")
        report["preparation"] = {
            "status": "prepared",
            "mode": "direct_copy" if match_type == "exact" else "bounded_dimension_adaptation",
            "validation": validation,
        }
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["step"] = str(step_path)
        artifacts["reference_step_search"] = _write_json(
            Path(state["output_dir"]) / "reference_step_search.json", report
        )
        return {
            "reference_step_search": report,
            "artifact_paths": artifacts,
            "model_source": "web_exact" if match_type == "exact" else "web_similar_adapted",
            "cadquery_version": str(getattr(cq, "__version__", "unknown")),
            "status": "reference_step_prepared",
        }
    except Exception as exc:
        step_path.unlink(missing_ok=True)
        report["preparation"] = {"status": "fallback", "error": str(exc)}
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["reference_step_search"] = _write_json(
            Path(state["output_dir"]) / "reference_step_search.json", report
        )
        logger.warning("step.image.reference.fallback", error=str(exc))
        return {
            "reference_step_search": report,
            "artifact_paths": artifacts,
            "status": "reference_step_unusable",
        }


@cad_node
def build_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：让参数化 Builder 只消费 Feature IR，并导出候选 STEP。

    Args:
        state: 包含证据版 ``feature_ir`` 和输出目录。

    Returns:
        候选 STEP 路径和 CadQuery 版本。

    失败状态:
        IR 执行、布尔运算或 STEP 导出失败时返回 ``failed``。
    """
    try:
        cq = load_cadquery()
        model = build_feature_model(cq, state["feature_ir"])
        step_path = Path(state["output_dir"]) / f"{safe_file_stem(Path(state['image_path']).stem)}.step"
        cq.exporters.export(model, str(step_path), exportType="STEP")
        if not step_path.is_file() or step_path.stat().st_size == 0:
            raise RuntimeError("候选 STEP 未成功写入")
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["step"] = str(step_path)
        return {
            "artifact_paths": artifacts,
            "model_source": "local_feature_ir",
            "status": "step_built",
            "cadquery_version": str(getattr(cq, "__version__", "unknown")),
        }
    except Exception as exc:
        logger.error("step.image.build.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


@cad_node
def verify_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：用 OpenCascade 回读候选 STEP，并核对自有 IR 预期。

    Args:
        state: 包含候选 STEP 路径和 Feature IR。

    Returns:
        实体、面、边、体积和包围盒验证结果。

    失败状态:
        STEP 不可回读或自有几何约束不满足时返回 ``failed``。
    """
    try:
        cq = load_cadquery()
        metrics = read_step_metrics(cq, Path(state["artifact_paths"]["step"]))
        expected = state["feature_ir"]["expected_geometry"]
        bbox_errors = {
            name: abs(float(metrics["bounding_box"][name]) - float(value))
            for name, value in expected["bounding_box"].items()
        }
        exact_solid_count = expected.get("solid_count")
        model_source = str(state.get("model_source", "local_feature_ir"))
        if model_source == "web_exact":
            # 同一产品的原厂/第三方 CAD 可能按装配体拆成多个 solid，不应要求其
            # 拆分方式与本地 Builder 完全一致；外形尺寸和有效 B-Rep 仍必须满足。
            solid_count_matches = metrics["solid_count"] >= 1
            actual_sizes = sorted(_bounding_box_sizes(metrics["bounding_box"]).values())
            expected_sizes = sorted(_bounding_box_sizes(expected["bounding_box"]).values())
            geometry_errors = [
                abs(actual - target)
                for actual, target in zip(actual_sizes, expected_sizes)
            ]
        else:
            solid_count_matches = (
                metrics["solid_count"] == int(exact_solid_count)
                if exact_solid_count is not None
                else metrics["solid_count"] >= int(expected["minimum_solid_count"])
            )
            geometry_errors = list(bbox_errors.values())
        passed = (
            metrics["is_valid"]
            and solid_count_matches
            and metrics["face_count"] >= int(expected["minimum_face_count"])
            and max(geometry_errors, default=0.0) <= 0.05
        )
        verification = {
            "passed": passed,
            "is_valid": metrics["is_valid"],
            "solid_count": metrics["solid_count"],
            "face_count": metrics["face_count"],
            "edge_count": metrics["edge_count"],
            "volume_mm3": metrics["volume_mm3"],
            "bounding_box": metrics["bounding_box"],
            "bbox_errors_mm": bbox_errors,
            "geometry_errors_mm": geometry_errors,
            "model_source": model_source,
        }
        if not passed:
            return {"verification": verification, "status": "failed"}
        return {"verification": verification, "status": "step_verified"}
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


@cad_node
def render_views_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：从已回读候选 STEP 渲染等轴、前、俯、右四视图。

    Args:
        state: 包含已验证候选 STEP。

    Returns:
        ``preview_paths`` 和更新后的产物路径。

    失败状态:
        STEP 回读或渲染失败时返回 ``failed``。
    """
    try:
        cq = load_cadquery()
        step_path = Path(state["artifact_paths"]["step"])
        shape = read_step_metrics(cq, step_path)["shape"]
        previews: dict[str, str] = {}
        artifacts = dict(state.get("artifact_paths", {}))
        for view in ("isometric", "front", "top", "right"):
            path = step_path.with_name(f"{step_path.stem}_{view}.png")
            render_shape_to_png(shape, path, view=view)
            previews[view] = str(path)
            artifacts[f"preview_{view}"] = str(path)
        return {
            "preview_paths": previews,
            "artifact_paths": artifacts,
            "status": "views_rendered",
        }
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


def _metric_sizes(metrics: dict[str, Any]) -> dict[str, float]:
    bbox = metrics["bounding_box"]
    return {
        "x": bbox["xmax"] - bbox["xmin"],
        "y": bbox["ymax"] - bbox["ymin"],
        "z": bbox["zmax"] - bbox["zmin"],
    }


@cad_node
def compare_golden_reference_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：候选 STEP 和四视图完成后，独立读取原厂 STEP 做只读比较。

    Args:
        state: 必须已包含候选 STEP、验证结果和四视图；不接受 Golden 参数输入。

    Returns:
        包围盒、体积、solid/face/edge 和关键结构差异。

    失败状态:
        对比失败只返回 ``review_required``，绝不修改或重建 Feature IR。
    """
    try:
        step_path = Path(state.get("artifact_paths", {}).get("step", ""))
        previews = state.get("preview_paths", {})
        if (
            not state.get("verification", {}).get("passed")
            or not step_path.is_file()
            or not all(view in previews and Path(previews[view]).is_file() for view in (
                "isometric", "front", "top", "right"
            ))
        ):
            raise RuntimeError("候选 STEP 和四视图尚未完成，禁止读取 Golden Reference")
        family_id = str(
            state.get("family_id")
            or state.get("feature_ir", {}).get("family_id")
            or ""
        )
        reference_path = GOLDEN_REFERENCE_STEPS.get(family_id)
        if reference_path is None:
            comparison = {
                "status": "review_required",
                "reason": "golden_reference_not_configured",
                "family_id": family_id,
                "candidate_step": str(step_path),
                "previews": previews,
                "note": "该器件族未配置独立原厂 STEP；没有读取其他器件族参考文件。",
            }
            path = Path(state["output_dir"]) / "golden_comparison.json"
            artifacts = dict(state.get("artifact_paths", {}))
            artifacts["golden_comparison"] = _write_json(path, comparison)
            return {
                "golden_comparison": comparison,
                "artifact_paths": artifacts,
                "status": "golden_compared",
            }
        if not reference_path.is_file():
            raise FileNotFoundError(f"原厂 STEP 不存在：{reference_path}")
        cq = load_cadquery()
        candidate = read_step_metrics(cq, step_path)
        reference = read_step_metrics(cq, reference_path)
        candidate_sizes = _metric_sizes(candidate)
        reference_sizes = _metric_sizes(reference)
        bbox_errors = {
            axis: candidate_sizes[axis] - reference_sizes[axis]
            for axis in ("x", "y", "z")
        }
        feature_types = {
            feature["feature_type"]
            for feature in state.get("feature_ir", {}).get("features", [])
        }
        expected_types = GOLDEN_EXPECTED_FEATURE_TYPES.get(family_id, set())
        if family_id == "ic/gullwing_ic" and "molded_body_box" in feature_types:
            expected_types = (expected_types - {"drafted_body_loft"}) | {"molded_body_box"}
        missing_structures = (
            []
            if state.get("model_source") == "web_exact"
            else sorted(expected_types - feature_types)
        )
        exact_like = (
            max(abs(value) for value in bbox_errors.values()) <= 0.05
            and abs(candidate["volume_mm3"] - reference["volume_mm3"]) <= 0.05
            and candidate["solid_count"] == reference["solid_count"]
            and not missing_structures
        )
        comparison = {
            "status": "matched" if exact_like else "review_required",
            "reference_step": str(reference_path),
            "candidate": {
                "bounding_box_size_mm": candidate_sizes,
                "volume_mm3": candidate["volume_mm3"],
                "solid_count": candidate["solid_count"],
                "face_count": candidate["face_count"],
                "edge_count": candidate["edge_count"],
            },
            "reference": {
                "bounding_box_size_mm": reference_sizes,
                "volume_mm3": reference["volume_mm3"],
                "solid_count": reference["solid_count"],
                "face_count": reference["face_count"],
                "edge_count": reference["edge_count"],
            },
            "bbox_size_delta_mm": bbox_errors,
            "volume_delta_mm3": candidate["volume_mm3"] - reference["volume_mm3"],
            "solid_count_delta": candidate["solid_count"] - reference["solid_count"],
            "face_count_delta": candidate["face_count"] - reference["face_count"],
            "edge_count_delta": candidate["edge_count"] - reference["edge_count"],
            "missing_structures": missing_structures,
            "previews": state["preview_paths"],
            "note": "原厂 STEP 在候选 STEP 与四视图完成后首次读取；结果不会反馈建模链路。",
        }
        path = Path(state["output_dir"]) / "golden_comparison.json"
        artifacts = dict(state.get("artifact_paths", {}))
        artifacts["golden_comparison"] = _write_json(path, comparison)
        return {
            "golden_comparison": comparison,
            "artifact_paths": artifacts,
            "status": "golden_compared",
        }
    except Exception as exc:
        comparison = {"status": "review_required", "error": str(exc)}
        return {"golden_comparison": comparison, "status": "golden_compared"}


async def stop_insufficient_extraction_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：证据不足时在 Feature IR 前安全结束，禁止创建 STEP。

    Args:
        state: 包含 ``dimension_gate`` 和证据产物。

    Returns:
        用户要求的停止状态、缺失项、冲突项、低置信度项和证据报告。

    失败状态:
        本节点本身是预期安全终点，不返回技术失败。
    """
    gate = state.get("dimension_gate", {})
    classification = (
        state.get("fused_evidence")
        or state.get("view_classification")
        or {}
    )
    status = str(gate.get("status") or "stopped_insufficient_extraction")
    result = {
        "status": status,
        "category_id": classification.get("category_id", ""),
        "subcategory_id": classification.get("subcategory_id"),
        "family_id": classification.get("family_id", ""),
        "missing_fields": gate.get("missing_fields", []),
        "conflicting_fields": gate.get("conflicting_fields", []),
        "low_confidence_fields": gate.get("low_confidence_fields", []),
        "evidence_report": gate.get("evidence_report", "工程图证据不足。"),
        "step_file": None,
    }
    if status == "needs_human_follow_up":
        result["candidates"] = gate.get("candidates", [])
        result["follow_up_question"] = gate.get("follow_up_question", "")
    _write_json(Path(state["output_dir"]) / "result.json", result)
    return {"result": result, "status": status}


async def finalize_result_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：汇总候选 STEP、四视图、回读验证和 Golden 独立比较。

    Args:
        state: 包含完整建模与评测状态。

    Returns:
        对外 ``result`` JSON。

    失败状态:
        汇总写盘失败时返回 ``failed``。
    """
    try:
        classification = (
            state.get("feature_ir")
            or state.get("fused_evidence")
            or state.get("view_classification")
            or {}
        )
        result = {
            "status": state.get("golden_comparison", {}).get("status", "review_required"),
            "category_id": classification.get("category_id", ""),
            "subcategory_id": classification.get("subcategory_id"),
            "family_id": classification.get("family_id", ""),
            "step_file": state["artifact_paths"]["step"],
            "model_source": state.get("model_source", "local_feature_ir"),
            "jev_decision": state.get("jev_decision", {}),
            "reference_step_search": state.get("reference_step_search", {}),
            "previews": state.get("preview_paths", {}),
            "verification": state.get("verification", {}),
            "golden_comparison": state.get("golden_comparison", {}),
            "needs_review": bool(state.get("needs_review")),
            "modeling_assumptions": state.get("feature_ir", {}).get("assumptions", []),
        }
        _write_json(Path(state["output_dir"]) / "result.json", result)
        return {"result": result, "status": result["status"]}
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def failed_node(state: dict[str, Any]) -> dict[str, Any]:
    """作用：统一收敛技术失败，且绝不创建占位 STEP。

    Args:
        state: 包含上游积累的 ``errors``。

    Returns:
        ``status=failed`` 的对外结果。

    失败状态:
        本节点就是技术失败终点，不再继续建模。
    """
    classification = (
        state.get("fused_evidence")
        or state.get("view_classification")
        or {}
    )
    return {
        "result": {
            "status": "failed",
            "category_id": classification.get("category_id", ""),
            "subcategory_id": classification.get("subcategory_id"),
            "family_id": classification.get("family_id", ""),
            "errors": state.get("errors", []),
        },
        "status": "failed",
    }


def route_continue_or_failed(state: dict[str, Any]) -> str:
    """普通节点路由：技术失败进入 failed，否则继续。"""
    return "failed" if state.get("status") == "failed" else "continue"


def route_after_dimension_gate(state: dict[str, Any]) -> str:
    """尺寸门禁路由：证据不足时安全停止。"""
    gate = state.get("dimension_gate", {})
    if gate.get("passed"):
        return "continue"
    if gate.get("status") == "needs_human_follow_up":
        return "follow_up"
    if gate.get("status") == "stopped_unsupported_template":
        return "unsupported"
    from backend.agents.step.semantic_review_nodes import semantic_review_regions
    if semantic_review_regions(state):
        return "review"
    return "stop"


def route_after_view_result(state: dict[str, Any]) -> str:
    """逐视图循环路由：仍有视图则继续检索，否则进入确定性合并。"""
    if state.get("status") == "failed":
        return "failed"
    return "next" if state.get("pending_view_ids") else "merge"


def route_after_reference_search(state: dict[str, Any]) -> str:
    """Feature IR 后候选验证路由：兼容则准备，否则使用原 Builder。"""
    if state.get("status") == "failed":
        return "failed"
    selected = state.get("reference_step_search", {}).get("selected_candidate")
    return "reference" if selected else "local"


def route_after_early_reference_search(state: dict[str, Any]) -> str:
    """早期检索路由：精确产品进入回读，否则继续尺寸语义分析。"""
    if state.get("status") == "failed":
        return "failed"
    exact = state.get("reference_step_search", {}).get("early_exact_candidate")
    return "exact" if exact else "semantic"


def route_after_exact_reference_prepare(state: dict[str, Any]) -> str:
    """精确 STEP 准备路由：验证成功直接渲染，失败继续图纸建模链路。"""
    if state.get("status") == "failed":
        return "failed"
    return "render" if state.get("status") == "step_verified" else "semantic"


def route_after_reference_prepare(state: dict[str, Any]) -> str:
    """参考文件准备路由：成功进入回读验证，失败无损回退原 Builder。"""
    if state.get("status") == "failed":
        return "failed"
    return "verify" if state.get("status") == "reference_step_prepared" else "local"
