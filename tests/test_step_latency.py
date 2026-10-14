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


def test_ocr_uses_explicit_local_weights_before_engine_import(monkeypatch, tmp_path):
    root=tmp_path/'paddlex'
    for name in ('det','rec'):
        directory=root/'official_models'/name;directory.mkdir(parents=True)
        (directory/'inference.pdiparams').write_bytes(b'weights')
        (directory/'inference.yml').write_text('model_name: fixture')
    settings=SimpleNamespace(step_ocr_model_dir=str(root),step_ocr_runtime_dir='',step_ocr_cpu_threads=4,
          step_ocr_detection_model='det',step_ocr_recognition_model='rec',step_ocr_enable_mkldnn=True)
    monkeypatch.setattr(ocr,'get_settings',lambda:settings)
    def create(**kwargs):
        import os
        assert os.environ['PADDLE_PDX_CACHE_HOME']==str(root)
        assert os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']=='True'
        assert kwargs['text_detection_model_dir']==str(root/'official_models/det')
        assert kwargs['text_recognition_model_dir']==str(root/'official_models/rec')
        return object()
    monkeypatch.setitem(sys.modules,'paddleocr',SimpleNamespace(PaddleOCR=create))
    ocr.create_paddle_engine.cache_clear()
    try:ocr.create_paddle_engine()
    finally:ocr.create_paddle_engine.cache_clear()


def test_missing_explicit_ocr_weights_fails_before_remote_download(monkeypatch,tmp_path):
    monkeypatch.setattr(ocr,'get_settings',lambda:SimpleNamespace(step_ocr_model_dir=str(tmp_path),
          step_ocr_detection_model='missing_det',step_ocr_recognition_model='missing_rec'))
    ocr.create_paddle_engine.cache_clear()
    try:
        with pytest.raises(RuntimeError,match='本地模型不完整'):ocr.create_paddle_engine()
    finally:ocr.create_paddle_engine.cache_clear()


def test_ocr_warmup_has_separate_deadline_and_runs_once_per_worker():
    calls=[]
    class Executor:
        def submit(self,function):
            assert function is ocr._warmup_in_process
            calls.append('warmup')
            return SimpleNamespace(result=lambda timeout:calls.append(timeout))
    executor=Executor();settings=SimpleNamespace(step_ocr_startup_timeout=300,step_ocr_scan_timeout=120)
    ocr._ensure_ocr_ready(executor,settings)
    ocr._ensure_ocr_ready(executor,settings)
    assert calls==['warmup',300]


def test_ocr_warmup_does_not_reuse_blank_prediction_cache(monkeypatch):
    calls=[]
    monkeypatch.setattr(ocr,'create_paddle_engine',lambda:object())
    monkeypatch.setattr(ocr,'_run_engine',lambda engine,image:calls.append(image.shape) or [])
    monkeypatch.setattr(ocr,'_cached_predictions',lambda *args:pytest.fail('A cache hit cannot warm native predictors'))
    ocr._warmup_in_process()
    assert calls==[(128,128,3)]


def test_ocr_warmup_timeout_resets_worker_before_any_image_scan(monkeypatch):
    executor=SimpleNamespace(submit=lambda function:SimpleNamespace(result=lambda timeout:(_ for _ in ()).throw(ocr.FutureTimeoutError())))
    reset=[];monkeypatch.setattr(ocr,'_reset_ocr_process',lambda value:reset.append(value))
    with pytest.raises(RuntimeError,match='OCR 初始化超过 300 秒'):
        ocr._ensure_ocr_ready(executor,SimpleNamespace(step_ocr_startup_timeout=300))
    assert reset==[executor] and not getattr(executor,'_ocr_warmup_done',False)


