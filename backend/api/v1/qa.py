# backend/api/v1/qa.py

import json
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import HumanMessage

from backend.agents.qa.graph import build_qa_graph
from backend.core.memory import build_thread_id
from backend.dependencies import get_current_user
from backend.core.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


# ── 请求 / 响应模型 ───────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id:        str        = Field(..., description="会话 ID")
    course_id:         str | None = Field(None, description="课程 ID（可选，限定检索范围）")
    message:           str        = Field(..., min_length=1, max_length=2000)
    enable_web_search: bool       = Field(False, description="低置信度时是否先走 Web Search 再给 LLM")


class ChatResponse(BaseModel):
    session_id:    str
    answer:        str
    answer_mode:   str        # "rag" / "web_augmented" / "llm_direct" / "general"
    confidence:    float
    sources:       list[str]
    fallback_used: bool


class SessionMessage(BaseModel):
    role:       str   # "user" / "assistant"
    content:    str
    created_at: str


class HistoryResponse(BaseModel):
    session_id:  str
    messages:    list[SessionMessage]
    summary:     str | None
    total_turns: int

@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """智能问答：发送消息，获取 RAG 或 LLM 直答（非流式）"""
    graph     = build_qa_graph()
    thread_id = build_thread_id(current_user["user_id"], req.session_id)

    initial_state = {
        "messages":            [HumanMessage(content=req.message)],
        "student_id":          current_user["user_id"],
        "tenant_id":           current_user["tenant_id"],
        "session_id":          req.session_id,
        "course_id":           req.course_id,
        "query_type":          "PRECISE",     # 占位初始值，classify_query_node 内部会动态覆盖
        "enable_web_search":   req.enable_web_search,
        "web_search_results":  [],            # 每轮重置，防止上轮搜索结果污染本轮 sources
    }
    config: dict = {"configurable": {"thread_id": thread_id}}

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        logger.error("chat.invoke_error", error=str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "AGENT_ERROR", "message": str(e)},
        )
    print(f'response: {result}')
    return ChatResponse(
        session_id=req.session_id,
        answer=result.get("answer", ""),
        answer_mode=result.get("answer_mode", "llm_direct"),
        confidence=result.get("confidence", 0.0),
        sources=result.get("sources", []),
        fallback_used=result.get("fallback_used", False),
    )

@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """智能问答流式接口（SSE），与 /chat 入参相同，响应改为流式推送"""
    graph     = build_qa_graph()
    thread_id = build_thread_id(current_user["user_id"], req.session_id)

    initial_state = {
        "messages":            [HumanMessage(content=req.message)],
        "student_id":          current_user["user_id"],
        "tenant_id":           current_user["tenant_id"],
        "session_id":          req.session_id,
        "course_id":           req.course_id,
        "query_type":          "PRECISE",
        "enable_web_search":   req.enable_web_search,
        "web_search_results":  [],            # 每轮重置，防止上轮搜索结果污染本轮 sources
    }
    config: dict = {"configurable": {"thread_id": thread_id}}

    # 只对这三个节点做 token 级流式推送
    _GENERATE_NODES = {"generate_rag", "generate_direct", "generate_general"}

    # 节点开始时推送的进度文案
    _PROGRESS_LABELS = {
        "classify_query":      "理解问题中...",
        "hyde_generate":       "理解问题中...",
        "multi_query_rewrite": "改写查询中...",
        "retrieve":            "召回相关文档...",
        "web_search":          "搜索互联网...",
        "generate_general":    "思考中...",
    }

    async def event_generator():
        answer_mode = "llm_direct"
        confidence  = 0.0
        sources: list[str] = []

        try:
            async for event in graph.astream_events(
                initial_state, config=config, version="v2"
            ):
                evt  = event["event"]
                node = event.get("metadata", {}).get("langgraph_node", "")

                # ── 节点开始：推送进度提示 ──────────────────────
                if evt == "on_chain_start" and node in _PROGRESS_LABELS:
                    yield {
                        "data": json.dumps(
                            {"type": "progress", "stage": _PROGRESS_LABELS[node]},
                            ensure_ascii=False,
                        )
                    }

                # ── LLM 实时 token ──────────────────────────────
                elif evt == "on_chat_model_stream" and node in _GENERATE_NODES:
                    chunk = event["data"].get("chunk")
                    if chunk and chunk.content:
                        yield {
                            "data": json.dumps(
                                {"type": "token", "content": chunk.content},
                                ensure_ascii=False,
                            )
                        }

                # ── 节点结束：捕获元数据 ────────────────────────
                elif evt == "on_chain_end" and node in _GENERATE_NODES:
                    output = event["data"].get("output", {})
                    if isinstance(output, dict):
                        _mode = output.get("answer_mode")
                        if _mode:
                            answer_mode = _mode
                        _srcs = output.get("sources")
                        if _srcs is not None:
                            sources = _srcs
                        _conf = (output.get("structured_output") or {}).get("confidence")
                        if _conf is not None:
                            confidence = _conf

        except Exception as e:
            logger.error("chat_stream.error", error=str(e), exc_info=True)
            yield {
                "data": json.dumps(
                    {"type": "error", "message": "流式输出异常，请使用普通接口重试"},
                    ensure_ascii=False,
                )
            }
            return

        # ── 元数据帧（流结束后一次性推送）──────────────────────
        yield {
            "data": json.dumps(
                {
                    "type":        "meta",
                    "session_id":  req.session_id,
                    "answer_mode": answer_mode,
                    "confidence":  confidence,
                    "sources":     sources,
                },
                ensure_ascii=False,
            )
        }
        yield {"data": json.dumps({"type": "done"})}

    return EventSourceResponse(event_generator())

@router.get("/sessions/{session_id}/history", response_model=HistoryResponse)
async def get_session_history(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    """获取会话历史消息与摘要"""
    from sqlalchemy import text as sa_text
    from langchain_core.messages import HumanMessage as LCHuman, AIMessage as LCAi
    from backend.dependencies import AsyncSessionLocal

    student_id = current_user["user_id"]
    thread_id  = build_thread_id(student_id, session_id)

    # ── ① 从 DB 读摘要 ────────────────────────────────────────
    summary = None
    try:
        async with AsyncSessionLocal() as db_session:
            result = await db_session.execute(
                sa_text(
                    "SELECT summary FROM qa_sessions "
                    "WHERE thread_id = :tid AND student_id = :sid"
                ),
                {"tid": thread_id, "sid": student_id},
            )
            row = result.fetchone()
            if row:
                summary = row[0]
    except Exception as e:
        logger.warning("get_history.db_error", error=str(e))

    # ── ② 从 MemorySaver 读消息历史 ──────────────────────────
    messages: list[SessionMessage] = []
    total_turns = 0
    try:
        graph  = build_qa_graph()
        config = {"configurable": {"thread_id": thread_id}}
        state  = await graph.aget_state(config)
        if state and state.values:
            for msg in state.values.get("messages", []):
                content = (
                    msg.text
                    if hasattr(msg, "text") and not callable(msg.text)
                    else str(msg.content)
                )
                if isinstance(msg, LCHuman):
                    messages.append(SessionMessage(role="user", content=content, created_at=""))
                elif isinstance(msg, LCAi):
                    messages.append(SessionMessage(role="assistant", content=content, created_at=""))
            total_turns = sum(1 for m in messages if m.role == "user")
    except Exception as e:
        logger.warning("get_history.checkpoint_error", error=str(e))

    return HistoryResponse(
        session_id=session_id,
        messages=messages,
        summary=summary,
        total_turns=total_turns,
    )

