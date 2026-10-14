# 安装、配置与 API 使用指南

本指南保留项目的环境配置、启动、STEP 与 Symbol API、Cadence 本机／SSH 执行说明。项目概览和入口导航见 [仓库首页](../README.md)。

## 功能概览

- **图纸到 STEP**：识别工程图视图、文字和尺寸，融合尺寸证据，选择已实现的器件族模板，并构造和验证 CAD 模型。
- **人工参与**：在封装选择、模板路由、缺少尺寸或最终审核阶段暂停，记录操作员输入；人工补充的尺寸与图纸证据分开保存。
- **任务持久化**：使用 PostgreSQL 保存任务、checkpoint 和回答记录；启动时迁移所需业务表并恢复可恢复任务。
- **鉴权与租户隔离**：登录 API 签发 JWT，任务查询和文件下载按租户隔离。
- **统一工作台**：默认应用挂载认证、STEP、PCB、Symbol 和工作台 API；问答、检索和 PDF 处理模块不作为独立 API 挂载。

## 技术栈

Python、FastAPI、LangGraph、PostgreSQL、SQLAlchemy、Pydantic Settings、CadQuery，以及兼容 OpenAI API 的 DeepSeek 和 Qwen 模型服务。

## 项目结构

```text
backend/
  api/v1/             FastAPI 路由：认证、STEP、PCB、Symbol 和工作台
  agents/step/        STEP 图纸解析、证据复核、器件族模板和 CAD 执行
  agents/pcb/         PCB Agent 模块
  agents/symbol/      符号 Agent 模块
  agents/pdf/         PDF Agent 说明及相关资源
  core/               模型工厂、日志、重试和异常处理
  db/                 数据库迁移
  main.py             统一应用、启动与三个 Agent 的后台任务恢复
docs/
  step-human-interaction.md  STEP 人工交互、API、恢复行为及终端验证说明
scripts/
  init_db_package.sql        PostgreSQL 初始表结构
tests/                       自动化测试和回归样例
sample/                      本地样例文件（被 Git 忽略）
output/                      生成文件（被 Git 忽略）
```

## 环境准备

