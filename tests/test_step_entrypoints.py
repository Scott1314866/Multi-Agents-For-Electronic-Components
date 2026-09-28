"""Backend entrypoint checks without starting a server or connecting to a DB."""

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
ENTRYPOINTS = ("backend.main", "backend.new_main", "backend.step_main")


def test_entrypoints_import_in_a_fresh_process_and_share_one_app():
    # Importing the ASGI app must not start lifespan/migrations, models, or a server.
    result = subprocess.run(
        [sys.executable, "-c", (
            "import backend.main as standard; import backend.new_main as legacy; "
            "import backend.step_main as step; "
            "assert standard.app is legacy.app is step.app; "
            "assert standard.lifespan is legacy.lifespan is step.lifespan; "
            "assert standard.run_server is legacy.run_server is step.run_server"
        )],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("module_name", ENTRYPOINTS)
def test_entrypoint_routes_contain_only_implemented_auth_step_and_health(module_name):
    module = importlib.import_module(module_name)
    canonical = importlib.import_module("backend.step_main")
    assert module.app is canonical.app
    assert module.lifespan is canonical.lifespan
    assert module.app.router.lifespan_context is canonical.app.router.lifespan_context
    paths = set(module.app.openapi()["paths"])
    assert "/health" in paths
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/auth/me" in paths
    assert "/api/v1/step/drawings" in paths
    assert "/api/v1/step/drawings/{drawing_id}/human-input" in paths
    assert all(
        path == "/health" or path.startswith(("/api/v1/auth/", "/api/v1/step/"))
        for path in paths
    )
    assert all(
        not any(fragment in path for fragment in ("/resume", "/interview", "/symbol"))
        for path in paths
    )


def test_router_catalog_matches_the_shared_application():
    router = importlib.import_module("backend.api.router").api_router
    canonical = importlib.import_module("backend.step_main")
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


def test_shared_app_lifespan_runs_migrations_and_recovery_once(monkeypatch):
    canonical = importlib.import_module("backend.step_main")
    events = []

    async def migrate():
        events.append("migrate")

    async def recover():
        events.append("recover")

    monkeypatch.setattr(canonical, "run_migrations", migrate)
    monkeypatch.setattr(canonical.step, "recover_step_jobs", recover)
    monkeypatch.setattr(canonical.step, "_background_tasks", set())

    async def enter():
        async with canonical.app.router.lifespan_context(canonical.app):
            events.append("running")

    asyncio.run(enter())
    assert events == ["migrate", "recover", "running"]


@pytest.mark.parametrize("module_name", ["backend.main", "backend.new_main"])
def test_compatibility_module_main_calls_shared_startup(monkeypatch, module_name):
    canonical = importlib.import_module("backend.step_main")
    calls = []
    monkeypatch.setattr(canonical, "run_server", lambda: calls.append("started"))
    path = PROJECT_ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
    namespace = runpy.run_path(str(path), run_name="__main__")
    assert calls == ["started"]
    assert namespace["app"] is canonical.app


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_shared_startup_preserves_selector_and_uvicorn_configuration(monkeypatch, platform):
    import uvicorn

    canonical = importlib.import_module("backend.step_main")
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

    assert server_config == [(canonical.app, {"host": "127.0.0.1", "port": 8000, "loop": "asyncio"})]
    expected = ["runner.enter", "serve", "runner.run", "runner.exit"] if platform == "win32" else ["server.run"]
    assert events == expected


def test_obsolete_resume_linked_api_sources_are_deleted():
    api_root = PROJECT_ROOT / "backend" / "api"
    assert not (api_root / "v1" / "symbol.py").exists()
    assert not (api_root / "v1" / "verify_interview_e2e.py").exists()
    removed_symbols = ("resume_review_id", "resume_reviews", "seed_resume", "backend.agents.interview")
    for source in api_root.rglob("*.py"):
        contents = source.read_text(encoding="utf-8")
        assert not any(symbol in contents for symbol in removed_symbols), source