def test_isolated_ocr_keeps_parent_free_of_paddle_and_preserves_evidence(monkeypatch, tmp_path):
    image = tmp_path / "gray.png"
    cv2.imwrite(str(image), np.full((80, 120), 255, dtype=np.uint8))
    prediction = [([[10, 10], [40, 10], [40, 25], [10, 25]], "0.40", .99)]
    class Engine:
        def predict(self, image):
            return [{"dt_polys": [prediction[0][0]], "rec_texts": ["0.40"], "rec_scores": [.99]}]
    expected = ocr.extract_ocr_tokens(image, [], engine=Engine(), rotations=(0,))
    submitted = []
    class Executor:
        def submit(self, function, *args):
            submitted.append((function, args[0].shape if args else None))
            return SimpleNamespace(result=lambda timeout: prediction if args else None)
    monkeypatch.setattr(ocr, "get_settings", lambda: SimpleNamespace(step_ocr_isolate_process=True, step_ocr_scan_timeout=120))
    monkeypatch.setattr(ocr, "_ocr_process_executor", lambda: Executor())
    monkeypatch.setattr(ocr, "create_paddle_engine", lambda: pytest.fail("Paddle must not load in the API process"))
    actual = ocr.extract_ocr_tokens(image, [], rotations=(0,))
    assert [token.model_dump() for token in actual] == [token.model_dump() for token in expected]
    assert submitted == [(ocr._warmup_in_process, None), (ocr._predict_in_process, (80, 120, 3))]


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


def test_ocr_timeout_terminates_only_its_worker_and_resets_pool(monkeypatch, tmp_path):
    image = tmp_path / 'timeout.png'
    cv2.imwrite(str(image), np.full((80, 120), 255, dtype=np.uint8))
    calls = []
    class Process:
        alive = True
        def is_alive(self): return self.alive
        def terminate(self): calls.append('terminate'); self.alive = False
        def join(self, timeout): calls.append(('join', timeout))
    class Future:
        def result(self, timeout):
            assert timeout == .1
            raise ocr.FutureTimeoutError()
    class Executor:
        _ocr_warmup_done = True
        _processes = {123: Process()}
        def submit(self, *_): return Future()
        def shutdown(self, **kwargs): calls.append(('shutdown', kwargs))
    def executor(): return Executor()
    executor.cache_clear = lambda: calls.append('reset')
    monkeypatch.setattr(ocr, '_ocr_process_executor', executor)
    monkeypatch.setattr(ocr, 'get_settings', lambda: SimpleNamespace(step_ocr_isolate_process=True, step_ocr_scan_timeout=.1))
    with pytest.raises(RuntimeError, match='OCR 单轮识别超过 0.1 秒'):
        ocr.extract_ocr_tokens(image, [], rotations=(0,))
    assert calls == ['terminate', ('join', 2), ('shutdown', {'wait': False, 'cancel_futures': True}), 'reset']


def test_ocr_cache_preserves_evidence_and_invalidates_changed_image_model_and_corruption(monkeypatch, tmp_path):
    settings = SimpleNamespace(step_ocr_cache_dir=str(tmp_path/'cache'), step_ocr_runtime_dir='',
                               step_ocr_detection_model='det', step_ocr_recognition_model='rec',
                               step_ocr_enable_mkldnn=True, step_ocr_cpu_threads=4)
    monkeypatch.setattr(ocr, 'get_settings', lambda: settings)
    monkeypatch.setenv('PADDLE_PDX_CACHE_HOME', str(tmp_path/'models'))
    weights = tmp_path/'models/official_models/det/inference.pdiparams'
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b'weights')
    predictions = [([[2., 2.], [50., 2.], [50., 30.], [2., 30.]], '0.40', .99)]
    calls = []
    monkeypatch.setattr(ocr, 'create_paddle_engine', lambda: calls.append('engine') or object())
    monkeypatch.setattr(ocr, '_run_engine', lambda *args: predictions)
    image = np.full((80, 120, 3), 255, dtype=np.uint8)
    assert ocr._predict_in_process(image) == predictions
    assert ocr._predict_in_process(image) == predictions
    assert calls == ['engine']
    image[0, 0] = 0
    assert ocr._predict_in_process(image) == predictions
    weights.write_bytes(b'changed weights')
    assert ocr._predict_in_process(image) == predictions
    cache = ocr._prediction_cache_path(image, settings)
    cache.write_text('{"schema":1,"items":[[[],"fake",0.99]]}', encoding='utf-8')
    assert ocr._predict_in_process(image) == predictions
    assert calls == ['engine'] * 4
