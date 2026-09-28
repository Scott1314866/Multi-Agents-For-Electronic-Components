# STEP Agent 人工交互

上传图片后，工作流先询问封装；遇到不明确的分类和需要审核的产物时再询问。每次问题均由 LangGraph `interrupt` 暂停，API 通过 PostgreSQL 中同一 `step:image:<drawing_id>` 的 checkpoint 恢复。

| 阶段 | 何时询问 | 允许的回答 |
| --- | --- | --- |
| `package` | 新任务开始时必问 | `provide` + `package_type`，或明确选择 `auto` 自动识别，或 `cancel` |
| `routing` | `jev_route_template` 前，分类不明确、置信度低于 0.80、存在歧义或与人工封装输入冲突 | `confirm` 接受 `suggested`；`change` + `family_id`（可附 `package_type`）；或 `cancel` |
| `review` | 生成并验证 STEP 和四视图后，需要人工审核时 | `approve` 或 `reject`，可附 `comment` |

`routing` 的可选模板从问题里的 `candidates` 读取。只有明确且已实现的候选模板才提供 `confirm`；`change` 也只能选择 `implemented=true` 的模板。目录中尚未实现的类别仍会展示其限制，不会自动换成其他类别。人工填写封装和路由不会补造 OCR 尺寸证据。缺少证据、模板不支持、构建失败等停止结果不能通过人工批准变成生成成功。

## 终端用法

当前机器已准备项目 `.venv`，继承现有 `ima-agent` 的 OCR/CAD 等依赖，并在项目环境内安装 PostgreSQL saver。直接启动后端：

```powershell
cd D:\WorkSpace\Xien\project_01
.\.venv\Scripts\python.exe -m backend.main
```

默认后端只挂载 Auth 和 STEP，旧简历审查、面试 API 已移除，不再依赖缺失的 QA 模块。服务监听 `127.0.0.1:8000`，启动时执行数据库迁移和任务恢复扫描。

`backend.main`、`backend.step_main`、`backend.new_main` 复用同一个应用和 `run_server()`；后两者保留为兼容入口。Windows 启动使用显式 Selector Runner 执行 Uvicorn `serve()`，保证 PostgreSQL saver 兼容。使用上述模块命令即可。

另开 PowerShell，使用 API 账号登录（不是 PostgreSQL 账号）。不设置 `STEP_PASSWORD` 时会隐藏输入密码：

```powershell
cd D:\WorkSpace\Xien\project_01
$env:STEP_USERNAME = "你的API用户名"
.\.venv\Scripts\python.exe -m backend.api.v1.verify_step_e2e --image "sample\picture\ADC_图纸10.png" --interactive
```

脚本打印完整问题后，实际使用者输入 `answer` JSON，例如封装阶段：

```json
{"action":"provide","package_type":"TSSOP-16"}
```

若省略 `--interactive`，脚本遇到人工问题会先核对 API、业务表和 checkpoint 暂停记录，再输出问题与 `drawing_id`，以退出码 **2** 结束，绝不自动代答。空行也保留暂停。之后继续同一任务：

```powershell
.\.venv\Scripts\python.exe -m backend.api.v1.verify_step_e2e --drawing-id "任务UUID" --interactive
```

脚本检查 **5 个产物：STEP、等轴图、前视图、俯视图、右视图**。每个下载均须返回 200、内容非空，且 SHA-256 与数据库路径对应的文件一致；同时核对 state/checkpoint 和业务表的状态、待答问题及产物路径。

退出码 **0** 表示成功终态且上述检查通过；**1** 表示验证失败、生成失败、拒绝或停止；**2** 表示仍待人工输入。`pending_review` 时也检查这 5 个候选产物，但仍保持暂停，不算完成。脚本须在后端所在机器运行，并读取相同 `.env.local`。记录和文件保留供事后检查。

仅运行本地图、不经过 API 的命令：

```powershell
.\.venv\Scripts\python.exe scripts/test_step_image_agent.py "sample\picture\ADC_图纸10.png" --interactive
```

本地脚本使用进程内 MemorySaver，省略 `--interactive` 时打印真实 interrupt 并退出 2；退出后内存 checkpoint 失效。需要数据库验证或跨进程恢复时使用上面的 API 脚本。

当前机器重建该环境的命令（依赖已有 `ima-agent`，不是全新机器安装清单）：

```powershell
& D:\Anaconda_envs\envs\ima-agent\python.exe -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install "langgraph-checkpoint-postgres==3.1.2" "psycopg[binary,pool]==3.3.5"
```

根目录 `requirements.txt` 目前是包版本表，不能直接作为 `pip install -r` 的输入。上述操作仅在 `.venv` 增加依赖，不更改既有 Conda 环境。

## API 调用

所有请求使用 `Authorization: Bearer <access_token>`。

