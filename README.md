# 器件图纸智能解析与 STEP 数模生成

面向电子元器件工程图的 AI 辅助建模服务。当前可运行的后端以 STEP Agent 为主：接收二维图纸，提取并复核尺寸证据，按器件族生成 CAD 模型及 STEP 文件和多视图预览；遇到封装、路由、尺寸或审核问题时暂停任务，等待操作员通过 API 回答。任务状态与 LangGraph checkpoint 保存在 PostgreSQL 中，可在服务重启后继续。

## 功能概览

- **图纸到 STEP**：识别工程图视图、文字和尺寸，融合尺寸证据，选择已实现的器件族模板，并构造和验证 CAD 模型。
- **人工参与**：在封装选择、模板路由、缺少尺寸或最终审核阶段暂停，记录操作员输入；人工补充的尺寸与图纸证据分开保存。
- **任务持久化**：使用 PostgreSQL 保存任务、checkpoint 和回答记录；启动时迁移所需业务表并恢复可恢复任务。
- **鉴权与租户隔离**：登录 API 签发 JWT，任务查询和文件下载按租户隔离。
- **辅助模块**：代码库还包含 PCB、问答、符号、检索和 PDF 处理相关模块；它们不属于当前默认 FastAPI 应用所挂载的 API。

## 技术栈

Python、FastAPI、LangGraph、PostgreSQL、SQLAlchemy、Pydantic Settings、CadQuery，以及兼容 OpenAI API 的 DeepSeek 和 Qwen 模型服务。

## 项目结构

```text
backend/
  api/v1/             FastAPI 路由：认证和 STEP 任务
  agents/step/        STEP 图纸解析、证据复核、器件族模板和 CAD 执行
  agents/pcb/         PCB Agent 模块
  agents/symbol/      符号 Agent 模块
  agents/pdf/         PDF Agent 说明及相关资源
  core/               模型工厂、日志、重试和异常处理
  db/                 数据库迁移
  step_main.py        STEP 应用、启动与后台任务恢复
docs/
  step-human-interaction.md  STEP 人工交互、API、恢复行为及终端验证说明
scripts/
  init_db_package.sql        PostgreSQL 初始表结构
tests/                       自动化测试和回归样例
sample/                      本地样例文件（被 Git 忽略）
output/                      生成文件（被 Git 忽略）
```

## 环境准备

- Python 3.11 或更高版本；CadQuery 及其几何内核需要兼容的运行环境。
- PostgreSQL，且应用账号有权创建/更新 LangGraph checkpoint 表和 STEP 业务表。
- DeepSeek 对话模型和 Qwen 视觉模型的 API 凭据及 OpenAI 兼容接口地址。
- 项目依赖。仓库根目录的 `requirements.txt` 是当前环境的包版本清单，格式不是 pip requirements 格式，不能直接用 `pip install -r requirements.txt` 安装。部署时请根据目标环境准备可安装的依赖清单；当前项目还依赖 `langgraph-checkpoint-postgres` 和 `psycopg[binary,pool]`。

## 配置

在项目根目录创建本地 `.env.local`。该文件已被 Git 忽略，请勿提交真实凭据。至少配置数据库账号、JWT 签名密钥，以及模型服务参数：

```dotenv
DB_HOST=localhost
DB_PORT=5432
DB_NAME=agent
DB_USER=your_db_user
DB_PASSWORD=your_db_password

JWT_SECRET_KEY=replace_with_a_long_random_secret

DEEPSEEK_API_KEY=your_deepseek_api_key
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL_CHAT=your_chat_model

QWEN_API_KEY=your_qwen_api_key
QWEN_BASE_URL=https://your-qwen-compatible-endpoint/v1
QWEN_MODEL_VL=your_vision_model
```

配置由 [backend/config.py](backend/config.py) 的 `Settings` 定义，也可通过环境变量提供。默认数据库名为 `agent`，默认端口为 `5433`；上例显式使用 PostgreSQL 常见的 `5432`，请按实际实例修改。

首次启动前，在目标数据库执行一次初始表结构脚本：

```powershell
psql -h localhost -p 5432 -U your_db_user -d agent -f scripts/init_db_package.sql
```

该脚本创建用户、STEP 任务等业务表。应用启动时的迁移会更新已有的 `step_drawings` 表，但不会代替初始建库。LangGraph PostgreSQL saver 所需表会由其组件初始化。

