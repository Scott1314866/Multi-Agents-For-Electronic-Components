"""PostgreSQL-backed LangGraph persistence for STEP workflows."""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy.engine import URL

from backend.config import get_settings
from backend.agents.step.progress import invoke_with_progress


def _postgres_dsn() -> str:
    """Build a psycopg DSN from the existing .env.local PostgreSQL settings."""
    settings = get_settings()
    return URL.create(
        drivername="postgresql",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        query={"sslmode": "disable"},
    ).render_as_string(hide_password=False)


@asynccontextmanager
async def open_postgres_step_graph(
    mode: str = "image",
    *,
    setup: bool = True,
) -> AsyncIterator[Any]:
    """Open a STEP graph whose LangGraph checkpoints live in PostgreSQL.

    The saver owns its connection for the lifetime of the context. Its ``setup``
    method creates or migrates LangGraph's checkpoint tables idempotently.
    """
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.agents.step.graph import select_graph_mode

    async with AsyncPostgresSaver.from_conn_string(_postgres_dsn()) as saver:
        if setup:
            await saver.setup()
        yield select_graph_mode(mode, checkpointer=saver)


def step_checkpoint_config(mode: str, step_drawing_id: str | UUID) -> dict:
    """Return a stable LangGraph thread config keyed by ``step_drawings.id``.

    Reusing this config for later invocations resumes the same run. A distinct
    drawing row or workflow mode receives an isolated checkpoint thread.
    """
    if mode not in {"image", "drawing"}:
        raise ValueError(f"不支持的 STEP Agent 模式：{mode}")
    drawing_id = UUID(str(step_drawing_id))
    return {
        # View evidence uses a bounded per-view loop; 25 steps is insufficient
        # for ordinary drawings once human checkpoints are part of the graph.
        "recursion_limit": 256,
        "configurable": {
            "thread_id": f"step:{mode}:{drawing_id}",
        }
    }


async def invoke_postgres_step_graph(
    mode: str,
    step_drawing_id: str | UUID,
    input_state: Any,
) -> Any:
    """Invoke or resume a STEP workflow using the matching database thread."""
    config = step_checkpoint_config(mode, step_drawing_id)
    async with open_postgres_step_graph(mode) as graph:
        return await invoke_with_progress(graph, input_state, config)


async def resume_postgres_step_graph_at_node(
    mode: str,
    step_drawing_id: str | UUID,
    updates: dict[str, Any],
    *,
    as_node: str,
) -> Any:
    """Resume an existing thread from a checkpointed node with audited updates."""
    config = step_checkpoint_config(mode, step_drawing_id)
    async with open_postgres_step_graph(mode) as graph:
        await graph.aupdate_state(config, updates, as_node=as_node)
        return await invoke_with_progress(graph, None, config)


def serialize_step_snapshot(snapshot: Any) -> dict[str, Any]:
    """Expose stable checkpoint metadata without leaking LangGraph objects.

    Interrupts belong to pending tasks, not to the persisted values dictionary.
    Keeping both is essential when a different process resumes a paused job.
    """
    configurable = (getattr(snapshot, "config", None) or {}).get("configurable", {})
    interrupts = []
    seen_ids: set[str] = set()
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            interrupt_id = str(item.id)
            if interrupt_id not in seen_ids:
                interrupts.append({"id": interrupt_id, "value": item.value})
                seen_ids.add(interrupt_id)
    return {
        "values": dict(getattr(snapshot, "values", None) or {}),
        "checkpoint_id": configurable.get("checkpoint_id"),
        "next_nodes": list(getattr(snapshot, "next", ()) or ()),
        "interrupts": interrupts,
    }


async def get_postgres_step_snapshot(
    mode: str,
    step_drawing_id: str | UUID,
) -> dict[str, Any]:
    """Read state and pending questions from PostgreSQL using the job's thread.

    Returns ``values``, ``checkpoint_id``, ``next_nodes`` and ``interrupts``;
    each interrupt is ``{"id": str, "value": dict}``. Callers exposing this
    through an API must verify the drawing's tenant before calling this helper.
    """
    config = step_checkpoint_config(mode, step_drawing_id)
    async with open_postgres_step_graph(mode, setup=False) as graph:
        return serialize_step_snapshot(await graph.aget_state(config))


@asynccontextmanager
async def open_postgres_step_job_lock(
    step_drawing_id: str | UUID,
    *,
    wait: bool = False,
) -> AsyncIterator[bool]:
    """Hold a dedicated-session advisory lock while one worker owns a job.

    Recovery skips jobs owned by another process. A newly accepted answer may
    wait briefly for the previous worker to finish persisting its pause. The
    connection is never pooled: closing it also releases the lock on failure.
    """
    from psycopg import AsyncConnection

    thread_id = step_checkpoint_config("image", step_drawing_id)["configurable"]["thread_id"]
    async with await AsyncConnection.connect(_postgres_dsn(), autocommit=True) as conn:
        async with conn.cursor() as cursor:
            if wait:
                await cursor.execute(
                    "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (thread_id,)
                )
                acquired = True
            else:
                await cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (thread_id,)
                )
                acquired = bool((await cursor.fetchone())[0])
        try:
            yield acquired
        finally:
            if acquired:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (thread_id,)
                    )