1. `POST /api/v1/step/drawings`，multipart 字段 `file` 上传图片，返回 HTTP 202 和 `drawing_id`。
2. `GET /api/v1/step/drawings/{drawing_id}`，读取 `status`、`pending_input`、`checkpoint_id`、`next_nodes` 和产物地址。
3. 若有 `pending_input`，展示问题，收集实际使用者的回答，提交到 `human_input_url`。
4. 返回 202 后继续 GET，直到下一次问题或最终状态。

候选产物生成后，GET 返回以下 5 个鉴权下载地址；路径前缀均为 `/api/v1/step/drawings/{drawing_id}/artifacts/`：

| 产物 | 响应字段 | 路径末段 |
| --- | --- | --- |
| STEP | `step_file_url` | `step` |
| 等轴图 | `preview_urls.isometric` | `preview_isometric` |
| 前视图 | `preview_urls.front` | `preview_front` |
| 俯视图 | `preview_urls.top` | `preview_top` |
| 右视图 | `preview_urls.right` | `preview_right` |

旧 `preview_url` 和 `/artifacts/preview` 保留为等轴图兼容地址。审核问题也附带 `step_file_url`、`preview_urls` 和 `modeling_assumptions`，用于展示候选文件及模型简化限制。

暂停响应示意：

```json
{
  "drawing_id": "任务UUID",
  "status": "awaiting_input",
  "pending_input": {
    "interrupt_id": "本次问题ID",
    "stage": "package",
    "question": "这张工程图需要生成什么封装？",
    "options": ["provide", "auto", "cancel"]
  },
  "human_input_url": "/api/v1/step/drawings/任务UUID/human-input"
}
```

`POST /api/v1/step/drawings/{drawing_id}/human-input` 的请求体：

```json
{
  "interrupt_id": "从最新pending_input取得的ID",
  "answer": {"action":"provide","package_type":"TSSOP-16"}
}
```

其他阶段的 `answer` 示例（由使用者决定适用选项）：

```json
{"action":"auto"}
{"action":"confirm"}
{"action":"change","family_id":"ic/gullwing_ic","package_type":"TSSOP-16"}
{"action":"cancel","comment":"停止此任务"}
{"action":"approve","comment":"已查看STEP和预览"}
{"action":"reject","comment":"引脚结构需要重新检查"}
{"action":"provide","text":"本体高度=1.2 mm；引脚厚度=0.15 mm"}
{"action":"provide","values":{"housing_height":{"value":1.2,"unit":"mm"}}}
```

上述每行是独立示例。`auto` 仅用于封装阶段，`confirm/change` 仅用于路由阶段，`approve/reject` 仅用于审核阶段，`provide` 用于封装或尺寸补充阶段。尺寸回答可提交 `action=provide` 和自然语言 `text`；省略 action 时，系统把可识别的参数文本判定为 provide，把明确的取消用语判定为 cancel。更可靠的方式是用 `values`，只提交当前问题 `fields` 中列出的字段。长度默认 mm，也接受 cm、um、mil、inch 并转成 mm；引脚数必须是整数。尺寸答案存为 `human_input`，没有 OCR token 或图像坐标。人工输入的尺寸会导致最终产物进入人工审核。审核前应查看 `verification`、`golden_comparison`、预览和 STEP 文件。审核人和时间由认证 API 写入，客户端无需提交。

| API 状态 | 含义 |
| --- | --- |
| `ai_processing` | 正在执行或回答已接收，等待后台继续 |
| `awaiting_input` | 等待封装、路由或尺寸回答 |
| `pending_review` | 等待审核回答，可下载候选产物 |
| `completed` | 成功完成，自动检查未要求人工审核 |
| `reviewed` | 人工批准后的成功终态 |
| `rejected` | 人工拒绝，不能当作生成通过 |
| `stopped` | 人工取消、证据不足或不支持等停止结果 |
| `failed` | 执行失败，查看 `error_msg` |

格式或阶段不合法的回答返回 422；重复回答、过期 `interrupt_id` 或当前未等待输入返回 409；其他租户的任务返回 404。收到 409 后重新 GET 当前问题，不重复使用旧 ID。

## 自动语义复核

尺寸门禁因提取不足未通过，且能定位需要复核的视图时，`semantic_review_nodes.py` 逻辑上最多执行 **1 轮**自动复核（`MAX_SEMANTIC_REVIEW_ATTEMPTS = 1`），每个选中视图在正常运行时调用模型 1 次。门禁正常通过时不调用。模型只能重新绑定当前视图已有的 token/line，不注入或修改 OCR 数值，也不替代封装、路由或审核阶段的人工回答。

