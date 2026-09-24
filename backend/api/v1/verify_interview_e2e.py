# scripts/manual_tests/itv_07_13_e2e.py

import sys
import json
import time
import asyncio
import httpx

sys.path.insert(0, "scripts/manual_tests")
from backend.agents.interview.manual_tests.itv_fixtures import seed_resume, ANSWER_SCRIPT, TARGET_POSITION

BASE_URL   = "http://localhost:8006/api/v1"
STUDENT_UN = "student01"
STUDENT_PW = "Student@123456"

# 学员回答序列：直接复用贯穿全章 07-02~07-12 的统一答题脚本（AI 大模型岗，李明）。
# 覆盖 WARMUP 自我介绍 → 4 道技术题（含一题"不知道"）→ 2 轮项目深挖 → 反问 → 结束面试。
ANSWER_SEQUENCE = ANSWER_SCRIPT


def login(username: str, password: str) -> str:
    resp = httpx.post(
        f"{BASE_URL}/auth/login",
        json={"username": username, "password": password},
        trust_env=False,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print("="*60)


if __name__ == "__main__":
    # ── ① 学员登录 ─────────────────────────────────────────────
    print_section("① 学员登录")
    token = login(STUDENT_UN, STUDENT_PW)
    print(f"token 获取成功：{token[:20]}...")

    headers = {"Authorization": f"Bearer {token}"}

    # ── ②a 写入李明简历（供 PROJECT 阶段联动深挖）────────────────
    seeded = asyncio.run(seed_resume())
    print(f"简历已写入，resume_review_id: {seeded['resume_review_id']}")

    # ── ②b 创建面试会话（带 resume_review_id 联动）──────────────
    print_section(f"② 创建面试会话（目标岗位：{TARGET_POSITION}）")
    resp = httpx.post(
        f"{BASE_URL}/interview/sessions",
        headers=headers,
        json={"target_position": TARGET_POSITION,
              "resume_review_id": seeded["resume_review_id"]},
        timeout=30.0,
        trust_env=False,
    )
    resp.raise_for_status()
    session_data = resp.json()
    session_id   = session_data["session_id"]
    print(f"session_id: {session_id}")
    print(f"开场白：\n{session_data['message']}\n")

    # ── ③ 多轮对话 ─────────────────────────────────────────────
    print_section("③ 开始多轮对话")
    for i, answer in enumerate(ANSWER_SEQUENCE):
        print(f"\n--- 第{i+1}轮 ---")
        print(f"学员：{answer[:60]}...")

        resp = httpx.post(
            f"{BASE_URL}/interview/sessions/{session_id}/chat",
            headers=headers,
            json={"message": answer},
            timeout=30.0,
            trust_env=False,
        )
        resp.raise_for_status()
        result = resp.json()

        print(f"面试官：{result['reply'][:80]}...")
        print(f"阶段：{result['current_stage']}  总轮数：{result['total_turns']}")

        if result["is_finished"]:
            print("\n面试已结束！")
            summary = result.get("report_summary", {})
            print(f"综合评分：{summary.get('overall_score', 0)} / 100")
            print(f"优势：{summary.get('strengths', [])[:2]}")
            print(f"改进方向：{summary.get('improvements', [])[:2]}")
            break

        time.sleep(1)  # 避免过快请求

    # ── ④ 查看完整报告 ─────────────────────────────────────────
    print_section("④ 查看完整评估报告")
    resp = httpx.get(
        f"{BASE_URL}/interview/sessions/{session_id}/report",
        headers=headers,
        trust_env=False,
    )
    resp.raise_for_status()
    report = resp.json()

    print(f"综合评分：{report['overall_score']} / 100")
    print(f"维度数量：{len(report['dimensions'])}")
    for dim in report["dimensions"]:
        print(f"  - {dim['dimension']}：{dim['score']}分  {dim['comment'][:40]}")
    print(f"推荐复习：{report['recommended_topics']}")
    print(f"下一步建议：{report['next_step_advice']}")

    # ── ⑤ 查看历史面试列表 ─────────────────────────────────────
    print_section("⑤ 查看历史面试列表")
    resp = httpx.get(
        f"{BASE_URL}/interview/sessions",
        headers=headers,
        trust_env=False,
    )
    resp.raise_for_status()
    sessions_list = resp.json()
    print(f"历史面试数量：{sessions_list['total']}")
    for s in sessions_list["items"][:3]:
        print(f"  - {s['session_id'][:8]}... [{s['target_position']}] "
              f"评分：{s['overall_score'] or '进行中'} 状态：{s['status']}")

    print("\n" + "="*60)
    print("  端到端测试全部通过 ✅")
    print("="*60)