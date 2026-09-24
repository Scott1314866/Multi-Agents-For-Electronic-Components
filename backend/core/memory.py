# backend/core/memory.py

from typing import Optional
from pydantic import BaseModel, Field
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage

from backend.core.logger import get_logger

logger = get_logger(__name__)

# 每个 Agent 独立的 MemorySaver 实例
# 不同 Agent 的 State schema 不同，共用同一个 MemorySaver 会导致
# msgpack 序列化时 schema 字段冲突，必须隔离
_memory_savers: dict[str, MemorySaver] = {}


def get_memory_saver(agent_type: str = "default") -> MemorySaver:
    """
    获取指定 Agent 类型的 MemorySaver 单例。

    本地阶段使用内存存储（进程重启后历史丢失）。
    生产阶段替换为 AsyncPostgresSaver 即可持久化，业务代码无需修改。

    Args:
        agent_type: Agent 标识符，如 "qa" / "pcb" / "step" / "symbol"

    Returns:
        MemorySaver 实例，传给 StateGraph.compile(checkpointer=...)
    """
    if agent_type not in _memory_savers:
        _memory_savers[agent_type] = MemorySaver()
        logger.info("memory.saver_initialized", agent=agent_type)
    return _memory_savers[agent_type]


def build_thread_id(student_id: str, session_id: str) -> str:
    """
    构建 LangGraph Checkpointer 使用的 thread_id。

    格式：student_{student_id}_session_{session_id}
    同一用户的不同会话有独立的历史，互不干扰。

    示例：
        build_thread_id("Scott", "7jlk9nlkxvfjhgjh3g4g")
        → "user_Scott_session_7jlk9nlkxvfjhgjh3g4g"
    """
    return f"student_{student_id}_session_{session_id}"


def build_config(student_id: str, session_id: str) -> dict:
    """
    构建 LangGraph 调用所需的 config 字典。

    用法：
        config = build_config(student_id, session_id)
        result = await graph.ainvoke(state, config=config)

    Returns:
        {"configurable": {"thread_id": "student_xxx_session_yyy"}}
    """
    return {
        "configurable": {
            "thread_id": build_thread_id(student_id, session_id),
        }
    }


def trim_messages_to_window(
    messages: list[BaseMessage],
    window_size: int = 10,
) -> list[BaseMessage]:
    # TODO 1: 目前是滑动窗口 + 摘要压缩的方式进行记忆压缩, 后期可以改成按照token数量(在token监测系统基础上)
    """
    滑动窗口裁剪：保留最近 window_size 轮对话。
    SystemMessage 始终保留在最前，不受 window_size 限制。

    Args:
        messages:    当前完整消息列表
        window_size: 保留的对话轮数（1轮 = 1 Human + 1 AI），默认 10 轮

    Returns:
        裁剪后的消息列表
    """
    system_messages  = [m for m in messages if isinstance(m, SystemMessage)]
    # TODO: print记得注释
    # print(f'system_messages: {system_messages}')
    dialogue_messages = [m for m in messages if not isinstance(m, SystemMessage)]
    # print(f'dialogue_messages: {dialogue_messages}')
    max_dialogue_messages = window_size * 2   # 1轮 = 2条消息

    if len(dialogue_messages) <= max_dialogue_messages:
        return messages   # 未超出窗口，不裁剪

    trimmed = dialogue_messages[-max_dialogue_messages:]

    logger.info(
        "memory.window_trimmed",
        original=len(dialogue_messages),
        kept=len(trimmed),
        window_size=window_size,
    )

    return system_messages + trimmed

def should_trigger_summary(
    messages: list[BaseMessage],
    threshold: int = 10,
) -> bool:
    """
    判断对话轮数是否超过阈值，决定是否触发摘要压缩。

    Args:
        messages:  当前消息列表
        threshold: 触发压缩的轮数阈值，默认 10 轮

    Returns:
        True → 需要压缩
    """
    dialogue_count = sum(1 for m in messages if isinstance(m, (HumanMessage, AIMessage)))
    # print(f'dialogue_count: {dialogue_count}')
    # rounds = dialogue_count // 2 # 每轮2条
    #
    return dialogue_count % ( threshold * 2 ) == 0 # 每轮 2 条，dialogue_count / 2 = 实际轮数