登录前还需在 `users` 表准备有效用户，并存储 bcrypt 密码哈希；应用没有内置默认账号。

## 启动服务

本机使用 conda 的 `ima-agent` 环境，可直接运行下面的启动脚本（数据库容器应已启动）：

```powershell
.\scripts\start_project.ps1
# 在后台运行：
.\scripts\start_project.ps1 -Background
```

脚本使用 `D:\Anaconda_envs\envs\ima-agent\python.exe`，也可通过 `-PythonPath` 指定其他解释器。启动前检查运行依赖，并通过 Python 的 `-s` 参数禁用用户目录包，避免用户目录中旧版 LangGraph 覆盖 conda 环境的依赖。手动启动已激活的 conda 环境时使用 `python -s -m backend.main`。

在 Windows PowerShell 中，从项目根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -s -m backend.main
```

以上 venv 命令仅创建虚拟环境；请先在目标环境安装项目及 CadQuery、PostgreSQL saver 等运行依赖。若已有可用的项目环境，直接使用该环境中的 Python 启动即可。

当前标准入口为 `backend.main`，兼容入口 `backend.step_main` 和 `backend.new_main` 也可用：

```powershell
python -s -m backend.step_main
```

服务默认监听 `127.0.0.1:8000`。启动时会执行数据库迁移和 STEP 任务恢复；健康检查为 `GET /health`，OpenAPI 文档为 `/docs`，最简上传页面为 `/api/v1/step/ui`。

对话页会自动更新当前任务的执行日志，显示各步骤的开始、完成、等待确认和耗时。日志随任务存入 PostgreSQL，刷新或切换会话后仍可查看；默认展示最近 12 条，可展开此前日志。状态接口返回 `execution_logs`，每个任务最多保留 600 条。仅记录功能上线后实际执行的步骤，不补写旧任务日志。后台启动日志使用 UTF-8 并立即刷新，输出在 `tmp/server.stdout.log` 和 `tmp/server.stderr.log`。

## STEP API 快速流程

除健康检查外，以下接口需要 `Authorization: Bearer <access_token>`。先调用登录接口取得 token：

```http
POST /api/v1/auth/login
Content-Type: application/json

{"username":"your_username","password":"your_password"}
```

随后上传图纸并读取任务状态：

```http
POST /api/v1/step/generate
Authorization: Bearer <access_token>
Content-Type: multipart/form-data

message=请根据这张工程图生成 STEP 模型
file=@drawing.png
```

接口返回 `202 Accepted` 和 `drawing_id`。客户端轮询 `GET /api/v1/step/drawings/{drawing_id}`；若返回 `pending_input`，应向用户展示当前问题，并把其回答提交到响应中的 `human_input_url`。回答必须带当前的 `interrupt_id`：

```http
POST /api/v1/step/drawings/{drawing_id}/human-input
Authorization: Bearer <access_token>
Content-Type: application/json

{"interrupt_id":"<当前问题 ID>","answer":{"action":"provide","package_type":"TSSOP-16"}}
```

成功产物可通过响应中的鉴权地址下载：STEP 文件、等轴图、前视图、俯视图和右视图。任务状态、可用回答和终态说明见 [STEP 人工交互文档](docs/step-human-interaction.md)。

## 测试与开发

在包含测试依赖的项目环境中运行：

```powershell
python -m pytest
```

涉及真实模型服务的测试可能需要额外凭据或外部服务；具体测试标注见测试文件和 `pytest.ini`。STEP API 端到端验证脚本、人工交互示例及其退出码说明见 [STEP 人工交互文档](docs/step-human-interaction.md)。

## 当前边界

- STEP 自动化只覆盖已实现且能通过证据门禁的器件族；不支持或证据不足的任务会报告原因、询问补充信息或停止，不应将人工批准视为模型校验通过。
- 使用人工补充尺寸生成的结果会进入审核流程；请在批准前核对尺寸证据、验证结果、STEP 文件和预览图。
- `backend/api/router.py` 中的 QA、PCB 等路由不等同于默认应用已启用的接口。默认应用仅注册认证和 STEP 路由。
- `sample/`、`output/` 和本地环境文件被 Git 忽略，不会随仓库提交。
