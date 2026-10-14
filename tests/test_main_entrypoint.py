"""Unified backend entrypoint checks without binding ports or connecting to a DB."""

from __future__ import annotations

import importlib
import asyncio
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

from fastapi import FastAPI
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_main_import_in_a_fresh_process_does_not_start_services():
    # Importing the ASGI app must not start lifespan/migrations, models, or a server.
    result = subprocess.run(
        [sys.executable, "-s", "-c", (
            "import sys; import backend.main as main; "
            "assert callable(main.app.router.lifespan_context); "
            "assert callable(main.lifespan); "
            "assert callable(main.run_server); "
            "assert not any(name in sys.modules for name in ('cadquery', 'paddle', 'paddleocr'))"
        )],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_main_routes_contain_only_implemented_apis():
    """只挂已实现的接口 —— 别让 exam / interview / qa 的残骸复活。

    pcb（PCB 封装生成）与 symbol（OrCAD 符号生成）现已实现，因此列入允许
    前缀；而 resume / interview / exam / qa 仍然是被删掉的残骸模块。
    """
    module = importlib.import_module("backend.main")
    paths = set(module.app.openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/me" in paths
    assert "/api/v1/step/drawings" in paths
    assert "/api/v1/step/drawings/{drawing_id}/human-input" in paths
    assert "/api/v1/pcb/generate" in paths
    assert "/api/v1/symbol/generate" in paths
    assert all(
        path == "/health"
        or path.startswith(
            ("/api/v1/auth/", "/api/v1/step/", "/api/v1/pcb/", "/api/v1/symbol/", "/api/v1/workbench/")
        )
        for path in paths
    )
    assert all(
        not any(fragment in path for fragment in ("/resume", "/interview", "/exam", "/qa/"))
        for path in paths
    )


def test_router_catalog_matches_the_main_application():
    router = importlib.import_module("backend.api.router").api_router
    canonical = importlib.import_module("backend.main")
    catalog_app = FastAPI()
    catalog_app.include_router(router, prefix="/api/v1")
    router_routes = {
        (path, method)
        for path, operations in catalog_app.openapi()["paths"].items()
        for method in operations
    }
    app_routes = {
        (path, method)
        for path, operations in canonical.app.openapi()["paths"].items()
        if path.startswith("/api/v1/")
        for method in operations
    }
    assert router_routes == app_routes


def test_main_lifespan_runs_migrations_and_recovery_once(monkeypatch):
    canonical = importlib.import_module("backend.main")
    events = []

    async def migrate():
        events.append("migrate")

    async def recover():
        events.append("recover_step")

    async def recover_pcb():
        events.append("recover_pcb")

    async def recover_symbol():
        events.append("recover_symbol")

    monkeypatch.setattr(canonical, "run_migrations", migrate)
    monkeypatch.setattr(canonical.step, "recover_step_jobs", recover)
    monkeypatch.setattr(canonical.pcb, "recover_pcb_jobs", recover_pcb)
    monkeypatch.setattr(canonical.symbol, "recover_symbol_jobs", recover_symbol)
    monkeypatch.setattr(canonical.step, "_background_tasks", set())
    monkeypatch.setattr(canonical.pcb, "_background_tasks", set())
    monkeypatch.setattr(canonical.symbol, "_background_tasks", set())

    async def enter():
        async with canonical.app.router.lifespan_context(canonical.app):
            events.append("running")

    asyncio.run(enter())
    assert events == ["migrate", "recover_step", "recover_pcb", "recover_symbol", "running"]


def test_main_module_execution_launches_its_application(monkeypatch):
    import uvicorn

    configs = []

    class Server:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            configs.append(self.config)

        def run(self):
            configs.append(self.config)

    monkeypatch.setattr(uvicorn, "Server", Server)
    namespace = runpy.run_path(str(PROJECT_ROOT / "backend" / "main.py"), run_name="__main__")
    assert len(configs) == 1
    assert configs[0].app is namespace["app"]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_main_startup_preserves_selector_and_uvicorn_configuration(monkeypatch, platform):
    import uvicorn

    canonical = importlib.import_module("backend.main")
    events = []
    server_config = []
    serve_result = object()

    def config(app, **kwargs):
        server_config.append((app, kwargs))
        return SimpleNamespace(app=app, **kwargs)

    class Server:
        def __init__(self, value):
            assert value.app is canonical.app

        def serve(self):
            events.append("serve")
            return serve_result

        def run(self):
            events.append("server.run")

    class Runner:
        def __init__(self, *, loop_factory):
            assert loop_factory is canonical.asyncio.SelectorEventLoop

        def __enter__(self):
            events.append("runner.enter")
            return self

        def __exit__(self, *_args):
            events.append("runner.exit")

        def run(self, value):
            assert value is serve_result
            events.append("runner.run")

    monkeypatch.setattr(canonical.sys, "platform", platform)
    monkeypatch.setattr(uvicorn, "Config", config)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(canonical.asyncio, "Runner", Runner)
    canonical.run_server()

    assert server_config == [(canonical.app, {"host": "0.0.0.0", "port": 8000, "loop": "asyncio"})]
    expected = ["runner.enter", "serve", "runner.run", "runner.exit"] if platform == "win32" else ["server.run"]
    assert events == expected


def test_obsolete_resume_linked_api_sources_are_deleted():
    """exam / interview / resume 的残骸不得复活。

    ``api/v1/symbol.py`` 曾经是 interview（模拟面试）的副本 —— 它现在被
    **重写为 OrCAD 符号生成的接口**了，所以这里不再断言它不存在，改为断言
    它里面不含任何面试残留。
    """
    api_root = PROJECT_ROOT / "backend" / "api"
    assert not (api_root / "v1" / "verify_interview_e2e.py").exists()
    removed_symbols = ("resume_review_id", "resume_reviews", "seed_resume", "backend.agents.interview")
    for source in api_root.rglob("*.py"):
        contents = source.read_text(encoding="utf-8")
        assert not any(symbol in contents for symbol in removed_symbols), source

    # 同一个位置的 symbol.py 现在是合法实现，但必须确实是"符号生成"，
    # 而不是面试业务的换名副本。
    # （注意别去查裸 "resume" —— LangGraph 的 Command(resume=...) 与
    #   resume_input 都在正常用法里。）
    symbol_source = (api_root / "v1" / "symbol.py").read_text(encoding="utf-8")
    assert "backend.agents.symbol" in symbol_source
    assert "invoke_postgres_symbol_graph" in symbol_source
    assert "interview" not in symbol_source.lower()
    assert "start_session" not in symbol_source.lower()
