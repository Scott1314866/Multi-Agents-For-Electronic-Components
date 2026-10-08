"""PostgreSQL 支撑的 LangGraph 持久化（符号生成工作流）。

与 :mod:`backend.agents.step.persistence` / :mod:`backend.agents.pcb.persistence`
同构，只是 thread 前缀与图构造器不同。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from typing import Any, AsyncIterator
from uuid import UUID

from sqlalchemy.engine import URL

from backend.config import get_settings


def _postgres_dsn() -> str:
    """由 .env.local 里的 PostgreSQL 设置拼一个 psycopg DSN。"""
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
async def open_postgres_symbol_graph(*, setup: bool = True) -> AsyncIterator[Any]:
    """打开一个 checkpoint 落在 PostgreSQL 的符号生成图。"""
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.agents.symbol.graph import build_symbol_graph

    async with AsyncPostgresSaver.from_conn_string(_postgres_dsn()) as saver:
        if setup:
            await saver.setup()
        yield build_symbol_graph(checkpointer=saver)


def symbol_checkpoint_config(symbol_drawing_id: str | UUID) -> dict:
    """按 ``symbol_drawings.id`` 生成稳定的 LangGraph thread 配置。"""
    drawing_id = UUID(str(symbol_drawing_id))
    return {
        # 本图节点多、人工停点密（五个），默认的 25 步远远不够。
        "recursion_limit": 256,
        "configurable": {
            "thread_id": f"symbol:{drawing_id}",
        },
    }


async def invoke_postgres_symbol_graph(
    symbol_drawing_id: str | UUID, input_state: Any
) -> Any:
    """调用或续跑一个符号生成工作流。"""
    config = symbol_checkpoint_config(symbol_drawing_id)
    async with open_postgres_symbol_graph() as graph:
        return await graph.ainvoke(input_state, config=config)


async def resume_postgres_symbol_graph_at_node(
    symbol_drawing_id: str | UUID,
    updates: dict[str, Any],
    *,
    as_node: str,
) -> Any:
    """从一个已 checkpoint 的节点续跑，并带上经过审计的更新。"""
    config = symbol_checkpoint_config(symbol_drawing_id)
    async with open_postgres_symbol_graph() as graph:
        await graph.aupdate_state(config, updates, as_node=as_node)
        return await graph.ainvoke(None, config=config)


def serialize_symbol_snapshot(snapshot: Any) -> dict[str, Any]:
    """把 checkpoint 摊平成纯 dict，不泄漏 LangGraph 对象。

    interrupt 属于 pending task，不在持久化的 values 字典里；两者都要保留。
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


async def get_postgres_symbol_snapshot(symbol_drawing_id: str | UUID) -> dict[str, Any]:
    """按任务的 thread 读取状态与待答问题。"""
    config = symbol_checkpoint_config(symbol_drawing_id)
    async with open_postgres_symbol_graph(setup=False) as graph:
        return serialize_symbol_snapshot(await graph.aget_state(config))


@asynccontextmanager
async def open_postgres_symbol_job_lock(
    symbol_drawing_id: str | UUID,
    *,
    wait: bool = False,
) -> AsyncIterator[bool]:
    """持有一个专属会话的 advisory lock，表示某个 worker 独占该任务。

    连接**绝不入池**：关闭连接即释放锁，失败也安全。
    """
    from psycopg import AsyncConnection

    thread_id = symbol_checkpoint_config(symbol_drawing_id)["configurable"]["thread_id"]
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
