"""PostgreSQL 支撑的 LangGraph 持久化（PCB 封装工作流）。

与 :mod:`backend.agents.step.persistence` 同构，只是 thread 前缀与图构造器不同。
四件套：开图（含 saver.setup 幂等建表）、稳定 thread 配置、调用/续跑、
跨进程互斥锁。
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
async def open_postgres_pcb_graph(*, setup: bool = True) -> AsyncIterator[Any]:
    """打开一个 checkpoint 落在 PostgreSQL 的 PCB 封装图。

    saver 在自己的上下文里持有连接；``setup()`` 幂等地创建或迁移
    LangGraph 的 checkpoint 表。

    两个 import 刻意放在函数体内：既是延迟加载，也避开模块级循环导入。
    """
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from backend.agents.pcb.graph import build_pcb_graph

    async with AsyncPostgresSaver.from_conn_string(_postgres_dsn()) as saver:
        if setup:
            await saver.setup()
        yield build_pcb_graph(checkpointer=saver)


def pcb_checkpoint_config(pcb_drawing_id: str | UUID) -> dict:
    """按 ``pcb_drawings.id`` 生成稳定的 LangGraph thread 配置。

    用同一个 config 再次调用即续跑同一次运行；换一个 drawing 行就是一条
    隔离的 checkpoint 线程。
    """
    drawing_id = UUID(str(pcb_drawing_id))
    return {
        # 本图不算长（十余个节点），但人工停点会让步数翻倍，留足余量；
        # LangGraph 的默认 25 在带 HITL 的图上不够用。
        "recursion_limit": 128,
        "configurable": {
            "thread_id": f"pcb:{drawing_id}",
        },
    }


async def invoke_postgres_pcb_graph(pcb_drawing_id: str | UUID, input_state: Any) -> Any:
    """调用或续跑一个 PCB 封装工作流。"""
    config = pcb_checkpoint_config(pcb_drawing_id)
    async with open_postgres_pcb_graph() as graph:
        return await graph.ainvoke(input_state, config=config)


async def resume_postgres_pcb_graph_at_node(
    pcb_drawing_id: str | UUID,
    updates: dict[str, Any],
    *,
    as_node: str,
) -> Any:
    """从一个已 checkpoint 的节点续跑，并带上经过审计的更新。"""
    config = pcb_checkpoint_config(pcb_drawing_id)
    async with open_postgres_pcb_graph() as graph:
        await graph.aupdate_state(config, updates, as_node=as_node)
        return await graph.ainvoke(None, config=config)


def serialize_pcb_snapshot(snapshot: Any) -> dict[str, Any]:
    """把 checkpoint 摊平成纯 dict，不泄漏 LangGraph 对象。

    interrupt 属于 pending task，不在持久化的 values 字典里；两者都要保留，
    换一个进程恢复暂停任务时缺一不可。
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


async def get_postgres_pcb_snapshot(pcb_drawing_id: str | UUID) -> dict[str, Any]:
    """按任务的 thread 读取状态与待答问题。

    返回 ``values``、``checkpoint_id``、``next_nodes`` 与 ``interrupts``；
    每个 interrupt 形如 ``{"id": str, "value": dict}``。通过 API 暴露之前，
    调用方必须先校验该 drawing 的租户归属。
    """
    config = pcb_checkpoint_config(pcb_drawing_id)
    async with open_postgres_pcb_graph(setup=False) as graph:
        return serialize_pcb_snapshot(await graph.aget_state(config))


@asynccontextmanager
async def open_postgres_pcb_job_lock(
    pcb_drawing_id: str | UUID,
    *,
    wait: bool = False,
) -> AsyncIterator[bool]:
    """持有一个专属会话的 advisory lock，表示某个 worker 独占该任务。

    恢复流程会跳过被其他进程持有的任务；刚被接受的人工回答可以稍等前一个
    worker 把暂停点写完。连接**绝不入池**：关闭连接即释放锁，失败也安全。
    """
    from psycopg import AsyncConnection

    thread_id = pcb_checkpoint_config(pcb_drawing_id)["configurable"]["thread_id"]
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
