"""DIP__2 二值工程图到 STEP 的真实端到端回归测试。

该测试会调用 PaddleOCR、Qwen、Jev、Web Search 和 CadQuery，因此默认跳过。
在 ``ima-agent`` 环境中设置 ``RUN_STEP_IMAGE_LIVE_REGRESSION=1`` 后执行。
唯一业务输入是用户指定的 ``preprocessed_binary.png``，测试不提供任何人工尺寸。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import uuid

import pytest

from backend.agents.step.graph import build_image_to_step_graph


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIP_BINARY_DRAWING = (
    PROJECT_ROOT
    / "output"
    / "drawing_to_step"
    / "image_agent"
    / "DIP__2"
    / "vision"
    / "preprocessed_binary.png"
)

EXPECTED_FEATURE_TYPES = {
    "dsub_shell_frame",
    "dsub_rear_housing",
    "dsub_right_angle_contacts",
    "dsub_mounting_hardware",
}


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RUN_STEP_IMAGE_LIVE_REGRESSION") != "1",
    reason="设置 RUN_STEP_IMAGE_LIVE_REGRESSION=1 才运行真实模型回归测试",
)
def test_dip_binary_drawing_routes_and_builds_verified_dsub_step():
    """只输入 DIP 二值图，验证 Jev 路由、证据门禁、Feature IR 和 STEP。

    Args:
        无。输入路径固定为用户指定的 DIP__2 二值工程图。

    Returns:
        无；所有阶段通过 pytest 断言验证。

    失败状态:
        图片缺失、Jev 未路由到连接器、尺寸证据不足、Feature IR 缺结构、
        STEP 未生成或 OpenCascade 回读失败时测试失败。
    """
    assert DIP_BINARY_DRAWING.is_file(), f"回归图片不存在：{DIP_BINARY_DRAWING}"
    source_sha256 = hashlib.sha256(DIP_BINARY_DRAWING.read_bytes()).hexdigest()
    graph = build_image_to_step_graph()

    state = asyncio.run(graph.ainvoke(
        {"image_path": str(DIP_BINARY_DRAWING)},
        config={
            "configurable": {
                "thread_id": f"dip-binary-regression-{uuid.uuid4().hex[:10]}"
            }
        },
    ))

    assert state.get("status") not in {
        "failed",
        "stopped_insufficient_extraction",
    }, state.get("result")

    jev = state["jev_decision"]
    assert jev["decision"]["choice"] == "connector"
    assert jev["gate"]["auto_route"] is True
    assert jev["resolved_family_id"] == "connector/cn/dsub_connector"
    assert jev["resolved_category_id"] == "connector"
    assert jev["resolved_subcategory_id"] == "cn"

    assert state["dimension_gate"]["passed"] is True
    assert state["family_id"] == "connector/cn/dsub_connector"

    feature_ir = state["feature_ir"]
    assert feature_ir["source_image_sha256"] == source_sha256
    assert feature_ir["category_id"] == "connector"
    assert feature_ir["subcategory_id"] == "cn"
    assert feature_ir["family_id"] == "connector/cn/dsub_connector"
    feature_types = {item["feature_type"] for item in feature_ir["features"]}
    assert EXPECTED_FEATURE_TYPES.issubset(feature_types)
    assert all(
        parameter["evidence_ids"]
        for parameter in feature_ir["source_dimensions"].values()
    )

    step_path = Path(state["artifact_paths"]["step"])
    assert step_path.is_file() and step_path.stat().st_size > 0
    assert state["verification"]["passed"] is True
    assert state["verification"]["solid_count"] >= 16
    assert state["verification"]["face_count"] >= 50
    assert set(state["preview_paths"]) == {"isometric", "front", "top", "right"}
    assert all(Path(path).is_file() for path in state["preview_paths"].values())
