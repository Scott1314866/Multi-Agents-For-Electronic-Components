"""History lookups always bind task thread and checkpoint ID together."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import pytest

from backend.agents.symbol import persistence


def test_snapshot_lookup_rejects_unknown_checkpoint_even_with_requested_id(monkeypatch):
    job_id, checkpoint_id = uuid.uuid4(), uuid.uuid4()
    calls = []
    class Graph:
        async def aget_state(self, config):
            calls.append(config)
            return SimpleNamespace(config=config, values={}, created_at=None)
    @asynccontextmanager
    async def graph(**kwargs):
        yield Graph()
    monkeypatch.setattr(persistence, "open_postgres_symbol_graph", graph)
    with pytest.raises(LookupError):
        asyncio.run(persistence.get_postgres_symbol_snapshot(job_id, checkpoint_id))
    assert calls[0]["configurable"] == {"thread_id": f"symbol:{job_id}", "checkpoint_id": str(checkpoint_id)}


def test_history_cursor_and_limit_are_bound_to_same_thread(monkeypatch):
    job_id, checkpoint_id = uuid.uuid4(), uuid.uuid4()
    calls = []
    class Graph:
        async def aget_state_history(self, config, **kwargs):
            calls.append((config, kwargs))
            yield SimpleNamespace(config={"configurable": {"checkpoint_id": str(checkpoint_id)}},
                values={"status": "failed"}, created_at="2026-10-09T00:00:00Z", metadata={"step": 3},
                parent_config={"configurable": {"checkpoint_id": "parent"}}, tasks=[], next=[])
    @asynccontextmanager
    async def graph(**kwargs):
        yield Graph()
    monkeypatch.setattr(persistence, "open_postgres_symbol_graph", graph)
    result = asyncio.run(persistence.list_postgres_symbol_snapshots(job_id, limit=4, before=checkpoint_id))
    config, kwargs = calls[0]
    assert config["configurable"]["thread_id"] == kwargs["before"]["configurable"]["thread_id"] == f"symbol:{job_id}"
    assert kwargs["limit"] == 4 and kwargs["before"]["configurable"]["checkpoint_id"] == str(checkpoint_id)
    assert result[0]["metadata"]["step"] == 3 and result[0]["parent_checkpoint_id"] == "parent"
