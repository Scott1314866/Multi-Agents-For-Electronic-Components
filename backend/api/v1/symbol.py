# backend/api/v1/interview.py

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import text

from backend.agents.interview.graph import build_interview_graph
from backend.agents.interview.state import InterviewStage
from backend.core.logger import get_logger
from backend.core.memory import build_thread_id, build_config
from backend.dependencies import AsyncSessionLocal, get_current_user

router = APIRouter()
logger = get_logger(__name__)
_graph = build_interview_graph()   # 模块级单例：导入时编译一次图，所有请求共用


class StartSessionRequest(BaseModel):
    target_position:  str                  # 目标岗位（必填）
    resume_review_id: str | None = None    # 简历审查记录 ID（可选，做项目联动）


class ChatRequest(BaseModel):
    message: str                           # 学员这一轮发的消息

@router.post("/sessions", status_code=201)
async def start_session(
    req: StartSessionRequest,                            # 请求体：岗位 + 可选简历 ID
    current_user: dict = Depends(get_current_user),      # 依赖注入：当前登录用户
):
    """
    创建新面试会话，触发首轮开场白（同步等待 LLM 返回）。

    请求：{target_position, resume_review_id?}
    响应：201 + {session_id, target_position, status, message(开场白)}
    """
    student_id = current_user["user_id"]                 # 学员 ID
    tenant_id  = current_user["tenant_id"]               # 租户 ID
    session_id = str(uuid.uuid4())                       # 生成会话 ID
    thread_id  = build_thread_id(student_id, session_id) # 拼 MemorySaver 的 key

    # 1. 写入初始会话记录（status=in_progress）
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO interview_sessions
                    (id, tenant_id, student_id, session_id, thread_id,
                     target_position, resume_review_id, status)
                VALUES
                    (:id, :tenant_id, :student_id, :session_id, :thread_id,
                     :target_position, :resume_review_id, 'in_progress')
            """),
            {
                "id":               str(uuid.uuid4()),
                "tenant_id":        tenant_id,
                "student_id":       student_id,
                "session_id":       session_id,
                "thread_id":        thread_id,
                "target_position":  req.target_position,
                "resume_review_id": req.resume_review_id,
            },
        )
        await session.commit()

    # 2. 构造完整 initial_state（首轮 MemorySaver 里还没存档，必须传全量 22 字段）
    initial_state = {
        "messages":            [HumanMessage(content="[开始面试]")],  # 触发图执行的特殊消息
        "student_id":          student_id,
        "tenant_id":           tenant_id,
        "session_id":          session_id,
        "target_position":     req.target_position,
        "resume_review_id":    req.resume_review_id,
        "resume_projects":     [],
        "resume_skills":       [],
        "current_stage":       InterviewStage.WARMUP.value,  # 从热身开始
        "stage_turn_count":    0,
        "total_turn_count":    0,                         # 0 → load_context 走首轮初始化
        "max_turns":           40,
        "question_bank":       [],
        "current_question":    None,
        "projects_asked":      [],
        "last_answer_quality": "adequate",
        "followup_count":      0,
        "existing_summary":    None,
        "should_summarize":    False,
        "report":              None,
        "fallback_used":       False,
        "structured_output":   None,
    }

    # 3. 首轮图执行（同步等待，拿到开场白）
    config  = build_config(student_id, session_id)        # 带 thread_id 的运行配置
    result  = await _graph.ainvoke(initial_state, config=config)  # 跑一遍图
    messages     = result.get("messages", [])             # 取结果消息
    opening_msg  = _get_last_ai_message(messages)          # 提取面试官开场白

    logger.info("interview.session_started", session_id=session_id,
                target_position=req.target_position)

    return {                                              # 返回会话信息 + 开场白
        "session_id":      session_id,
        "target_position": req.target_position,
        "status":          "in_progress",
        "message":         opening_msg,
    }

@router.post("/sessions/{session_id}/chat")
async def chat(
    session_id: str,
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    学员发送消息，面试官生成回应。
    MemorySaver 通过 thread_id 自动恢复历史 State，只需传入新消息。

    请求：{message}
    响应：{session_id, reply, current_stage, total_turns, is_finished, report_summary?}
    """
    student_id = current_user["user_id"]                 # 学员 ID
    tenant_id  = current_user["tenant_id"]               # 租户 ID

    # 验证 session 归属（防止越权访问别人的会话）
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT session_id FROM interview_sessions
                WHERE session_id = :session_id
                  AND tenant_id  = :tenant_id
                  AND student_id = :student_id
            """),
            {"session_id": session_id, "tenant_id": tenant_id, "student_id": student_id},
        )
        if not result.fetchone():                        # 查不到 → 不属于该用户
            raise HTTPException(status_code=404, detail="面试会话不存在")

    config = build_config(student_id, session_id)        # 带 thread_id 的运行配置

    # 只传增量：完整 State 由 MemorySaver 按 thread_id 自动恢复
    state_update = {
        "messages":   [HumanMessage(content=req.message)],  # 学员新消息（add_messages 会追加）
        "student_id": student_id,                        # 显式带上，防 MemorySaver 丢失时崩
        "session_id": session_id,
        "tenant_id":  tenant_id,
    }

    result = await _graph.ainvoke(state_update, config=config)  # 跑一遍图

    current_stage = result.get("current_stage", InterviewStage.WARMUP.value)  # 当前阶段
    total_turns   = result.get("total_turn_count", 0)    # 总轮数
    is_finished   = current_stage == InterviewStage.FINISHED.value  # 是否已结束
    reply         = _get_last_ai_message(result.get("messages", []))  # 面试官这轮回复

    response = {
        "session_id":    session_id,
        "reply":         reply,
        "current_stage": current_stage,
        "total_turns":   total_turns,
        "is_finished":   is_finished,
    }

    # 面试结束时附带报告摘要（前端可直接展示）
    if is_finished:
        report = result.get("report") or {}
        response["report_summary"] = {
            "overall_score": report.get("overall_score", 0),
            "strengths":     report.get("strengths", []),
            "improvements":  report.get("improvements", []),
        }

    return response


@router.get("/sessions/{session_id}/report")
async def get_report(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT session_id, target_position, status,
                       overall_score, report, finished_at
                FROM interview_sessions
                WHERE session_id = :session_id
                  AND student_id = :student_id
            """),
            {"session_id": session_id, "student_id": current_user["user_id"]},
        )
        row = result.mappings().fetchone()           # 取会话行

    if not row:                                       # 不存在或不属于该学员
        raise HTTPException(status_code=404, detail="面试记录不存在")

    if row["status"] != "finished":                   # 还没结束 → 报告未生成
        raise HTTPException(status_code=400, detail="面试尚未结束，报告生成中")

    # report 存在 JSONB 列：ORM 可能返回 dict（已解析）或字符串（需 json.loads），两种都兼容
    report = (
        row["report"]
        if isinstance(row["report"], dict)
        else (json.loads(row["report"]) if row["report"] else {})
    )

    return {                                           # 摊平成给前端的完整报告
        "session_id":         session_id,
        "target_position":    row["target_position"],
        "overall_score":      row["overall_score"],
        "dimensions":         report.get("dimensions", []),
        "strengths":          report.get("strengths", []),
        "improvements":       report.get("improvements", []),
        "overall_comment":    report.get("overall_comment", ""),
        "recommended_topics": report.get("recommended_topics", []),
        "next_step_advice":   report.get("next_step_advice", ""),
    }

