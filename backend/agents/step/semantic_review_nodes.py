"""有界的尺寸语义复核；仅重绑既有证据，不提供尺寸答案。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from backend.agents.step.families.registry import image_family_catalog
from backend.agents.step.prompts import IMAGE_SEMANTIC_REVIEW_SYSTEM_PROMPT
from backend.agents.step.vision.schemas import QwenViewSemanticResult, VisualEvidenceBundle
from backend.agents.step.vision.semantic_review import validate_review_candidate


MAX_SEMANTIC_REVIEW_ATTEMPTS = 1


def semantic_review_regions(state: dict[str, Any]) -> list[str]:
    """优先只复核有复用冲突的视图，其余视图的正确候选保持原样。"""
    from backend.agents.step.image_nodes import _is_geometry_view, _view_family_contract

    gate = state.get("dimension_gate") or {}
    if gate.get("passed") or gate.get("status") != "stopped_insufficient_extraction":
        return []
    if int(state.get("semantic_review_attempts", 0)) >= MAX_SEMANTIC_REVIEW_ATTEMPTS:
        return []
    if not state.get("visual_evidence") or not state.get("per_view_semantics"):
        return []
    classification = state.get("view_classification") or {}
    contract = image_family_catalog().get(classification.get("family_id"))
    if not contract:
        return []
    collision_regions = {
        region_id for item in state.get("semantic_collisions", [])
        for region_id in item.get("region_ids", [])
    }
    fields = set(gate.get("conflicting_fields", []))
    missing = set(gate.get("missing_fields", [])) | set(gate.get("low_confidence_fields", []))
    regions = []
    for item in state["per_view_semantics"]:
        region_id = item.get("region_id")
        view_type = classification.get("identified_views", {}).get(region_id, "")
        if not _is_geometry_view(view_type):
            continue
        if collision_regions:
            include = region_id in collision_regions
        else:
            scoped = _view_family_contract(contract, view_type)
            available = set(scoped.get("required_parameters", []))
            available.update(scoped.get("optional_parameters", []))
            names = {assignment.get("canonical_name") for assignment in item.get("assignments", [])}
            include = bool((fields & names) or (missing & available))
        if include and region_id not in regions:
            regions.append(region_id)
    return regions


def _complete_review_evidence(payload: dict, evidence: VisualEvidenceBundle, region_id: str) -> dict:
    """补齐当前区域已提取的 token/线，避免稀疏检索漏掉范围分隔线。

    全部内容来自当前任务的 immutable 视觉证据；不改原 ID、坐标或归属。
    其他区域的引用从复核白名单中排除，候选必须以当前区域证据自洽。
    """
    tokens = [token.model_dump() for token in evidence.ocr_tokens if token.source_region_id == region_id]
    lines = [line.model_dump() for line in evidence.lines if line.source_region_id == region_id]
    token_ids = {item["token_id"] for item in tokens}
    line_ids = {item["line_id"] for item in lines}
    groups = []
    for group in payload.get("dimension_groups", []):
        retained_tokens = [item for item in group.get("token_ids", []) if item in token_ids]
        if not retained_tokens:
            continue
        groups.append({
            **group, "token_ids": retained_tokens,
            **{
                key: [item for item in group.get(key, []) if item in token_ids]
                for key in ("row_label_token_ids", "context_token_ids")
            },
            **{
                key: [item for item in group.get(key, []) if item in line_ids]
                for key in ("dimension_line_ids", "extension_line_ids")
            },
            "table_columns": {key: item for key, item in group.get("table_columns", {}).items() if item in token_ids},
        })
    return {
        **payload,
        "coordinate_system": "原预处理图坐标；裁剪图原点为 region.bbox 左上角，未缩放",
        "dimension_groups": groups,
        "ocr_tokens": tokens,
        "lines": lines,
    }


async def prepare_semantic_review_node(state: dict[str, Any]) -> dict[str, Any]:
    """先持久化本轮预算与区域队列，再进入可能调用外部模型的节点。"""
    from backend.agents.step import image_nodes

    regions = semantic_review_regions(state)
    if not regions:
        return {"semantic_review_pending_regions": []}
    attempt = int(state.get("semantic_review_attempts", 0)) + 1
    before = {
        key: state.get(key) for key in (
            "per_view_semantics", "semantic_result", "fused_evidence",
            "dimension_gate", "dimension_chain_result", "semantic_collisions",
        )
    }
    output_dir = Path(state["output_dir"])
    artifacts = dict(state.get("artifact_paths", {}))
    artifacts[f"semantic_before_review_{attempt}"] = image_nodes._write_json(
        output_dir / f"semantic_before_review_{attempt}.json", before,
    )
    return {
        "semantic_review_attempts": attempt,
        "semantic_review_pending_regions": regions,
        "artifact_paths": artifacts,
        "status": "semantic_review_prepared",
    }


async def review_semantics_node(state: dict[str, Any]) -> dict[str, Any]:
    """每个 superstep 复核一个区域，完成后先存 checkpoint 再处理下一视图。

    非法候选整视图保留旧结果，合法候选仍重过原门禁。外部调用发生后、
    当前 superstep 提交前崩溃时可能重放当前区域，已完成区域不会重跑。
    """
    from backend.agents.step import image_nodes

    pending = list(state.get("semantic_review_pending_regions", []))
    attempt = int(state.get("semantic_review_attempts", 0))
    if not pending or not 1 <= attempt <= MAX_SEMANTIC_REVIEW_ATTEMPTS:
        return {"semantic_review_pending_regions": [], "status": "semantics_reviewed"}
    output_dir = Path(state["output_dir"])
    artifacts = dict(state.get("artifact_paths", {}))
    updated = [dict(item) for item in state["per_view_semantics"]]
    classification = state["view_classification"]
    full_contract = image_family_catalog()[classification["family_id"]]
    evidence = VisualEvidenceBundle.model_validate(state["visual_evidence"])
    audit: dict[str, Any] = {"attempt": attempt, "gate": state["dimension_gate"], "regions": []}
    for region_id in pending[:1]:
        original = next(item for item in updated if item["region_id"] == region_id)
        record: dict[str, Any] = {
            "region_id": region_id, "before": original, "accepted": False,
            "request_id": uuid4().hex,
        }
        request_path = output_dir / f"semantic_review_{attempt}_{record['request_id']}.json"
        audit["regions"].append(record)
        try:
            context = await image_nodes.retrieve_view_evidence_node({**state, "pending_view_ids": [region_id]})
            if context.get("status") == "failed":
                record["issues"] = ["当前视图证据不可读取，保留原候选"]
                continue
            view_type = classification["identified_views"][region_id]
            contract = image_nodes._view_family_contract(full_contract, view_type)
            allowed = set(contract.get("required_parameters", [])) | set(contract.get("optional_parameters", []))
            payload = _complete_review_evidence(context["current_prompt_evidence"], evidence, region_id)
            record["evidence_token_ids"] = [item["token_id"] for item in payload["ocr_tokens"]]
            record["evidence_line_ids"] = [item["line_id"] for item in payload["lines"]]
            request = {
                "region_id": region_id, "view_type": view_type,
                "family_id": classification["family_id"], "family_contract": contract,
                "gate_feedback": state["dimension_gate"],
                "semantic_collisions": state.get("semantic_collisions", []),
                "previous_unverified_result": original,
                "other_view_context_only": [
                    {"region_id": item["region_id"], "assignments": item.get("assignments", [])}
                    for item in state["per_view_semantics"] if item["region_id"] != region_id
                ],
                "current_view_evidence": payload,
            }
            image_nodes._write_json(request_path, record)
            response = await image_nodes._invoke_qwen(
                IMAGE_SEMANTIC_REVIEW_SYSTEM_PROMPT,
                json.dumps(request, ensure_ascii=False),
                image_path=Path(context["current_view_image_path"]),
            )
            record["raw_response"] = response
            image_nodes._write_json(request_path, record)
            candidate = image_nodes._parse_model_json(response, QwenViewSemanticResult)
            record["candidate"] = candidate.model_dump()
            issues = validate_review_candidate(candidate, payload, allowed, region_id, view_type)
            record["issues"] = issues
            if issues:
                continue
            updated = [candidate.model_dump() if item["region_id"] == region_id else item for item in updated]
            record["accepted"] = True
        except Exception as exc:
            # 模型/结构错误不抹掉首次证据，也不把失败伪装成已解决。
            record["issues"] = [f"复核未完成（{type(exc).__name__}），保留原候选"]
        finally:
            cancelling = isinstance(sys.exception(), asyncio.CancelledError)
            try:
                artifacts[f"semantic_review_request_{record['request_id']}"] = image_nodes._write_json(
                    request_path, record,
                )
            except OSError:
                # 保留取消信号，让 API 标记本 superstep 可恢复；不能让第二个
                # 审计 I/O 错误把正常关闭伪装成不可恢复的图异常。
                if not cancelling:
                    raise
    history = [*state.get("semantic_review_history", []), audit]
    artifacts[f"semantic_review_{attempt}"] = image_nodes._write_json(
        output_dir / f"semantic_review_{attempt}.json", {
            "attempt": attempt, "gate": state["dimension_gate"],
            "regions": [record for entry in history if entry["attempt"] == attempt for record in entry["regions"]],
        },
    )
    return {
        "per_view_semantics": updated,
        "semantic_review_attempts": attempt,
        "semantic_review_pending_regions": pending[1:],
        "semantic_review_history": history,
        "artifact_paths": artifacts,
        "status": "semantic_review_in_progress" if pending[1:] else "semantics_reviewed",
    }


def route_after_semantic_review(state: dict[str, Any]) -> str:
    return "next" if state.get("semantic_review_pending_regions") else "merge"
