"""二维工程图 → Feature IR → STEP 工作流节点。"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from backend.agents.step.drawing import (
    DrawingExtraction,
    build_feature_model,
    create_feature_ir,
    image_as_data_url,
    load_golden_cases,
    validate_golden_extraction,
)
from backend.agents.step.prompts import (
    DRAWING_BLIND_EXTRACTION_USER_PROMPT,
    DRAWING_EXTRACTION_SYSTEM_PROMPT,
    DRAWING_EXTRACTION_USER_PROMPT,
)
from backend.agents.step.cad_utils import (
    load_cadquery,
    read_step_metrics,
    render_shape_to_png,
    safe_file_stem,
)
from backend.core.llm_factory import get_llm, get_structured_llm
from backend.core.logger import get_logger


logger = get_logger(__name__)


def _errors(state: dict[str, Any], message: str) -> list[str]:
    """向二维图纸状态追加错误。"""
    return [*state.get("errors", []), message]


def _response_text(response: Any) -> str:
    """从不同 LangChain 消息表示中读取纯文本响应。"""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def _parse_json_extraction(text: str) -> DrawingExtraction:
    """把模型的纯文本 JSON 严格解析为 DrawingExtraction。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Qwen 未返回 JSON object")
    return DrawingExtraction.model_validate(json.loads(cleaned[start:end + 1]))


def _cropped_image_data_url(path: Path, box: tuple[float, float, float, float]) -> str:
    """从同一工程图生成内存放大区域，不引入第二数据源。"""
    import base64
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        crop = image.crop((
            int(width * box[0]), int(height * box[1]),
            int(width * box[2]), int(height * box[3]),
        ))
        buffer = BytesIO()
        crop.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _merge_repaired_extraction(
    original: DrawingExtraction,
    repair: DrawingExtraction,
    issue_fields: set[str],
) -> DrawingExtraction:
    """只用复读结果替换门禁指出的问题字段，其余证据保持不变。"""
    payload = original.model_dump()
    dimensions = {item["canonical_name"]: item for item in payload["dimensions"]}
    for item in repair.model_dump()["dimensions"]:
        if item["canonical_name"] in issue_fields:
            dimensions[item["canonical_name"]] = item
    payload["dimensions"] = list(dimensions.values())
    payload["identified_features"] = list(dict.fromkeys([
        *payload.get("identified_features", []),
        *repair.identified_features,
    ]))
    repaired_names = set(dimensions)
    payload["unresolved_required_fields"] = [
        name for name in repair.unresolved_required_fields
        if name not in repaired_names
    ]
    payload["ambiguities"] = list(dict.fromkeys([
        *payload.get("ambiguities", []), *repair.ambiguities
    ]))
    payload["overall_confidence"] = min(
        float(payload.get("overall_confidence", 0.0)),
        float(repair.overall_confidence),
    )
    return DrawingExtraction.model_validate(payload)


