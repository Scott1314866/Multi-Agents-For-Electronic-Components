# STEP Human-in-the-Loop 验证报告

- 日期：2026-09-24（Asia/Shanghai）
- 范围：验证 HITL 新节点、真实 LangGraph 中断/恢复、API 状态同步及 PostgreSQL checkpoint 跨进程读写。**不重跑六张 sample，也不将人工取消描述为样图生成成功。** baseline 报告保留在同目录 `report.md`。
- 结果：MemorySaver/合成逻辑测试、API/模拟测试通过；真实 PostgreSQL/API 合成封装暂停和跨进程恢复后显式取消通过。原生入口的首轮 PostgreSQL 写入曾失败，发现并记录 Windows Proactor 事件循环问题，修复 Selector 入口后复测通过。

## 测试环境与执行方式

- Python 3.12.13：`C:\Users\scott\AppData\Local\Temp\step-agent-e2e-20260924\Scripts\python.exe`，隔离临时 venv，使用既有 `ima-agent` site packages，补装 `langgraph-checkpoint-postgres==3.1.2`。
- 独立逻辑/拓扑回归命令：

  ```powershell
  & "$env:TEMP\step-agent-e2e-20260924\Scripts\python.exe" -m pytest -q `
    tests/test_step_human_nodes.py `
    tests/test_step_image_agent.py::test_graph_exposes_state_prompt_separation_and_view_loop `
    tests/test_step_image_agent.py::test_golden_reference_node_is_only_reachable_after_rendering
  ```

  结果：**19 passed**，1 条既有 Pydantic 配置弃用警告。
- API 代理报告 `tests/test_step_human_api.py`：**16 passed**（Mock SQL + MemorySaver/真实 LangGraph 节点，不连接 PostgreSQL）。
- CLI/docs 代理报告：**11 passed**，覆盖合成 CLI/MockTransport 与内存 interrupt；另有 1 项 Selector Runner mock 验证。未运行真实 sample。
- 实际数据库/API 使用 `python -m backend.step_main`；该入口仅监听 `127.0.0.1:8000`。用户现存本地测试用户只用于正常 `/auth/login`，凭据值未写入报告。人工回答仅针对临时 1×1 PNG 的“取消任务”，不含任何真实图纸判断。
- `.env.local` 由程序读取 PostgreSQL 参数；报告不含其值。

## 独立逻辑测试覆盖

新增 [test_step_human_nodes.py](../../../tests/test_step_human_nodes.py)，并只调整 [test_step_image_agent.py](../../../tests/test_step_image_agent.py) 中图拓扑断言。

覆盖项：

- 真实 LangGraph `MemorySaver` + `interrupt` + `Command(resume=...)` 在同一 `thread_id` 的暂停、恢复及 state 回读。
- 开始阶段始终询问封装；`provide`、`auto`、`cancel` 三种答案。
- 无效答案被拒绝并再次 interrupt；同一 thread 后续合法答案可继续；`human_history` 持久保留。
- 清晰高置信身份直接绕过路由询问；身份歧义、低置信、分类歧义、封装冲突及未实现模板均触发询问。
- 人工改变器件模板后分类选择保留，Jev 不得覆盖；高置信识别与人工封装一致时不重复询问，Jev 路由受请求封装约束。
- Golden 匹配结果免人工审核；需审核结果暂停并分别处理 approve/reject；缺 STEP 文件、四视图或验证通过标志时拒绝进入审核。
- 审核历史、封装历史及无法批准状态的校验。
- 图片 graph 拓扑含 `START -> ask_package`、`detect_views -> confirm_template -> jev_route_template`、`finalize_result -> review_result`。

## 真实 PostgreSQL/API 跨进程暂停与恢复

### 首轮入口问题

初次使用 Selector 修复前的 `step_main` 入口，以合成 1×1 PNG 测试，任务 `a80cda5c-6379-4e93-9be8-878d58bb4c57` 失败。真实异常为：

```text
psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.
```

`uvicorn.run(..., loop="asyncio")` 在当前 Windows / Uvicorn 版本覆盖了入口设置的 Selector policy。任务终态为 `failed`，checkpoint 不可读。随后入口改用 `asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)` 启动 Uvicorn，使用新任务重新验证；没有复用或修改首轮任务。

### 第二轮成功流程

第二个合成输入文件 `hitl-synthetic-selector.png`（1×1 PNG）经真实 STEP 上传 API 创建任务：

- drawing_id：`08449522-a434-4bcf-b779-b2a6f06096ad`
- 第一个服务进程将任务保存为 `awaiting_input`，pending 阶段为 `package`。
- 第一个进程的 checkpoint_id：`1f1b8055-1d88-631e-8000-2051eea14537`；PostgreSQL `checkpoints` 有 2 行，`next_nodes=["ask_package"]`。
- 读取的 checkpoint state `image_path` 与 `step_drawings.source_image_path` 一致；pending interrupt ID 与 API、DB 中保存的 ID 一致。
- 停止第一个服务进程，再通过正式入口启动新进程。第二进程的 authenticated GET 读回相同 drawing_id、checkpoint_id、pending interrupt、package 阶段及 `awaiting_input` 状态，checkpoint 可读。这证明暂停 state 与待答 interrupt 可跨进程从 PostgreSQL 恢复。
- 使用无效回答 `approve` 提交封装阶段，API 返回 422；任务继续保持 `awaiting_input`，interrupt ID 未变。
- 测试者对合成任务显式提交 `{"action":"cancel","comment":"synthetic cross-process HITL test cancellation"}`，API 返回 202。重复提交同一 interrupt 回答返回 409。
- 终态 API/业务记录为 `stopped`，LangGraph state/result 为 `cancelled`。新 checkpoint_id：`1f1b805a-c674-6e91-8001-d0ec9f9f58d4`；共 3 行 checkpoints、9 行 checkpoint_writes。state `human_history` 追加 `package:cancel`、回答用户 ID 和时间，业务表历史一致；`pending_input=null`，无 next nodes、无 interrupt。任务无 error、无 STEP/preview 产物，符合显式取消预期。

此测试在封装询问处结束，没有读取合成 PNG 的视觉内容、调用后续 LLM 或生成 CAD 模型。

## 数据库迁移与 baseline 保持情况

- 正式 `step_main` 启动时运行了幂等 `run_migrations()`；原 `step_drawings_status_check` 被替换为 `step_drawings_status_hitl_check`，允许 `awaiting_input` 与 `stopped` 等任务状态。迁移后约束定义已只读核实。
- 原有七条 sample baseline 记录仍为 `ai_processing`，没有被这轮 HITL 测试更新。此前创建的 `TR-00002.png` 重复提交记录仍单独存在，沿用 baseline 报告说明。
- 本轮另保留 2 条 synthetic 记录：首轮 Selector 前任务 `failed`（记录 Proactor 错误），第二轮测试任务 `stopped`（明确取消）。
- 测试服务已停止；本轮没有对 sample 图纸提交人工回答，也没有声称 sample 生成功能通过。

## 本轮读取或核对的源码

`backend/step_main.py`、`backend/db/migrations.py`、`backend/api/v1/step.py`、`backend/agents/step/persistence.py`、`backend/agents/step/graph.py`、`backend/agents/step/state.py`、`backend/agents/step/human_nodes.py`、`backend/agents/step/image_nodes.py`、`backend/agents/step/families/taxonomy.py`、`backend/agents/step/families/registry.py`、`backend/core/logger.py`、相关测试文件。测试改动限于新增 `tests/test_step_human_nodes.py` 与指定的 `tests/test_step_image_agent.py` 图拓扑断言。
