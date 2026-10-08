"""Bounded model output, optional lookup and native preprocessing contracts."""
import asyncio
from types import SimpleNamespace
import sys

import cv2
import numpy as np
import pytest

from backend.agents.step import image_nodes
from backend.agents.step.vision import ocr, preprocess


@pytest.mark.parametrize("sdk_error", [False, True])
def test_truncated_json_retries_once_and_keeps_same_evidence(monkeypatch, sdk_error):
    calls = []
    class FakeLengthError(Exception):
        pass
    class FakeModel:
        def bind(self, **kwargs):
            budget = kwargs["max_tokens"]
            async def invoke(messages):
                calls.append((budget, messages))
                if budget == 4096 and sdk_error:
                    raise FakeLengthError()
                return SimpleNamespace(content='{"complete":true}', response_metadata={"finish_reason": "length" if budget == 4096 else "stop"})
            return SimpleNamespace(ainvoke=invoke)
    monkeypatch.setattr(image_nodes, "get_llm", lambda *args: FakeModel())
    monkeypatch.setattr(image_nodes, "LengthFinishReasonError", FakeLengthError)
    result = asyncio.run(image_nodes._invoke_qwen("system", "same-input-token-id"))
    assert result == '{"complete":true}'
    assert [x[0] for x in calls] == [4096, 8192]
    assert calls[0][1] == calls[1][1]


def test_repeated_truncation_fails_without_accepting_partial_json(monkeypatch):
    calls = []
    class FakeModel:
        def bind(self, **kwargs):
            async def invoke(messages):
                calls.append(kwargs["max_tokens"])
                return SimpleNamespace(content='{"partial":', response_metadata={"finish_reason": "length"})
            return SimpleNamespace(ainvoke=invoke)
    monkeypatch.setattr(image_nodes, "get_llm", lambda *args: FakeModel())
    with pytest.raises(RuntimeError, match="输出仍不完整"):
        asyncio.run(image_nodes._invoke_qwen("system", "evidence"))
    assert calls == [4096, 8192]


def test_other_model_errors_are_not_retried(monkeypatch):
    calls = []
    class FakeModel:
        def bind(self, **kwargs):
            async def invoke(messages):
                calls.append(True)
                raise ConnectionError("offline")
            return SimpleNamespace(ainvoke=invoke)
    monkeypatch.setattr(image_nodes, "get_llm", lambda *args: FakeModel())
    with pytest.raises(ConnectionError):
        asyncio.run(image_nodes._invoke_qwen("system", "evidence"))
    assert len(calls) == 1


def test_reference_timeout_falls_back_to_local_evidence(monkeypatch, tmp_path):
    cancelled = []
    async def slow(**kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.append(True)
    monkeypatch.setattr(image_nodes, "search_public_step_candidates", slow)
    monkeypatch.setattr(image_nodes, "REFERENCE_SEARCH_TIMEOUT_SECONDS", 0.01)
    result = asyncio.run(image_nodes.search_reference_step_node({"output_dir": str(tmp_path)}))
    assert cancelled and result["status"] == "reference_step_searched"
    assert result["reference_step_search"]["status"] == "timeout"
    assert result["reference_step_search"]["candidates"] == []
    assert image_nodes.route_after_early_reference_search(result) == "semantic"


def test_ocr_engine_is_reused_without_loading_real_models(monkeypatch):
    builds = []
    def create(**kwargs):
        builds.append(kwargs)
        return object()
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=create))
    ocr.create_paddle_engine.cache_clear()
    try:
        assert ocr.create_paddle_engine() is ocr.create_paddle_engine()
        assert len(builds) == 1
    finally:
        ocr.create_paddle_engine.cache_clear()


def test_vectorized_denoising_preserves_every_pixel_of_original_rule():
    binary = np.where(np.random.default_rng(123).random((160, 200)) > .88, 0, 255).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats((binary < 128).astype(np.uint8), 8)
    expected = np.full(binary.shape, 255, np.uint8)
    kept = 0
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= 3:
            expected[labels == label] = 0
            kept += 1
    actual, actual_kept = preprocess._remove_tiny_components(binary)
    np.testing.assert_array_equal(actual, expected)
    assert actual_kept == kept
