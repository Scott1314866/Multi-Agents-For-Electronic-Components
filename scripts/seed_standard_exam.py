"""
把「标准试卷」（题目 + 正确答案 + 得分点）灌进数据库 exams / questions / scoring_points。
这是第 6 章批改前的「数据准备」——库里有了这份标准卷，load_questions_meta 才有东西可读。

- 用 SQLAlchemy + text()，复用项目的 AsyncSessionLocal（与课件正文节点代码同一种写法）
- 幂等：先按固定 exam_id 删除（级联清掉 questions/scoring_points），再插入
用法：
    conda activate edu_agent
    python scripts/seed_standard_exam.py
"""
import asyncio
import json

from sqlalchemy import text
from backend.dependencies import AsyncSessionLocal

TENANT = "tenant_default"

# ── 固定 ID（幂等 + 方便测试脚本引用）──────────────────────────
EXAM_ID = "e0000001-0000-0000-0000-000000000001"
Q = {  # question_no -> 固定 question_id
    1: "e0000001-0000-0000-0000-0000000000a1",
    2: "e0000001-0000-0000-0000-0000000000a2",
    3: "e0000001-0000-0000-0000-0000000000a3",
    4: "e0000001-0000-0000-0000-0000000000a4",
    5: "e0000001-0000-0000-0000-0000000000a5",
}

# ── 标准试卷：Java 基础测试卷（5 题，总分 35）────────────────────
QUESTIONS = [
    # (no, type, content, correct_answer, score, knowledge_tag)
    (1, "single_choice",
     "下列关于 Java 中 final 关键字的说法，正确的是（ ）\n"
     "A. final 修饰的变量不能重新赋值\nB. final 修饰的方法可以被重写\n"
     "C. final 修饰的类可以被继承\nD. 以上都不对",
     "A", 5, "Java基础"),
    (2, "multi_choice",
     "下列关于 Java 集合的说法正确的是（多选）\n"
     "A. ArrayList 底层是数组\nB. LinkedList 支持随机访问\n"
     "C. HashMap 允许 null key\nD. HashSet 不允许重复元素",
     "ACD", 6, "Java集合"),
    (3, "judge", "判断：Java 中 int 类型的成员变量默认值是 0。", "正确", 4, "Java基础"),
    (4, "short_answer", "请解释 Spring IOC 的概念及其核心作用。", "", 10, "Spring IOC"),
    (5, "code",
     "编写一个 Java 方法 fib(int n)，计算斐波那契数列第 N 项（fib(0)=0, fib(1)=1）。",
     # 代码题 correct_answer 按 schema 约定存 JSON 测试用例（Judge0 跳过，当前不执行）
     json.dumps([{"input": "10", "expected_output": "55"},
                 {"input": "1", "expected_output": "1"}], ensure_ascii=False),
     10, "算法-递归"),
]

# ── 第4题（简答）得分点，共 10 分 ─────────────────────────────
SCORING_POINTS = [
    ("正确说明 IOC 是「控制反转/依赖注入」的设计思想", 4),
    ("说明对象的创建与依赖管理交给 Spring 容器统一负责", 3),
    ("说明 IOC 的作用：降低耦合、便于测试与维护", 3),
]


async def main():
    # AsyncSessionLocal 是项目里统一的异步会话工厂，连接配置已在项目内配好，这里直接用
    async with AsyncSessionLocal() as session:
        # 幂等：先删（questions / scoring_points 靠外键 ON DELETE CASCADE 自动清掉）
        await session.execute(text("DELETE FROM exams WHERE id = :id"), {"id": EXAM_ID})

        await session.execute(
            text("""INSERT INTO exams (id, tenant_id, title, description, is_active)
                    VALUES (:id, :tenant, :title, :desc, TRUE)"""),
            {
                "id": EXAM_ID, "tenant": TENANT,
                "title": "Java 基础测试卷（第6章批改标准卷）",
                "desc": "覆盖单选/多选/判断/简答/代码五种题型，总分 35",
            },
        )

        for no, qtype, content, correct, score, tag in QUESTIONS:
            await session.execute(
                text("""INSERT INTO questions
                            (id, tenant_id, exam_id, question_no, question_type,
                             content, correct_answer, score, knowledge_tag)
                        VALUES (:id, :tenant, :exam_id, :no, :qtype,
                                :content, :correct, :score, :tag)"""),
                {
                    "id": Q[no], "tenant": TENANT, "exam_id": EXAM_ID, "no": no,
                    "qtype": qtype, "content": content, "correct": correct,
                    "score": score, "tag": tag,
                },
            )

        for desc, pts in SCORING_POINTS:
            await session.execute(
                text("""INSERT INTO scoring_points
                            (question_id, point_desc, point_score, is_active)
                        VALUES (:qid, :desc, :pts, TRUE)"""),
                {"qid": Q[4], "desc": desc, "pts": pts},
            )

        await session.commit()  # SQLAlchemy 需要显式提交，写入才会落库

        # 自检
        total = (await session.execute(
            text("SELECT SUM(score) FROM questions WHERE exam_id = :id"),
            {"id": EXAM_ID})).scalar()
        n_q = (await session.execute(
            text("SELECT COUNT(*) FROM questions WHERE exam_id = :id"),
            {"id": EXAM_ID})).scalar()
        n_sp = (await session.execute(
            text("SELECT COUNT(*) FROM scoring_points WHERE question_id = :qid"),
            {"qid": Q[4]})).scalar()

    print(f"✅ 已写入标准卷：exam_id={EXAM_ID}")
    print(f"   题目 {n_q} 道，总分 {total}；简答题得分点 {n_sp} 个")
    print("   把测试脚本里的 EXAM_ID 设成上面这个，即可提交批改")


if __name__ == "__main__":
    asyncio.run(main())
