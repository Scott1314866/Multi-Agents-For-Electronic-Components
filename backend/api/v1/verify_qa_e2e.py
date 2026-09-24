#!/usr/bin/env python3
# scripts/manual_tests/test_qa_simple.py
"""
简易 QA 接口集成测试（真实服务调用）
前提：服务已启动（localhost:8000），且 .env 配置了必要的 LLM/数据库/Milvus。

运行：
    conda activate edu_agent
    python scripts/manual_tests/test_qa_simple.py
"""

import sys
import json
import httpx

sys.path.insert(0, ".")

BASE_URL  = "http://localhost:8000/api/v1"
USERNAME  = "student01"
PASSWORD  = "Student@123456"
SESSION   = "qa-simple-session"   # 固定会话 ID


def login() -> str:
    """登录获取 JWT token"""
    resp = httpx.post(
        f"{BASE_URL}/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        trust_env=False
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    print(f"[登录] Token 获取成功（前20字符）: {token[:20]}...")
    return token


def chat(token: str, message: str, session_id: str = SESSION, enable_web_search: bool = False) -> dict:
    """非流式聊天"""
    resp = httpx.post(
        f"{BASE_URL}/qa/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "session_id":        session_id,
            "message":           message,
            "enable_web_search": enable_web_search,
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


def stream_chat(token: str, message: str, session_id: str = SESSION) -> None:
    """流式聊天，打印所有 SSE 事件"""
    print(f"\n[流式] 问题: {message}")
    with httpx.stream(
        "POST",
        f"{BASE_URL}/qa/chat/stream",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept":        "text/event-stream",
        },
        json={"session_id": session_id, "message": message},
        timeout=60.0,
    ) as resp:
        print("[流式] 开始接收事件:")
        for line in resp.iter_lines():
            if line.startswith("data:"):
                data_str = line[5:].strip()
                try:
                    event = json.loads(data_str)
                    # 根据事件类型美化打印
                    if event.get("type") == "token":
                        print(event["content"], end="", flush=True)  # 打字机效果
                    elif event.get("type") == "progress":
                        print(f"\n[进度] {event['stage']}")
                    elif event.get("type") == "meta":
                        print(f"\n[元数据] 模式={event['answer_mode']}, 置信度={event['confidence']}, 来源={event.get('sources', [])}")
                    elif event.get("type") == "done":
                        print("\n[流式] 完成")
                    else:
                        print(f"\n[其他事件] {event}")
                except json.JSONDecodeError:
                    print(f"[原始数据] {data_str}")
        print("\n")


def print_result(label: str, result: dict) -> None:
    """打印非流式结果摘要"""
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  模式 : {result['answer_mode']}")
    print(f"  置信度: {result['confidence']:.4f}")
    print(f"  来源 : {result['sources']}")
    print(f"  回答前100字: {result['answer'][:100]}...")
    print(f"{'='*60}")


if __name__ == "__main__":
    try:
        token = login()
    except Exception as e:
        print(f"❌ 登录失败，请检查服务是否启动及账号密码: {e}")
        sys.exit(1)

    # # ---------- 1. 测试 GENERAL 路径（闲聊） ----------
    # print("\n>>> 测试 GENERAL 路径")
    # r = chat(token, "你好，介绍一下你自己")
    # print_result("GENERAL", r)
    # if r["answer_mode"] == "general":
    #     print("✅ GENERAL 路径正常")
    # else:
    #     print(f"⚠️ 模式为 {r['answer_mode']}，可能未触发 GENERAL（需检查分类器）")
    #
    # # ---------- 2. 测试 PRECISE 路径（具体问题，期望 RAG） ----------
    # print("\n>>> 测试 PRECISE 路径（RAG）")
    # r = chat(token, "介绍下商品聚合的项目？")
    # print_result("PRECISE", r)
    # if r["answer_mode"] == "rag" and len(r["sources"]) > 0:
    #     print("✅ PRECISE RAG 正常")
    # else:
    #     print(f"⚠️ 模式={r['answer_mode']}, 来源数={len(r['sources'])}，可能知识库无相关内容或置信度不足")
    #
    # # ---------- 3. 测试低置信度（知识库无关内容） ----------
    # print("\n>>> 测试低置信度（知识库无关）")
    # r = chat(token, "如何制作披萨？")
    # print_result("低置信度", r)
    # if r["answer_mode"] == "llm_direct" and "⚠️" in r["answer"]:
    #     print("✅ 低置信度路径正常（llm_direct + 提示）")
    # else:
    #     print(f"⚠️ 模式={r['answer_mode']}，可能知识库意外包含相关内容，或置信度判读有变")

    # ---------- 4. 测试流式接口 ----------
    print("\n>>> 测试流式接口")
    stream_chat(token, "介绍下商品聚合的项目")

    # ---------- 5. 测试历史记录接口 ----------
    # print("\n>>> 测试历史记录接口")
    # try:
    #     hist_resp = httpx.get(
    #         f"{BASE_URL}/qa/sessions/{SESSION}/history",
    #         headers={"Authorization": f"Bearer {token}"},
    #         timeout=10.0,
    #     )
    #     hist_resp.raise_for_status()
    #     hist_data = hist_resp.json()
    #     print(f'hist_data-->{hist_data}')
    #     print(f"  会话ID: {hist_data['session_id']}")
    #     print(f"  总轮数: {hist_data['total_turns']}")
    #     summary_val = hist_data.get('summary')
    #     if summary_val is None:
    #         summary_display = "无摘要（对话轮数不足10轮）"
    #     else:
    #         summary_display = summary_val[:100]
    #     if hist_data["total_turns"] > 0:
    #         print("✅ 历史记录接口正常")
    #     else:
    #         print("⚠️ 历史记录为空，可能服务未保存")
    # except Exception as e:
    #     print(f"❌ 历史记录接口异常: {e}")

    print("\n✅ 所有测试完成。")