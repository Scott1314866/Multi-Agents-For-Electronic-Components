# STEP Agent sample 与 Auth 端到端测试报告

- 日期：2026-09-24（Asia/Shanghai）
- 结论：Auth 认证用例通过；6 张样图都成功进入 STEP 上传 API 并写入 `step_drawings`，但后台生成全部卡在 `ai_processing`。未生成 STEP / preview 文件，PostgreSQL 中没有对应 state/checkpoint，端到端生成未通过。
- 源码及配置：未修改应用代码、schema 或已有用户。测试写入的 6 个样图任务记录和上传图片保留在库与 `output/step_agent`，作为失败证据。

## 测试环境与边界

- Windows / PowerShell；Python 3.12.13，使用现有 `ima-agent` 环境依赖建立 `%TEMP%\step-agent-e2e-20260924` 临时虚拟环境，额外安装 `langgraph-checkpoint-postgres==3.1.2`。未安装进项目依赖或现有 conda 环境。
- `.env.local` 只由应用读取以连接既有 PostgreSQL；未打印或记录任何凭据值。
- `backend.main` 无法导入：`backend/api/router.py` 加载 `backend.agents.qa` 时抛 `ModuleNotFoundError`，仓库中该模块不存在。因此用临时 ASGI runner 挂载仓库里的真实 `auth.router` 与 `step.router`，路径仍为 `/api/v1/auth/*` 和 `/api/v1/step/*`，服务只监听 `127.0.0.1:8000`。这验证了真实路由处理逻辑，但没有覆盖完整 `backend.main` 挂载链路。测试服务已停止。
- 既有 `scripts/seed_data.py` 提供本地开发用户；使用 `Lee01` 通过真实 `POST /api/v1/auth/login`，无直接读取或复用数据库密码哈希。样图上传均返回 HTTP 202，任务 GET 返回 HTTP 200。
- Auth 没有公开注册接口。根据用户授权，用 `auth.py` 同一 `pwd_context` 为随机密码哈希，临时插入了唯一测试用户，执行真实登录与授权检查后立即按用户 ID 删除。用户名：`step_auth_test_24a56fd6888c`；user_id：`3bc50645-4910-43c1-814c-0ae186ee666c`。随机密码未输出或写入报告。

## Auth 验证结果

| 场景 | 结果 | HTTP |
|---|---:|---:|
| 错误密码登录 | 通过，拒绝 | 401 |
| 正确密码登录 | 通过 | 200 |
| 有效 token 调用 `/api/v1/auth/me` | 通过，返回匹配 user_id | 200 |
| 不带 token 调用 `/me` | 通过，拒绝 | 401 |
| 无效 token 调用 `/me` | 通过，拒绝 | 401 |
| 临时测试用户清理 | 已删除 | — |

## 六张样图结果

每张图的 `thread_id` 均按 `step:image:<drawing_id>` 检查。状态与 PostgreSQL 数据在测试服务停止后只读核验。

| 图片 | drawing_id | 上传 / 状态 API | DB 状态 | checkpoint / writes / blobs | 产物 |
|---|---|---|---|---|---|
| `ADC_图纸10.png` | `ab837174-a95b-408b-b9c1-02538d2f45cc` | 202 / 200 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |
| `ADC_图纸11.png` | `8beffb3d-4052-4567-afe5-6157777954ed` | 202 / 200 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |
| `MCU_图纸8.png` | `73f3e0d6-e1dc-4cf0-a895-22e854bfdb25` | 202 / 200 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |
| `MISC-00008.png` | `33881049-984c-47c3-be72-366f962e718a` | 202 / 200 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |
| `TR-00002.png` | `9a688bb1-77c3-4550-bc86-da3e75b9f242` | 202 / 至少一次 200 状态轮询 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |
| `TR-00002.png`（重复提交） | `0c48a70a-197f-481b-9908-81c9424a1403` | 202 / 200 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |
| `TR-00007-3.png` | `5779d117-0eb7-4827-8ef7-7c8a2e67435d` | 202 / 200 | `ai_processing` | 0 / 0 / 0 | 无 STEP、无 preview；仅上传图存在 |

> 测试中途停止重复等待后，`TR-00002.png` 被再次上传，产生额外任务记录。故总计是 6 个样图、7 条 STEP 任务记录；重复记录如上单独列出。没有手动删除任务或 DB 数据。

## 数据库与错误证据

- PostgreSQL `step_drawings` 查询显示：上述所有记录的 `source_image_path` 文件存在，`output_path`、`preview_path`、`error_msg` 均为 NULL，状态均为 `ai_processing`。
- 针对每个 `step:image:<drawing_id>` 查询 `checkpoints`、`checkpoint_writes`、`checkpoint_blobs` 均为 0。测试后的这三张表总记录数也是 0。`checkpoint_migrations` 表原先存在 10 条迁移记录；这不代表本次工作流写入了 state。
- API 日志显示每个后台任务提交后立即发生：`step.background_task_unhandled | error="'_Logger' object has no attribute 'exception'"`。定位到 `backend/api/v1/step.py` 的异常处理调用了 `logger.exception(...)`，而 `backend/core/logger.py` 的 `_Logger` 未实现 `exception` 方法；该二次异常发生在标记任务失败之前，所以 API/DB 留下 `ai_processing` 与空 `error_msg`。这条日志揭示了错误处理器自身的异常，**没有暴露最初触发后台异常的原始原因**。因此不能据此断言异常发生在模型推理或 checkpoint saver 的哪一步。
- 由于所有 checkpoint 为 0，本次无法证明 LangGraph state/checkpoint 可从 PostgreSQL 写入再读回；也没有生成文件可供下载验证。

## 执行命令摘要

1. 只读检查 `.env.local` 配置的 PG 连通性及表计数。
2. 在临时目录建立隔离 Python venv，并启动临时 runner：`uvicorn step_test_app:app --app-dir <TEMP> --host 127.0.0.1 --port 8000`。
3. Auth 使用正式 `/api/v1/auth/login`、`/api/v1/auth/me`；STEP 样图使用现有 `backend.api.v1.verify_step_e2e`（部分任务因已确认卡住，轮询缩短到 10 秒），剩余图片仍经真实 `POST /api/v1/step/drawings` 与 GET 状态路由提交和核验。
4. 使用 `psycopg` 对 `step_drawings`、`checkpoints`、`checkpoint_writes`、`checkpoint_blobs` 做只读核对。

## 本次读取的相关源码

`backend/api/v1/step.py`、`backend/api/v1/verify_step_e2e.py`、`backend/api/v1/auth.py`、`backend/api/router.py`、`backend/main.py`、`backend/dependencies.py`、`backend/agents/step/persistence.py`、`backend/agents/step/graph.py`、`backend/core/logger.py`、`scripts/seed_data.py`、`requirements.txt`。`.env.local` 仅检查了键名和由程序读取，未将值写入报告。
