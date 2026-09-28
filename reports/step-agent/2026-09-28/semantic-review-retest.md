# STEP semantic-review 真实复测

日期：2026-09-28（Asia/Shanghai）

## 结果摘要

本轮仅新建并运行一条 ADC10 任务：`424a6e7a-e742-49ba-a5c5-0968ed63b0b2`。它经正常登录、API 上传和 package 阶段 `action=auto` 后，在视图分类响应的 schema 校验处以 `failed` 结束。`overall_confidence` 收到字符串 `"high"`，而 schema 要求数值。

该任务**没有进入尺寸语义融合或 semantic-review 节点**：checkpoint 的 `next_nodes=[]`、无 interrupt；review attempts/history/pending queue、semantic collisions、dimension gate 均未写入；没有 `feature_ir`、STEP 或 preview 产物。因此，本次不能说明修复后的语义复核是否被触发或有效。失败任务和证据保留，未重试、未修改状态。

## 服务与前置状态

- 重启前监听 `127.0.0.1:8000` 的服务为 PID 14848，父 PID 7560，运行 `backend.step_main`。
- 重启前只读数据库检查未发现 `ai_processing` 任务；有 5 个 `awaiting_input` 和 1 个 `pending_review` 暂停任务。
- 按授权停止旧服务进程树一次，以项目 `.venv\Scripts\python.exe -m backend.main` 启动。新 launcher PID 为 12192，监听端口的实际服务 PID 为 36592；`GET /health` 返回 200，mode 为 `step`。没有再次重启。
- 新服务日志单独保留在 [stdout 日志](semantic-review-retest-server.stdout.log) 和 [stderr 日志](semantic-review-retest-server.stderr.log)，未覆盖旧日志。
- 认证使用已有活动用户 Lee01，未创建临时账号。本轮没有在报告或日志中记录密码。

## ADC10 任务证据

| 项目 | 观测结果 |
|---|---|
| Drawing ID | `424a6e7a-e742-49ba-a5c5-0968ed63b0b2` |
| 原图 | `sample/picture/ADC_图纸10.png` |
| SHA-256 | `e953c4140a7ee5f4ca6ed238b2f5c6252e4756ca3e036ddcb132a2c01495a2c3`；与上传文件、state `image_meta.sha256` 和 DB `package_params.image_sha256` 一致 |
| package 输入 | `action=auto`，已记录在 state 和 DB `human_history`；没有再次提交 |
| API / DB 状态 | `failed`，`needs_review=false`，`output_path`、`preview_path` 均为空 |
| PostgreSQL checkpoint | `checkpoint_id=1f1bae42-965d-69a0-8007-0617caeb9dc4`；checkpoint row count 为 9；API、DB 投影与只读 PG snapshot 的 ID 一致 |
| checkpoint 执行位置 | `next_nodes=[]`，`interrupts=[]`，`state.status=failed` |
| schema 错误 | `QwenViewClassificationResult.overall_confidence`：`float_parsing`，输入值 `'high'` 为字符串 |
| review 状态字段 | `semantic_review_attempts`、`semantic_review_pending_regions`、`semantic_review_history`、`semantic_collisions`、`dimension_gate` 均为空/未设置 |
| 生成物 | 仅存在 `all_evidence.json` 路径记录；无 Feature IR、STEP 或四视图预览可下载 |

错误发生于 `detect_views_node`：模型分类响应直接交给 `_parse_model_json(..., QwenViewClassificationResult)` 校验。Raw 响应无法从该任务的 state 或登记产物追溯：失败前只登记了 `all_evidence`，`view_classification.json` 仅在分类解析成功后写入。故目前证据只能确认错误字段和值，不能确认原始响应的完整 JSON 或请求 ID。

## 旧任务前后对照

重启前记录了原五个暂停任务以及独立任务 `27085...` 的 checkpoint ID、pending interrupt、next node 和 human history。重启并完成本次 ADC10 任务后再次只读核验，值保持一致：

| Drawing ID | DB 状态 | Checkpoint ID | Interrupt（阶段） | Next node |
|---|---|---|---|---|
| `d399c6b9-261c-4be8-879a-22efa7369fc8`（ADC11） | pending_review | `1f1bad88-6b03-60c4-801f-eea16fcbdecc` | `8d4147e05780cd1a3efdf06d7c83b5d5`（review） | `review_result` |
| `4f51a4c0-2153-4f7d-b022-20d2b84cca75`（MCU） | awaiting_input | `1f1bad80-be1d-649b-8006-ec38c1d97149` | `a134f991b704934dbf1038abe31c69a4`（routing） | `confirm_template` |
| `e29e0a96-5329-44f5-bacf-2e8d79e82720`（MISC） | awaiting_input | `1f1bad83-c43e-6468-8006-5f1a0345f057` | `a05bb34946e87d5e5c153e1719f47075`（routing） | `confirm_template` |
| `379cafbf-6373-4acd-8881-09e147b41be7`（TR-00002） | awaiting_input | `1f1bad84-42c7-6a78-8006-61322e0564dd` | `2a189c1475480bf088744525e2a9641b`（routing） | `confirm_template` |
| `71bae597-28a6-4a80-be3c-8b1417e6a589`（TR-00007-3） | awaiting_input | `1f1bad85-3f81-68d2-8006-0aed78ee5d37` | `02202500f11263e85e43c6817515cd52`（routing） | `confirm_template` |
| `27085bd7-b0dc-4cd6-b9ed-d7bfed6ddc88`（独立 ADC_图纸12） | awaiting_input | `1f1bad63-2c27-6801-8000-338c020e9921` | `ec9fe9963056703886b80e8f5c7dbfa6`（package） | `ask_package` |

六项的 `human_history` 与重启前相同；没有回答任何旧 routing/review/package interrupt，也没有 resume、重传或修改 `27085...`。此前 stopped 的 ADC10 记录亦未恢复或覆盖。

## 回归测试

以下是相互独立的 synthetic/API 回归命令，不代表真实 ADC10 的语义复核成功，也不是一次合并运行：

- `.\.venv\Scripts\python.exe -m pytest -q tests/test_step_preview_downloads.py tests/test_step_evidence_fusion_regressions.py tests/test_step_family_evidence_regressions.py tests/test_step_human_api.py tests/test_step_human_nodes.py tests/test_step_image_agent.py`：**185 passed，4 warnings**。
- `.\.venv\Scripts\python.exe -m pytest -q tests/test_step_entrypoints.py`：**11 passed，1 warning**。
- `.\.venv\Scripts\python.exe -m pytest -q tests/test_step_semantic_review.py`：**13 passed，1 warning**，包括 MemorySaver 同线程分段恢复及候选审计测试。

## 结论

真实端到端复测验证了新服务入口、正常 API 登录/上传、package auto 历史记录、图片 SHA 和 PostgreSQL state/checkpoint 写入与读取；未到达本轮目标 semantic-review 节点。当前阻塞是视图分类的模型响应 schema 不匹配。应保留本任务为失败证据；修复后需按新 drawing ID 重新运行，不能把 synthetic 回归结果计作本图通过。