@router.get("/sessions")
async def list_sessions(current_user: dict = Depends(get_current_user)):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT session_id, target_position, overall_score,
                       status, finished_at, created_at
                FROM interview_sessions
                WHERE student_id = :student_id
                ORDER BY created_at DESC
                LIMIT 50
            """),
            {"student_id": current_user["user_id"]},
        )
        rows = result.mappings().all()

    items = [
        {
            "session_id":      row["session_id"],
            "target_position": row["target_position"],
            "overall_score":   row["overall_score"],
            "status":          row["status"],
            "finished_at":     row["finished_at"].isoformat() if row["finished_at"] else None,
            "created_at":      row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]

    return {"items": items, "total": len(items)}


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(
    session_id: str,
    req: ChatRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    SSE 流式接口，适配语音交互模式（边生成边推 token）。

    请求：{message}
    响应：text/event-stream，逐条 data: 事件——
          type=token（面试官回复的每个片段）/ type=done（结束+最终状态）/ type=error
    """
    student_id = current_user["user_id"]                 # 学员 ID
    tenant_id  = current_user["tenant_id"]               # 租户 ID

    async with AsyncSessionLocal() as db:                # 验证会话归属
        result = await db.execute(
            text("SELECT session_id FROM interview_sessions WHERE session_id = :sid AND tenant_id = :tid"),
            {"sid": session_id, "tid": tenant_id},
        )
        if not result.fetchone():
            raise HTTPException(status_code=404, detail="面试会话不存在")

    config = build_config(student_id, session_id)        # 带 thread_id 的运行配置
    state_update = {                                     # 只传增量（同 /chat）
        "messages":   [HumanMessage(content=req.message)],
        "student_id": student_id,
        "session_id": session_id,
        "tenant_id":  tenant_id,
    }

    async def event_generator():                         # 异步生成器：逐个 yield SSE 事件
        try:
            # astream_events 流式跑图，逐个事件回调（version="v2" 是事件格式版本）
            async for event in _graph.astream_events(state_update, config=config, version="v2"):
                evt  = event["event"]                    # 事件类型
                node = event.get("metadata", {}).get("langgraph_node", "")  # 事件来自哪个节点

                # 只对 generate_response 节点的 token 流做转发（面试官回复）
                # 其他节点（check_stage/evaluate_answer 等）的 LLM 输出不暴露给前端
                if evt == "on_chat_model_stream" and node == "generate_response":
                    chunk = event["data"].get("chunk")   # 取本次 token 片段
                    if chunk and chunk.content:
                        yield {                          # 推一个 token 事件
                            "data": json.dumps(
                                {"type": "token", "content": chunk.content},
                                ensure_ascii=False,      # 中文不转义
                            )
                        }

            # 流结束后用 aget_state 读最终 State，组装 done 事件
            final         = await _graph.aget_state(config)  # 读检查点最新状态
            state         = final.values if final else {}
            current_stage = state.get("current_stage", InterviewStage.WARMUP.value)
            total_turns   = state.get("total_turn_count", 0)
            is_finished   = current_stage == InterviewStage.FINISHED.value
            reply         = _get_last_ai_message(state.get("messages", []))  # 完整回复（给前端兜底）

            done_payload: dict = {                       # done 事件：带最终状态
                "type":          "done",
                "reply":         reply,
                "current_stage": current_stage,
                "total_turns":   total_turns,
                "is_finished":   is_finished,
            }
            if is_finished:                              # 结束则附报告摘要
                report = state.get("report") or {}
                done_payload["report_summary"] = {
                    "overall_score": report.get("overall_score", 0),
                    "strengths":     report.get("strengths", []),
                    "improvements":  report.get("improvements", []),
                }

            yield {"data": json.dumps(done_payload, ensure_ascii=False)}  # 推 done 事件

        except Exception as e:                           # 出错推一个 error 事件
            logger.error("interview.chat_stream_error", error=str(e), exc_info=True)
            yield {
                "data": json.dumps(
                    {"type": "error", "message": "流式输出异常，请重试"},
                    ensure_ascii=False,
                )
            }

    return EventSourceResponse(event_generator())        # 用 SSE 响应包装生成器

def _get_last_ai_message(messages: list) -> str:
    """提取消息列表中最后一条面试官（AI）消息的文本。"""
    for msg in reversed(messages):                    # 从后往前找
        if isinstance(msg, AIMessage):                # 找到最后一条 AI 消息
            content = msg.content
            if isinstance(content, list):             # 多模态：拼接各片段文本
                return "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            return str(content)                       # 普通字符串直接返回
    return "面试已开始，请等待面试官回应..."           # 没有 AI 消息时的兜底文案