每个视图返回完整候选集合；证据引用或器件族合同校验失败时，保留该视图原候选。接受的候选须重新经过合并、融合、尺寸链和原尺寸门禁；复核后仍缺失可填写尺寸时，会进入 `dimensions` 人工询问，而不是立即终止。最多询问 3 轮；问题会列明缺失、冲突或低置信度字段及现有候选值。操作员填入后重新执行确定性派生、尺寸链和门禁；未解决的字段保留在失败报告中，API 状态为 `stopped`。

显式封装名 `TSSOP-16`、`LQFP64` 等仅可补充 `nominal_pin_count`，并记录为 `human_input` 来源；该解析使用有限的封装命名规则，不从任意器件型号末尾猜脚数。若明确封装脚数与图纸识别值不同，系统会询问操作员消歧。

回答槽位只接受当前问题明确列出的规范参数名。系统检查数值、正负范围、单位换算和整数脚数；每次回答保留被替换的图纸候选、操作者和时间。人工尺寸和图纸证据保持分开，Feature IR 会列出 `human_supplied_parameters`，并强制进入最终人工审核。

prepare 节点先把 `semantic_review_attempts = 1` 和待复核区域队列写入 PostgreSQL checkpoint；review 节点每个 superstep 只处理一个视图，提交结果与 `semantic_review_history` 后才处理下一个。恢复时不会重跑已提交的区域；若当前视图尚未提交就崩溃，其外部模型调用仍可能重放，因此不保证物理请求仅发生一次。

`semantic_before_review_1.json` 保存复核前结果；`semantic_review_1.json` 保存各视图原候选、新候选、接受与否及诊断信息。每次请求使用唯一 `request_id`，返回的 `raw_response` 在解析前记录，解析失败也保留原始响应审计。该功能尚未完成真实样图复测，不能据此认定 ADC10 已通过。

## state、checkpoint 和后台恢复

LangGraph 的完整 state 与待答 interrupt 保存在 PostgreSQL checkpoint 表；`step_drawings.package_params` 保存人工历史、待答问题和结果摘要。`checkpoint_migrations` 只是 saver schema 的版本记录，不代表任务执行过。被 interrupt 暂停的节点尚未提交返回值，所以 `state.status` 可能仍是前一节点状态；暂停与否应结合 `interrupts/next_nodes` 判断。

已持久化的暂停任务在后端重启后可继续：重新 GET 同一 `drawing_id`，取得当前问题，再提交人工回答。后端用同一 `thread_id` 和 `Command(resume={interrupt_id: answer})` 恢复。不要重新上传图片来代替恢复。

后端启动时会扫描 `ai_processing` 任务，以及 `package_params.projection_sync_failed=true` 的记录。三个兼容入口使用相同恢复逻辑。后台执行与恢复都持有同一任务的 PostgreSQL advisory lock；恢复扫描会跳过仍有其他进程执行的任务。取得锁后再次读取数据库状态，再按 checkpoint 决定下一步：

| checkpoint 情况 | 恢复行为 |
| --- | --- |
| 正在等待人工，且没有匹配的已接收回答 | 重建 `awaiting_input` 或 `pending_review` 状态，保留原问题 |
| 正在等待人工，且 `accepted_input.interrupt_id` 与当前问题匹配 | 用数据库里已认证、已校验的回答执行 `Command(resume=...)` |
| 已执行到后续问题，业务表还保留旧回答 | 保留新问题并清除旧回答，不把旧答案用于新问题 |
| 没有人工问题，但还有 `next_nodes` | 使用 `ainvoke(None)` 从 checkpoint 继续，不重新注入上传输入 |
| checkpoint 已经结束 | 只重建业务表结果、产物地址和审核记录，不再次运行模型 |
| 历史处理中任务没有 checkpoint | 标记为 `failed` 并写入诊断原因；不会自动创建或重新生成任务 |

服务只恢复实际使用者已经提交成功、保存到 `accepted_input` 的回答，不会自动选择 `auto`、`approve` 或填写封装。正常暂停的任务不需要启动扫描；重启后继续通过 GET 和人工回答接口操作即可。

图执行失败会记为 `failed`。图已有有效 checkpoint，但更新 `step_drawings` 摘要或读取执行结果暂时失败时，会保留 `ai_processing`，设置 `package_params.projection_sync_failed=true`，并在 `error_msg` 与 `projection_sync_error` 中记录原因，供下一次启动恢复。任务被取消时会在释放执行锁前记录 `worker_interrupted=true`；如果进程被强制结束，原有 `ai_processing` 记录同样会被下次启动扫描。

后台仍由 `asyncio.create_task` 执行，恢复调度在服务启动时触发，目前没有定时重试队列。节点中途退出后可能重新执行尚未提交 checkpoint 的该节点；已持久化的人工问题和终态不会被当作新任务重新开始。若暂时的数据库故障留下恢复标记，数据库恢复后重新运行 `.\.venv\Scripts\python.exe -m backend.main`，再查询同一 `drawing_id`。
