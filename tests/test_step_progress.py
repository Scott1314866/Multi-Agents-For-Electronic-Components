"""Real LangGraph task events, without OCR/model calls or database access."""

import asyncio
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from backend.agents.step.progress import invoke_with_progress, progress_scope


class State(TypedDict, total=False):
    status: str
    all_ocr_tokens: list
    all_line_segments: list
    secret: str


def test_progress_starts_before_work_and_records_real_counts():
    async def run():
        events = []
        async def sink(event):
            events.append(event)
        async def extract(state):
            await asyncio.sleep(0.02)
            assert any(e["phase"] == "started" for e in events)
            return {"status": "ok", "all_ocr_tokens": [1, 2], "all_line_segments": [3], "secret": "private-path-api-key"}
        builder = StateGraph(State)
        builder.add_node("extract_all_evidence", extract)
        builder.add_edge(START, "extract_all_evidence")
        builder.add_edge("extract_all_evidence", END)
        with progress_scope(sink):
            output = await invoke_with_progress(builder.compile(), {}, {})
        assert output["status"] == "ok"
        assert [e["phase"] for e in events] == ["started", "completed"]
        assert "2 条文字，1 条线段" in events[-1]["message"]
        assert events[-1]["duration_seconds"] >= 0
        assert "private-path-api-key" not in str(events)
        assert len({e["id"] for e in events}) == len(events)
    asyncio.run(run())


def test_interrupt_and_resume_keep_checkpoint_and_public_waiting_log():
    async def run():
        events = []
        async def sink(event):
            events.append(event)
        def ask(state):
            interrupt({"secret": "private-question"})
            return {"status": "ok"}
        builder = StateGraph(State)
        builder.add_node("ask_package", ask)
        builder.add_edge(START, "ask_package")
        builder.add_edge("ask_package", END)
        graph = builder.compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "log-test"}}
        with progress_scope(sink):
            await invoke_with_progress(graph, {"status": "running"}, config)
            assert events[-1]["phase"] == "waiting"
            assert "private-question" not in str(events)
            assert (await graph.aget_state(config)).tasks[0].interrupts
            result = await invoke_with_progress(graph, Command(resume=True), config)
        assert result["status"] == "ok"
        assert events[-1]["phase"] == "completed"
        assert not (await graph.aget_state(config)).next
    asyncio.run(run())


def test_handled_and_raised_failures_are_never_reported_as_completed():
    async def run():
        for raised in (False, True):
            events = []
            async def sink(event):
                events.append(event)
            def fail(state):
                if raised:
                    raise ValueError("private-runtime-detail")
                return {"status": "failed", "secret": "private-runtime-detail"}
            builder = StateGraph(State)
            builder.add_node("build_step", fail)
            builder.add_edge(START, "build_step")
            builder.add_edge("build_step", END)
            with progress_scope(sink):
                try:
                    await invoke_with_progress(builder.compile(), {}, {})
                except ValueError:
                    assert raised
            assert events[-1]["phase"] == "error"
            assert "private-runtime-detail" not in str(events)
    asyncio.run(run())


def test_concurrent_jobs_have_isolated_sinks_and_log_failure_is_nonfatal():
    async def job(label):
        events = []
        async def sink(event):
            events.append(event)
        async def work(state):
            await asyncio.sleep(0.01)
            return {"status": label}
        builder = StateGraph(State)
        builder.add_node(label, work)
        builder.add_edge(START, label)
        builder.add_edge(label, END)
        with progress_scope(sink):
            result = await invoke_with_progress(builder.compile(), {}, {})
        assert result["status"] == label
        assert {event["node"] for event in events} == {label}
        return builder.compile()
    async def run():
        graphs = await asyncio.gather(job("load_image"), job("preprocess_image"))
        async def broken_sink(event):
            raise RuntimeError("database unavailable")
        with progress_scope(broken_sink):
            assert (await invoke_with_progress(graphs[0], {}, {}))["status"] == "load_image"
    asyncio.run(run())