async def extract_drawing_node(state: dict[str, Any]) -> dict[str, Any]:
    """调用视觉模型从二维图纸提取尺寸与 CAD 特征证据。

    Args:
        state: 包含 ``golden_case_id`` 和 ``drawing_path`` 的状态；参考图可选。

    Returns:
        结构化 ``drawing_extraction``；调用失败时返回失败状态。
    """
    try:
        cases = load_golden_cases()
        case_id = str(state["golden_case_id"])
        case = cases[case_id]
        drawing_path = Path(state.get("drawing_path") or case["drawing_path"])
        reference_value = state.get("reference_image_path") or case.get("reference_image_path")
        reference_path = Path(reference_value) if reference_value else None
        if not drawing_path.is_absolute():
            from backend.agents.step.drawing import PROJECT_ROOT
            drawing_path = PROJECT_ROOT / drawing_path
        if reference_path is not None and not reference_path.is_absolute():
            from backend.agents.step.drawing import PROJECT_ROOT
            reference_path = PROJECT_ROOT / reference_path
        if not drawing_path.is_file():
            raise FileNotFoundError(f"二维工程图不存在：{drawing_path}")
        if reference_path is not None and not reference_path.is_file():
            raise FileNotFoundError(f"3D 参考图不存在：{reference_path}")

        if state.get("drawing_extraction"):
            extraction = DrawingExtraction.model_validate(state["drawing_extraction"])
            return {
                "golden_case": case,
                "drawing_path": str(drawing_path),
                "reference_image_path": str(reference_path) if reference_path else "",
                "drawing_extraction": extraction.model_dump(),
                "status": "drawing_extracted",
            }

        blind_mode = bool(case.get("blind_mode", False))
        if blind_mode:
            # 严格盲测不得把 Golden case、器件族、目标字段或尺寸语义传给视觉模型。
            prompt = DRAWING_BLIND_EXTRACTION_USER_PROMPT
        else:
            required_names = ", ".join(case["required_dimensions"].keys())
            prompt = DRAWING_EXTRACTION_USER_PROMPT.format(
                case_id=case_id,
                part_type_hint=case["expected_part_type"],
                package_type_hint=case["expected_package_type"],
                required_names=required_names,
                dimension_guidance="\n".join(
                    f"- {item}" for item in case.get("dimension_guidance", ["按字段英文语义读取图纸。"])
                ),
            )
        llm = get_structured_llm("drawing_extract", DrawingExtraction)
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt},
            {"type": "text", "text": "下面这张二维工程图是唯一尺寸与外形依据。"},
            {"type": "image_url", "image_url": {"url": image_as_data_url(drawing_path)}},
        ]
        if reference_path is not None and not case.get("drawing_only_input", False):
            content.extend([
                {"type": "text", "text": "下面第二张只用于识别外观，不得从中估算尺寸。"},
                {"type": "image_url", "image_url": {"url": image_as_data_url(reference_path)}},
            ])
        messages = [
            SystemMessage(content=DRAWING_EXTRACTION_SYSTEM_PROMPT),
            HumanMessage(content=content),
        ]
        try:
            extraction = await llm.ainvoke(messages)
        except Exception as structured_exc:
            logger.warning(
                "step.drawing.extract.structured_retry",
                case_id=case_id,
                error_type=type(structured_exc).__name__,
            )
            retry_content = [
                {
                    "type": "text",
                    "text": (
                        prompt
                        + "\n上次响应不是 JSON object。现在只返回一个完整 JSON object；"
                        "不允许返回空数组，不允许添加 Markdown。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_as_data_url(drawing_path)}},
            ]
            raw_response = await get_llm("drawing_extract").ainvoke([
                SystemMessage(content=DRAWING_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=retry_content),
            ])
            extraction = _parse_json_extraction(_response_text(raw_response))

        first_gate = validate_golden_extraction(case, extraction.model_dump())
        if (
            not first_gate["passed"]
            and case.get("drawing_only_input", False)
            and not blind_mode
        ):
            issue_fields = {
                name.split(":", 1)[0]
                for name in [
                    *first_gate["missing_required_fields"],
                    *first_gate["conflicting_fields"],
                    *first_gate["low_confidence_fields"],
                ]
                if not name.startswith("feature:")
            }
            issue_labels = ", ".join(sorted(issue_fields))
            repair_prompt = (
                "请对同一张工程图做一次冲突字段复读，并仍返回完整 JSON object。"
                f"重点重新核对：{issue_labels or 'identified_features'}。\n"
                + "\n".join(case.get("dimension_guidance", []))
                + "\n不得使用常识或参考 STEP；找不到明确标注就加入 unresolved_required_fields。"
            )
            repair_response = await get_llm("drawing_extract").ainvoke([
                SystemMessage(content=DRAWING_EXTRACTION_SYSTEM_PROMPT),
                HumanMessage(content=[
                    {"type": "text", "text": repair_prompt},
                    {"type": "text", "text": "原始整图："},
                    {"type": "image_url", "image_url": {"url": image_as_data_url(drawing_path)}},
                    {"type": "text", "text": "左上主视图局部放大（来自同一文件）："},
                    {"type": "image_url", "image_url": {"url": _cropped_image_data_url(drawing_path, (0.04, 0.15, 0.58, 0.50))}},
                    {"type": "text", "text": "侧视图局部放大（来自同一文件）："},
                    {"type": "image_url", "image_url": {"url": _cropped_image_data_url(drawing_path, (0.36, 0.16, 0.58, 0.48))}},
                ]),
            ])
            try:
                repair = _parse_json_extraction(_response_text(repair_response))
                extraction = _merge_repaired_extraction(extraction, repair, issue_fields)
            except Exception as repair_exc:
                logger.warning(
                    "step.drawing.extract.repair_rejected",
                    case_id=case_id,
                    error_type=type(repair_exc).__name__,
                )
        logger.info("step.drawing.extract.done", case_id=case_id)
        return {
            "golden_case": case,
            "drawing_path": str(drawing_path),
            "reference_image_path": str(reference_path) if reference_path else "",
            "drawing_extraction": extraction.model_dump(),
            "status": "drawing_extracted",
        }
    except Exception as exc:
        logger.error("step.drawing.extract.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def validate_drawing_extraction_node(state: dict[str, Any]) -> dict[str, Any]:
    """执行工程图证据硬门禁，信息不足时停止建模。

    Args:
        state: 包含 Family 合同和视觉提取结果的状态。

    Returns:
        ``extraction_gate`` 以及可继续或停止的状态。
    """
    gate = validate_golden_extraction(
        state["golden_case"], state["drawing_extraction"]
    )
    return {
        "extraction_gate": gate,
        "status": "extraction_valid" if gate["passed"] else "insufficient_extraction",
    }


async def create_feature_ir_node(state: dict[str, Any]) -> dict[str, Any]:
    """把已通过门禁的工程图证据映射为白名单 Feature IR。

    Args:
        state: 包含 Golden 用例和图纸提取结果的状态。

    Returns:
        ``feature_ir`` 和规划状态。
    """
    try:
        feature_ir = create_feature_ir(
            state["golden_case"], state["drawing_extraction"]
        )
        return {"feature_ir": feature_ir, "status": "feature_ir_planned"}
    except Exception as exc:
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def build_drawing_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """执行 Feature IR 并导出含独立本体和引脚实体的 STEP。

    Args:
        state: 包含 ``feature_ir`` 和输出设置的状态。

    Returns:
        STEP 路径、CadQuery 版本和建模状态。
    """
    try:
        cq = load_cadquery()
        feature_ir = state["feature_ir"]
        model = build_feature_model(cq, feature_ir)
        output_dir = Path(state.get("output_dir") or "output/drawing_to_step").resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = safe_file_stem(state.get("output_name") or feature_ir["case_id"])
        step_path = output_dir / f"{stem}.step"
        cq.exporters.export(model, str(step_path), exportType="STEP")
        if not step_path.is_file() or step_path.stat().st_size == 0:
            raise RuntimeError("Feature IR 未生成有效 STEP")
        return {
            "artifact_paths": {"step": str(step_path)},
            "cadquery_version": str(getattr(cq, "__version__", "unknown")),
            "status": "drawing_step_built",
        }
    except Exception as exc:
        logger.error("step.drawing.build.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def verify_drawing_step_node(state: dict[str, Any]) -> dict[str, Any]:
    """回读 STEP，并独立核对实体数、面数和图纸关键包围盒。

    Args:
        state: 包含 STEP 路径和 Feature IR 期望几何的状态。

    Returns:
        ``drawing_verification`` 和验证状态。
    """
    try:
        cq = load_cadquery()
        metrics = read_step_metrics(cq, Path(state["artifact_paths"]["step"]))
        expected = state["feature_ir"]["expected_geometry"]
        bbox_errors = {
            name: abs(float(metrics["bounding_box"][name]) - float(value))
            for name, value in expected["bounding_box"].items()
        }
        if "solid_count" in expected:
            solids_ok = metrics["solid_count"] == expected["solid_count"]
        else:
            solids_ok = metrics["solid_count"] >= expected["minimum_solid_count"]
        passed = (
            solids_ok
            and metrics["face_count"] >= expected["minimum_face_count"]
            and max(bbox_errors.values(), default=0.0) <= 0.05
        )
        verification = {
            "passed": passed,
            "solid_count": metrics["solid_count"],
            "face_count": metrics["face_count"],
            "edge_count": metrics["edge_count"],
            "volume_mm3": metrics["volume_mm3"],
            "bounding_box": metrics["bounding_box"],
            "bbox_errors_mm": bbox_errors,
            "expected": expected,
        }
        return {
            "drawing_verification": verification,
            "status": "drawing_step_verified" if passed else "failed",
        }
    except Exception as exc:
        logger.error("step.drawing.verify.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def compare_golden_reference_node(state: dict[str, Any]) -> dict[str, Any]:
    """建模完成后读取原厂 STEP，并生成独立 Golden Reference 差异报告。

    此节点位于 STEP 生成和内核回读之后，原厂模型不会进入尺寸提取、Feature IR
    或建模节点，从流程拓扑上避免参考模型反向污染候选件。

    Args:
        state: 包含候选 STEP、Golden 用例和回读结果的状态。

    Returns:
        包围盒、体积和拓扑差异；没有参考 STEP 时返回跳过状态。
    """
    try:
        case = state["golden_case"]
        reference_value = case.get("golden_reference_step_path")
        if not reference_value:
            return {"golden_comparison": {"status": "not_configured"}, "status": "golden_compared"}
        from backend.agents.step.drawing import PROJECT_ROOT
        reference_path = Path(reference_value)
        if not reference_path.is_absolute():
            reference_path = PROJECT_ROOT / reference_path
        if not reference_path.is_file():
            raise FileNotFoundError(f"Golden Reference STEP 不存在：{reference_path}")
        cq = load_cadquery()
        candidate = read_step_metrics(cq, Path(state["artifact_paths"]["step"]))
        reference = read_step_metrics(cq, reference_path)

        def sizes(metrics: dict[str, Any]) -> dict[str, float]:
            bbox = metrics["bounding_box"]
            return {
                "x": bbox["xmax"] - bbox["xmin"],
                "y": bbox["ymax"] - bbox["ymin"],
                "z": bbox["zmax"] - bbox["zmin"],
            }

        candidate_sizes = sizes(candidate)
        reference_sizes = sizes(reference)
        size_errors = {
            axis: abs(candidate_sizes[axis] - reference_sizes[axis])
            for axis in ("x", "y", "z")
        }
        volume_ratio = (
            candidate["volume_mm3"] / reference["volume_mm3"]
            if reference["volume_mm3"] > 0 else 0.0
        )
        limits = case.get("golden_comparison", {})
        bbox_passed = max(size_errors.values()) <= float(
            limits.get("bbox_size_tolerance_mm", 0.5)
        )
        volume_passed = (
            float(limits.get("volume_ratio_min", 0.6))
            <= volume_ratio
            <= float(limits.get("volume_ratio_max", 1.4))
        )
        comparison = {
            "status": "matched" if bbox_passed and volume_passed else "review_required",
            "passed": bbox_passed and volume_passed,
            "reference_step": str(reference_path),
            "candidate": {
                "solid_count": candidate["solid_count"],
                "face_count": candidate["face_count"],
                "edge_count": candidate["edge_count"],
                "volume_mm3": candidate["volume_mm3"],
                "bounding_box_size_mm": candidate_sizes,
            },
            "reference": {
                "solid_count": reference["solid_count"],
                "face_count": reference["face_count"],
                "edge_count": reference["edge_count"],
                "volume_mm3": reference["volume_mm3"],
                "bounding_box_size_mm": reference_sizes,
            },
            "bbox_size_errors_mm": size_errors,
            "volume_ratio": volume_ratio,
            "bbox_passed": bbox_passed,
            "volume_passed": volume_passed,
            "note": "Golden Reference 仅在候选 STEP 已生成后读取，未参与尺寸提取或建模。",
        }
        return {"golden_comparison": comparison, "status": "golden_compared"}
    except Exception as exc:
        logger.error("step.drawing.golden_compare.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def render_drawing_views_node(state: dict[str, Any]) -> dict[str, Any]:
    """从 STEP 回读同一实体并生成等轴、前、俯、右四视图。

    Args:
        state: 包含已验证 STEP 路径的状态。

    Returns:
        四视图路径和候选件状态。
    """
    try:
        cq = load_cadquery()
        step_path = Path(state["artifact_paths"]["step"])
        shape = read_step_metrics(cq, step_path)["shape"]
        artifacts = dict(state.get("artifact_paths", {}))
        previews: dict[str, str] = {}
        for view in ("isometric", "front", "top", "right"):
            path = step_path.with_name(f"{step_path.stem}_{view}.png")
            render_shape_to_png(shape, path, view=view)
            previews[view] = str(path)
            artifacts[f"preview_{view}"] = str(path)
        return {
            "preview_paths": previews,
            "artifact_paths": artifacts,
            "status": "candidate_ready_for_review",
        }
    except Exception as exc:
        logger.error("step.drawing.preview.failed", error=str(exc), exc_info=True)
        return {"errors": _errors(state, str(exc)), "status": "failed"}


async def stop_insufficient_extraction_node(state: dict[str, Any]) -> dict[str, Any]:
    """在尺寸或特征证据不足时安全结束，不创建任何 CAD 文件。

    Args:
        state: 包含提取门禁结果的状态。

    Returns:
        明确的停止结果。
    """
    return {
        "result": {
            "case_id": state.get("golden_case_id"),
            "status": "stopped_insufficient_extraction",
            "step_file": None,
            "gate": state.get("extraction_gate", {}),
        },
        "status": "stopped_insufficient_extraction",
    }


async def finalize_drawing_result_node(state: dict[str, Any]) -> dict[str, Any]:
    """汇总 Golden Set 候选件和全部验证证据。

    Args:
        state: 已完成建模、回读和四视图渲染的状态。

    Returns:
        候选件结果；最终交付仍须与参考图人工比对。
    """
    return {
        "result": {
            "case_id": state.get("golden_case_id"),
            "status": "candidate_ready_for_review",
            "step_file": state.get("artifact_paths", {}).get("step"),
            "previews": state.get("preview_paths", {}),
            "extraction_gate": state.get("extraction_gate", {}),
            "verification": state.get("drawing_verification", {}),
            "golden_comparison": state.get("golden_comparison", {}),
            "feature_ir": state.get("feature_ir", {}),
            "note": "候选件必须与 Golden 参考图进行人工外形比对后才能标记为交付通过。",
        },
        "status": "candidate_ready_for_review",
    }


def route_after_drawing_extraction(state: dict[str, Any]) -> str:
    """根据视觉提取节点状态选择继续或失败。"""
    return "failed" if state.get("status") == "failed" else "validate"


def route_after_extraction_gate(state: dict[str, Any]) -> str:
    """提取充分时进入 Feature IR，否则安全停止。"""
    return "continue" if state.get("status") == "extraction_valid" else "stop"


def route_after_drawing_step(state: dict[str, Any]) -> str:
    """通用二维图纸节点失败路由。"""
    return "failed" if state.get("status") == "failed" else "continue"


async def failed_node(state: dict[str, Any]) -> dict[str, Any]:
    """统一收敛技术失败，且绝不创建占位 STEP。

    Args:
        state: 包含上游积累的 ``errors``。

    Returns:
        ``status=failed`` 的对外结果。
    """
    return {
        "result": {"status": "failed", "errors": state.get("errors", [])},
        "status": "failed",
    }
