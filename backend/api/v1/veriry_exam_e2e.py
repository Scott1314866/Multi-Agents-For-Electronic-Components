# scripts/manual_tests/test_exam.py

import sys
import json
import time
import httpx

sys.path.insert(0, ".")

BASE_URL    = "http://localhost:8000/api/v1"
EXAM_ID     = "e0000001-0000-0000-0000-000000000001"   # seed_data.py 写入的 Java 基础测试卷
DOCX_PATH   = "/Users/ligang/PycharmProjects/LLM/EduAgent/backend/agents/exam/student_answer.docx"
STUDENT_UN  = "student01"
STUDENT_PW  = "Student@123456"
TEACHER_UN  = "teacher01"
TEACHER_PW  = "Teacher@123456"


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
    # ── ① 学员登录 ────────────────────────────────────────────
    print_section("① 学员登录")
    student_token = login(STUDENT_UN, STUDENT_PW)
    print(f"学员 token 获取成功：{student_token[:20]}...")

    # ── ② 提交试卷 ───────────────────────────────────────────
    print_section("② 学员提交试卷")
    with open(DOCX_PATH, "rb") as f:
        resp = httpx.post(
            f"{BASE_URL}/exam/submit",
            headers={"Authorization": f"Bearer {student_token}"},
            data={"exam_id": EXAM_ID},
            files={"file": ("test_exam_answer.docx", f, "application/octet-stream")},
            timeout=30.0,
            trust_env=False,
        )
    resp.raise_for_status()
    submit_result = resp.json()
    # print(f"submit result: {submit_result}")

    submission_id = submit_result["submission_id"]
    print(f"提交成功，submission_id: {submission_id}")
    print(f"状态: {submit_result['status']}")
    # ── ③ 轮询批改状态 ─────────────────────────────────────
    print_section("③ 轮询批改状态（等待 AI 批改完成）")
    for attempt in range(20):
        time.sleep(5)
        resp = httpx.get(
            f"{BASE_URL}/exam/my-submissions/{submission_id}",
            headers={"Authorization": f"Bearer {student_token}"},
            trust_env=False,
        )
        resp.raise_for_status()
        poll = resp.json()
        print(f"  第{attempt+1}次轮询，状态：{poll['status']}")
        if poll["status"] == "pending_review":
            print("  AI 批改完成，等待教师确认")
            break
        if poll["status"] == "published":
            print("  已发布（可能已被其他测试发布）")
            break
    else:
        print("  ⚠️ 超时，请检查后端日志")
        sys.exit(1)
    # ── ④ 教师登录 ────────────────────────────────────────────
    print_section("④ 教师登录")
    teacher_token = login(TEACHER_UN, TEACHER_PW)
    print(f"教师 token 获取成功：{teacher_token[:20]}...")
    #
    # ── ⑤ 查看待确认列表 ─────────────────────────────────────
    print_section("⑤ 教师查看待确认列表")
    resp = httpx.get(
        f"{BASE_URL}/exam/pending-reviews",
        headers={"Authorization": f"Bearer {teacher_token}"},
        trust_env=False,
    )
    resp.raise_for_status()
    pending = resp.json()
    print(f'pending: {pending}')
    print(f"待确认数量：{pending['total']}")
    for item in pending["items"][:3]:
        print(f"  - {item['submission_id'][:8]}... 学员：{item['student_name']} "
              f"AI分：{item['pre_review']['total_score']}/{item['pre_review']['full_score']}")

    # # ── ⑥ 查看预批改详情 ─────────────────────────────────────
    print_section("⑥ 教师查看预批改详情")
    resp = httpx.get(
        f"{BASE_URL}/exam/submissions/{submission_id}/review",
        headers={"Authorization": f"Bearer {teacher_token}"},
        trust_env=False,
    )
    resp.raise_for_status()
    review = resp.json()
    print(f"review result: {review}")
    summary = review["pre_review_summary"]
    print(f"AI 总分：{summary['total_score']}/{summary['full_score']} "
          f"（{summary['score_rate']*100:.1f}%）")
    print(f"需复核题数：{summary.get('needs_review_count', 0)}")
    print(f"薄弱点数量：{len(review['weak_points'])}")
    for wp in review["weak_points"][:3]:
        print(f"  - {wp['tag']}：{wp['wrong_count']} 道错题")

    # 在 ⑥ 获取预批改详情之后，插入这段代码
    print_section("⑥.⑤ 准备修改一道题的分数")

    # 从 review 结果中找一道简答题（short_answer）或任意非客观题
    target_question = None
    for q in review["pre_review_summary"]["by_question"]:
        if q["question_type"] == "short_answer":
            target_question = q
            break

    if not target_question:
        # 如果没有简答题，就取第一道题（但需要确保不是客观题，否则改了没意义）
        target_question = review["pre_review_summary"]["by_question"][0]

    original_score = target_question["score"]
    new_score = min(original_score + 1, target_question["full_score"])  # 加1分，不超过满分

    print(f"准备修改题号 {target_question['question_no']}，原分 {original_score} -> 新分 {new_score}")

    # ── ⑦ 教师 modify 发布 ──────────────────────────────────
    print_section("⑦ 教师执行 modify 发布")
    resp = httpx.post(
        f"{BASE_URL}/exam/submissions/{submission_id}/confirm",
        headers={"Authorization": f"Bearer {teacher_token}"},
        json={
            "action": "modify",
            "modifications": [
                {
                    "question_id": target_question["question_id"],  # 使用真实 ID
                    "new_score": new_score,
                    "comment": f"测试修改：从 {original_score} 分调整为 {new_score} 分"
                }
            ]
        },
        timeout=30.0,
        trust_env=False,
    )
    resp.raise_for_status()
    confirm = resp.json()
    print(f"发布成功，最终总分：{confirm['final_score']}/{confirm['full_score']}")
    # ── ⑦ 教师 approve 发布 ──────────────────────────────────
    # print_section("⑦ 教师 approve 发布")
    # resp = httpx.post(
    #     f"{BASE_URL}/exam/submissions/{submission_id}/confirm",
    #     headers={"Authorization": f"Bearer {teacher_token}"},
    #     json={"action": "approve", "modifications": []},
    #     timeout=30.0,
    #     trust_env=False,
    # )
    # resp.raise_for_status()
    # confirm = resp.json()
    # print(f"发布成功，最终得分：{confirm['final_score']}/{confirm['full_score']}")
    # print(f"得分率：{confirm['score_rate']*100:.1f}%")
    # assert confirm["status"] == "published"
    # print("✅ approve 流程通过")

    # ── ⑧ 学员查询最终结果 ──────────────────────────────────
    print_section("⑧ 学员查询最终结果")
    resp = httpx.get(
        f"{BASE_URL}/exam/my-submissions/{submission_id}",
        headers={"Authorization": f"Bearer {student_token}"},
        trust_env=False,
    )
    resp.raise_for_status()
    final = resp.json()
    assert final["status"] == "published"
    by_q = final["pre_review_summary"]["by_question"]
    print(f"题目数量：{len(by_q)}")
    for q in by_q:
        print(f"  题{q['question_no']} [{q['question_type']}] "
              f"{q['final_score']}/{q['full_score']} - {q['ai_feedback'][:40]}")
    print("✅ 学员查询结果通过")

    print("\n" + "="*60)
    print("  端到端测试全部通过 ✅")
    print("="*60)

