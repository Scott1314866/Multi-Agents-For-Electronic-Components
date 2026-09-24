#!/usr/bin/env python3
"""
简历审查 Agent —— 端到端本地验证脚本（免命令行版）
直接运行即可，无需输入任何参数。
"""

import sys
import time
import httpx

# ============ 在这里直接配置所有参数 ============
FILE_PATH = "/Users/ligang/Desktop/LiveEduAgent/samples/resume_sample.pdf"          # 改成你的 PDF 绝对路径或相对路径
BASE_URL = "http://localhost:8000"        # 后端地址
USERNAME = "student01"                    # 登录用户名
PASSWORD = "Student@123456"               # 登录密码
POLL_INTERVAL = 3.0                       # 轮询间隔（秒）
POLL_TIMEOUT = 180.0                      # 最长等待（秒）
# =============================================


def login(client: httpx.Client, username: str, password: str) -> str:
    """登录，返回 access_token。"""
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        sys.exit(f"❌ 登录失败（{resp.status_code}）：{resp.text}")
    print(f'response: {resp.json()}')
    token = resp.json().get("access_token")
    if not token:
        sys.exit(f"❌ 登录响应里没有 access_token：{resp.text}")
    print(f"✅ 登录成功（用户：{username}）")
    return token


def upload(client: httpx.Client, headers: dict, file_path: str) -> str:
    """上传 PDF，返回 review_id。"""
    try:
        f = open(file_path, "rb")
    except OSError as e:
        sys.exit(f"❌ 打不开文件 {file_path}：{e}")
    with f:
        resp = client.post("/api/v1/resume/upload", headers=headers,
                           files={"file": (file_path.split("/")[-1], f, "application/pdf")})
    if resp.status_code != 202:
        sys.exit(f"❌ 上传失败（{resp.status_code}）：{resp.text}")
    data = resp.json()
    review_id = data["review_id"]
    print(f"✅ 上传成功 → review_id = {review_id}")
    print(f"   状态：{data['status']}（审查在后台进行，开始轮询…）\n")
    return review_id


def poll(client: httpx.Client, headers: dict, review_id: str,
         interval: float, timeout: float) -> dict:
    """轮询查询，直到 done / failed / 超时。"""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        resp = client.get(f"/api/v1/resume/reviews/{review_id}", headers=headers)
        if resp.status_code != 200:
            sys.exit(f"❌ 查询失败（{resp.status_code}）：{resp.text}")
        data = resp.json()
        status = data["status"]
        print(f"  第 {attempt} 次轮询 → status: {status}")
        if status in ("done", "failed"):
            return data
        time.sleep(interval)
    sys.exit("⏰ 超时：仍未完成。请检查后端日志（搜 resume.background_task_failed / 是否配了可用的 DEEPSEEK_API_KEY）。")


def print_report(data: dict) -> None:
    """漂亮地打印审查报告。"""
    if data["status"] == "failed":
        print("\n❌ 审查失败：", data.get("error_msg", "（无错误信息）"))
        return

    print("\n" + "=" * 48)
    print("            简历审查报告")
    print("=" * 48)
    print(f"综合得分：{data.get('weighted_score')} / 100\n")

    print("【六维度评分】")
    for d in data.get("dimension_scores", []):
        weight = d.get("weight", 0)
        print(f"  · {d.get('dimension')}：{d.get('score')} 分（权重 {int(weight * 100)}%）")
        for issue in d.get("issues", []):
            print(f"      - 问题：{issue}")
        for sug in d.get("suggestions", []):
            print(f"      - 建议：{sug}")

    print("\n【问题诊断】")
    issues = data.get("issues", [])
    if not issues:
        print("  （无）")
    for it in issues:
        loc = it.get("location", "")
        print(f"  [{it.get('priority', '').upper()}] {it.get('description', '')}"
              + (f"（{loc}）" if loc else ""))
        if it.get("suggestion"):
            print(f"        → {it['suggestion']}")

    summary = data.get("summary") or {}
    print("\n【整体评价】")
    print("  亮点：", "；".join(summary.get("highlights", [])) or "（无）")
    print("  核心改进：", "；".join(summary.get("core_improvements", [])) or "（无）")
    print("  综合评语：", summary.get("overall_comment", "（无）"))
    print("  岗位匹配：", summary.get("fit_assessment", "（无）"))
    print("=" * 48)


def main() -> None:
    # 直接使用上面定义的全局常量，不再需要 argparse
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
        token = login(client, USERNAME, PASSWORD)
        print(f'token = {token}')
        headers = {"Authorization": f"Bearer {token}"}
        review_id = upload(client, headers, FILE_PATH)
        data = poll(client, headers, review_id, POLL_INTERVAL, POLL_TIMEOUT)
        print_report(data)


if __name__ == "__main__":
    main()