公司工作机的部署使用根目录 `docker-compose.yml` 管理依赖服务，应用及 Cadence 在 Windows 宿主机运行。邀请码注册、外部数据卷配置和现有容器迁移步骤见 [公司试用版部署说明](company-pilot部署说明.md#docker-compose-依赖服务配置)。

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

配置由 [backend/config.py](../backend/config.py) 的 `Settings` 定义，也可通过环境变量提供。默认数据库名为 `agent`，默认端口为 `5433`；上例显式使用 PostgreSQL 常见的 `5432`，请按实际实例修改。

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

唯一后端入口为 `backend.main`；应用、生命周期和启动逻辑均在 `backend/main.py`。从项目根目录启动：

```powershell
python -s -m backend.main
```

服务默认监听 `0.0.0.0:8000`。本机首页进入 [统一工程工作台](http://127.0.0.1:8000/api/v1/workbench/ui)，集中提交 STEP、Symbol 与 PCB 任务，选择 Cadence 设备并处理人工核对。其他局域网设备使用 `http://运行服务的电脑IPv4地址:8000/`，不能把 `0.0.0.0` 当作访问地址；启动脚本会显示实际局域网地址。三个 Agent 沿用原有检查点与恢复机制；Symbol、PCB 共用同设备执行队列。接口与部署说明见 [工作台文档](../frontend/workbench/README.md)。健康检查为 `GET /health`，OpenAPI 文档为 `/docs`。

公司试用版通过工作机的局域网地址访问，工作台路径为 `/api/v1/workbench/ui`，邀请码注册路径为 `/api/v1/auth/ui`。管理员可生成、限定名额和撤销邀请码；普通用户加入公司共享工作区。部署、账号、备份与上线验收见 [公司试用版说明](company-pilot部署说明.md)。数据库就绪检查为 `GET /api/v1/workbench/ready`。

PCB 独立页面为 [PCB 封装工作台](http://127.0.0.1:8000/api/v1/pcb/ui)。前端在 `frontend/pcb/`，API 在 `backend/api/v1/pcb.py`；与统一工作台共用 `frontend/shared/` 的核对交互和样式，直接调用 PCB 图片输入接口。详见 [PCB 前端文档](../frontend/pcb/README.md)。

对话页会自动更新当前任务的执行日志，显示各步骤的开始、完成、等待确认和耗时。日志随任务存入 PostgreSQL，刷新或切换会话后仍可查看；默认展示最近 12 条，可展开此前日志。状态接口返回 `execution_logs`，每个任务最多保留 600 条。仅记录功能上线后实际执行的步骤，不补写旧任务日志。后台启动日志使用 UTF-8 并立即刷新，输出在 `tmp/server.stdout.log` 和 `tmp/server.stderr.log`。

页面通过 `include_checkpoint=false` 轮询已保存的业务状态，避免重复编译工作流和加载完整证据；提交人工回答时仍会锁定记录并验证实时检查点。视觉 JSON 输出预算为 4096 token，截断时最多重试一次至 8192 token，整次调用最多等待 120 秒，不接受截断结果。此类失败可从当前视图分析恢复，复用 OCR 和此前回答。可选参考模型检索超过 20 秒后继续本地图纸建模；CPU OCR 引擎跨任务复用，首次加载和复杂图纸识别仍可能较慢。

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

成功产物可通过响应中的鉴权地址下载：STEP 文件、等轴图、前视图、俯视图和右视图。任务状态、可用回答和终态说明见 [STEP 人工交互文档](step-human-interaction.md)。

## Symbol Agent 与 Cadence

启动后端后，打开 [Symbol 符号工作台](http://127.0.0.1:8000/api/v1/symbol/ui)，使用现有账号登录。页面代码位于 `frontend/symbol/`，由后端提供同源静态页面，无需 npm 构建或单独启动前端。STEP 页侧栏也提供 Symbol 入口，两者共用当前浏览器会话的登录凭据。

工作台同时支持 PDF 手册（最多 64 MB）与单张封装引脚图 PNG、JPG、WEBP、BMP（最多 32 MB、3200 万像素），自动按文件类型选择流程，型号与封装可选。PDF 经手册解析、引脚核对及生成确认后生成 OLB/DSN；可选“仅解析引脚与封装章节”。图片直接读取图中的引脚编号、名称、所在边和坐标，还原主体比例、引脚顺序及对齐关系，通过自检后自动生成。清晰的图片无需重复复核或生成前确认；只有看不清的名称或位置需要补充。图片流程不调用 MinerU，未标注的电气类型使用 PAS，不从名称推断。

页面提供分页历史、任务恢复、布局缩放、引脚清单、自检、OLB/DSN/Tcl/日志和原始文件下载。填写中的表单在轮询时保留；任务切换会忽略旧请求的响应；登录过期时返回登录页。历史任务可继续查询、恢复及下载原始文件。

PDF 流程保留自动复核：大小写/空白差异、明确的 EP/EPAD 别名，以及由编号、图中位置和手册通道描述共同验证的 OCR 字形差异可自动处理，并记录依据。真实名称/位置冲突、低置信度和证据不足的差异才进入人工表单。已验证的独立裸露焊盘若未画在外围图中，会默认放在底部并注明；缺脚和编号错误仍由自检拦截。

历史任务支持删除和恢复。删除设置 `symbol_drawings.deleted_at`，从正常列表移除，保留业务记录、checkpoint 与文件；“已删除”列表可查看、下载回溯记录或恢复任务。运行中的任务拒绝删除，已删除任务不会被启动恢复或接受人工输入。删除/恢复操作的时间与操作者保存在 `package_params.lifecycle_events`。

Symbol 的每步 state 和人工暂停点由 PostgreSQL `AsyncPostgresSaver` 保存，thread ID 为 `symbol:<drawing_id>`，存储在 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes`；任务摘要、最新引脚/布局、自检和确认记录在 `symbol_drawings.package_params`。图片识别的原始结果和几何坐标也保存到 state。原始 PDF/图片、归一化图片、MinerU 中间文件及 OLB/DSN/Tcl/日志保存在本机任务目录 `output/symbol_agent/<drawing_id>/`，数据库保存路径。工作台的“回溯记录”下载最新完整 state、业务摘要及最多 100 个历史 checkpoint 索引；超过时返回游标供继续读取，索引包含各快照的完整 state 查询地址。

回溯 API 均按登录租户校验，已删除任务也可读取：`GET /api/v1/symbol/drawings/{id}/trace` 导出摘要与最新 state；`GET .../{id}/checkpoints?limit=20&before={checkpoint_id}` 从新到旧分页列出快照；`GET .../{id}/checkpoints/{checkpoint_id}` 读取某一步完整 state。`DELETE .../{id}` 删除可见任务，`POST .../{id}/restore` 恢复；`GET /api/v1/symbol/drawings?deleted=true` 查询已删除列表。不存在或属于其他任务的 checkpoint 不返回伪造的空快照。

图片任务通过自检后，agent 自动生成 Tcl，调用 Cadence 的 `tclsh.exe` 和 DBO DLL，生成原生 `.OLB` 符号库及放置一个符号实例的 `.DSN` 原理图。PDF 任务保留生成前确认。无需手动打开 Tcl。每次执行使用独立目录；退出码为 0、日志出现 PASS、两个产物的二进制格式验证通过，才将任务标记为完成。Tcl 和 `capture.log` 可下载；缺少安装环境会停止并保留 Tcl，许可失败或超时会返回失败原因和日志。

使用者在运行 agent 的 Windows 设备上安装 Cadence，并具有可用的 Capture 许可。默认 `SYMBOL_CAPTURE_MODE=local`，会探测常见 `C:/Cadence/SPB_*`、`D:/Cadence/SPB_*` 安装。其他路径可在 `.env.local` 设置：

```dotenv
SYMBOL_CAPTURE_MODE=local
CADENCE_INSTALL_DIR=C:/Cadence/SPB_17.2
SYMBOL_CAPTURE_TIMEOUT=300
```

也可用 `CADENCE_TCLSH` 指定运行器、`CADENCE_DBO_DLL` 指定 DLL；未指定 DLL 时使用运行器同目录的 `orDb_Dll_Tcl64.dll`，保持同一版本。配置修改后重启后端。

如果 agent 与 Cadence 位于不同的 Windows 设备，可由服务端选择 SSH 模式：

```dotenv
SYMBOL_CAPTURE_MODE=ssh
SYMBOL_SSH_HOST=192.168.1.20
SYMBOL_SSH_USER=your_username
SYMBOL_SSH_PORT=22
SYMBOL_SSH_PRIVATE_KEY=C:/Users/your_username/.ssh/cadence_key
SYMBOL_SSH_KNOWN_HOSTS=C:/Users/your_username/.ssh/known_hosts
SYMBOL_SSH_TCLSH=C:/Cadence/SPB_17.2/tools/bin/tclsh.exe
SYMBOL_SSH_DBO_DLL=C:/Cadence/SPB_17.2/tools/bin/orDb_Dll_Tcl64.dll
```

SSH 模式需要 agent 设备的 OpenSSH `ssh/scp` 客户端，以及远端 Windows OpenSSH 服务、已配置的公钥登录和已核对的主机密钥。使用可供后台进程读取的私钥；agent 不交互询问密码，不自动接受未知主机。远端任务默认保存在 `%LOCALAPPDATA%/symbol-agent/runs/<唯一任务目录>`；可通过 `SYMBOL_SSH_REMOTE_ROOT` 指定绝对路径。Tcl 上传和产物下载校验 SHA256；OLB/DSN 和日志回传到当前任务目录。许可服务应由使用者正常配置，agent 不重启许可服务。

Symbol API 位于 `/api/v1/symbol`。状态接口返回 `capture_execution`、`capture_log_tail` 和产物列表；用 `/api/v1/symbol/drawings/{drawing_id}/artifacts/{artifact_name}` 下载 `tcl`、`olb`、`dsn` 或 `capture_log`，沿用鉴权和任务目录校验。

| 接口 | 用途 |
| --- | --- |
| `POST /api/v1/symbol/generate` | multipart 上传 PDF/图片 `file`，可附 `device`、`package`、`strict_pages`（仅 PDF），返回 202 和任务 ID |
| `GET /api/v1/symbol/drawings?limit=20&offset=0` | 当前租户的分页历史，返回 `items` 与 `has_more` |
| `GET /api/v1/symbol/drawings/{drawing_id}` | 状态、`pending_input`、引脚 `pins`、布局 `layout`、自检、确认历史和下载链接 |
| `POST /api/v1/symbol/drawings/{drawing_id}/human-input` | 提交当前 `interrupt_id` 和阶段对应的 `answer`，继续工作流 |
| `GET /api/v1/symbol/drawings/{drawing_id}/source` | 下载原始 PDF 手册或封装图片 |
| `GET /api/v1/symbol/drawings/{drawing_id}/artifacts/{artifact_name}` | 下载任务文件 |

页面资源无需登录即可加载；任务、原图和产物接口都需要 Bearer Token。下载由前端携带鉴权头发起，不把 token 放进 URL。源文件与产物均检查所属租户及任务目录，任务 ID 在查询数据库前校验 UUID。

## 测试与开发

测试范围、环境配置、真实模型回归及端到端验证方式见 [测试指南](testing.md)。在包含测试依赖的项目环境中运行默认离线测试集：

```powershell
python -s -m pytest -m "not integration"
```

STEP API 端到端验证脚本、人工交互示例及其退出码说明见 [STEP 人工交互文档](step-human-interaction.md)。

## 当前边界

- STEP 自动化只覆盖已实现且能通过证据门禁的器件族；不支持或证据不足的任务会报告原因、询问补充信息或停止，不应将人工批准视为模型校验通过。
- 使用人工补充尺寸生成的结果会进入审核流程；请在批准前核对尺寸证据、验证结果、STEP 文件和预览图。
- `backend/api/router.py` 中的 QA 等路由不等同于默认应用已启用的接口。默认应用注册认证、STEP、PCB 和 Symbol 路由。
- `sample/`、`output/` 和本地环境文件被 Git 忽略，不会随仓库提交。
