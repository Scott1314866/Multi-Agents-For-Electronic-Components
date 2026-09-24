# backend/agents/exam/graph.py

from langgraph.graph import StateGraph, START, END

from backend.agents.symbol.state import ExamState
from backend.agents.symbol.nodes import (
    parse_word_node,                 # 节点1：解析学员作答的 Word 试卷文件
    load_questions_meta_node,        # 节点2：从数据库加载题目元数据并与解析结果合并
    run_three_tracks_node,           # 节点3：三轨并行批改（客观题规则 / 简答题 LLM / 代码题 LLM）
    aggregate_results_node,          # 节点4：汇总三轨结果，按题号排序并计算总分
    analyze_weak_points_node,        # 节点5：分析学员知识薄弱点并生成复习建议
    notify_teacher_node,             # 节点6：更新提交状态为待复核，通知教师
    teacher_review_node,             # 节点7：Human-in-the-Loop 暂停点，等待教师决策
    apply_teacher_decision_node,     # 节点8：把教师的 approve/modify 决策合并进批改结果
    publish_results_node,            # 节点9：将最终结果写入数据库并发布
)
from backend.core.memory import get_memory_saver


def build_exam_graph():
    """
    构建并编译 电器符号图生成 Agent 的 LangGraph 状态图。

    执行链路（线性）：
        parse_word → load_questions_meta → run_three_tracks
        → aggregate_results → analyze_weak_points
        → notify_teacher → teacher_review [interrupt]
        → apply_teacher_decision → publish_results → END
    """
    builder = StateGraph(ExamState)

    # ── 注册节点 ──────────────────────────────────────────────
    builder.add_node("parse_word",             parse_word_node)
    builder.add_node("load_questions_meta",    load_questions_meta_node)
    builder.add_node("run_three_tracks",       run_three_tracks_node)
    builder.add_node("aggregate_results",      aggregate_results_node)
    builder.add_node("analyze_weak_points",    analyze_weak_points_node)
    builder.add_node("notify_teacher",         notify_teacher_node)
    builder.add_node("teacher_review",         teacher_review_node)
    builder.add_node("apply_teacher_decision", apply_teacher_decision_node)
    builder.add_node("publish_results",        publish_results_node)

    # ── 固定边（线性链）──────────────────────────────────────
    builder.add_edge(START,                    "parse_word")
    builder.add_edge("parse_word",             "load_questions_meta")
    builder.add_edge("load_questions_meta",    "run_three_tracks")
    builder.add_edge("run_three_tracks",       "aggregate_results")
    builder.add_edge("aggregate_results",      "analyze_weak_points")
    builder.add_edge("analyze_weak_points",    "notify_teacher")
    builder.add_edge("notify_teacher",         "teacher_review")
    builder.add_edge("teacher_review",         "apply_teacher_decision")
    builder.add_edge("apply_teacher_decision", "publish_results")
    builder.add_edge("publish_results",        END)

    # ── 编译，绑定 MemorySaver ────────────────────────────────
    checkpointer = get_memory_saver("exam")
    return builder.compile(checkpointer=checkpointer)