class PackageParams(BaseModel):
    """对话中沉淀的封装参数（结构化摘要，供符号图/PCB图/STEP图绘制使用）。
    字段均为可选：对话中未提及的参数保持 None，禁止编造。"""
    package_name: Optional[str] = Field(None, description="封装名称，如 VFQFPN36")
    pin_count: Optional[int] = Field(None, description="引脚数量")
    body_size: Optional[str] = Field(None, description="封装体尺寸，如 6x6mm")
    pitch: Optional[str] = Field(None, description="引脚间距，如 0.5mm")
    key_points: str = Field(description="其他与封装绘制相关的关键约定/补充说明，没有则填\"无\"")


async def compress_to_summary(
    messages: list[BaseMessage],
    existing_summary: Optional[dict] = None,
) -> dict:
    # TODO 2:目前只覆盖符号图核心参数, 后期应扩展 PCB图和STEP图绘制所需的信息[加入意图识别 或者 统一提取的封装信息]
    """
    将历史对话压缩为结构化的封装参数摘要（PackageParams）。

    增量压缩：传入 existing_summary（上次返回的 dict）防止已确认参数被改写或丢失。

    Args:
        messages:         待压缩的历史消息列表
        existing_summary: 上次的结构化摘要（可选）

    Returns:
        PackageParams 的 dict 形式，可直接 JSON 序列化后存入记忆
    """
    from langchain_core.messages import HumanMessage as LCHuman
    from backend.core.llm_factory import get_structured_llm

    SUMMARY_PROMPT = """请将以下对话压缩为结构化的封装参数摘要（用于后续绘制符号图 / PCB图 / STEP图）。

【压缩规则】
必须保留：已确认的封装名称 / 引脚数量 / 封装体尺寸 / 引脚间距等参数
选择性保留：用户提出的绘制要求、格式约定、补充说明
可以丢弃：闲聊 / 寒暄 / 与封装无关的内容
约束：对话中没有出现的参数必须保持 null，不得编造；已有参数以用户最新确认为准。

【上一次摘要】
{previous_summary}

【本次新增对话】
{new_conversations}
"""

    conversation_text = "\n".join([
        f"{'用户' if isinstance(m, HumanMessage) else 'AI'}：{m.text if hasattr(m, 'text') and not callable(m.text) else str(m.content)}"
        for m in messages
        if isinstance(m, (HumanMessage, AIMessage))
    ])

    prompt_text = SUMMARY_PROMPT.format(
        previous_summary=existing_summary or "（无上次摘要）",
        new_conversations=conversation_text,
    )

    llm_struct = get_structured_llm("summarize", PackageParams)
    result = await llm_struct.ainvoke([LCHuman(content=prompt_text)])
    summary = result.model_dump()

    logger.info(
        "memory.summary_generated",
        input_messages=len(messages),
        summary_fields=len([v for v in summary.values() if v]),
    )
    return summary

if __name__ == '__main__':
    # ── 测试：对话压缩为结构化封装参数摘要 ─────────────────────────
    messages = [SystemMessage(content="你是电子元器件封装信息提取专家")]
    # 模拟 3 轮封装相关对话
    dialogue = [
        ("你好，帮我看下 VFQFPN36 这个封装", "好的，VFQFPN36 是 36 引脚的微小型 QFN 封装"),
        ("它的尺寸是多少？", "封装体 6x6mm，引脚间距 0.5mm"),
        ("好的，按这个参数准备画符号图", "没问题，参数已记录"),
    ]
    for q, a in dialogue:
        messages.append(HumanMessage(content=q))
        messages.append(AIMessage(content=a))

    import asyncio
    async def main():
        if should_trigger_summary(messages, threshold=3):
            summary = await compress_to_summary(messages)
            print(f'summary: {summary}')
    asyncio.run(main())
