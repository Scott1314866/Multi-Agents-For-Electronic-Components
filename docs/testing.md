# 测试指南

本文说明当前仓库的自动化测试、可选真实服务回归和端到端验证方式。自动化测试覆盖 STEP 图纸处理、人工交互与恢复、PCB 封装图、符号生成契约，以及模型输出截断和任务进度等场景。

## 环境要求

- 使用安装了项目依赖的 Python 环境。CadQuery 相关测试还需要可运行的 CadQuery/OpenCascade 环境。
- 根目录的 `requirements.txt` 是环境版本清单，不是可直接交给 pip 的 requirements 文件。
- `backend.config` 在模块导入时加载配置。运行测试前，需要在 `.env.local` 或环境变量中提供 `DB_USER`、`DB_PASSWORD`、`JWT_SECRET_KEY`；单元测试使用模拟存储或进程内 checkpoint 时不需要连接 PostgreSQL。
- 默认单元测试不需要调用在线大模型。真实模型和服务回归需要额外的凭据、网络和样例文件，见下文。

Windows PowerShell 示例（使用当前已配置的 `ima-agent` 环境）：

```powershell
$env:DB_USER = "your_db_user"
$env:DB_PASSWORD = "your_db_password"
$env:JWT_SECRET_KEY = "local-test-secret"
D:\Anaconda_envs\envs\ima-agent\python.exe -s -m pytest -m "not integration"
```

也可以把这些配置放入根目录的 `.env.local`。该文件已被 Git 忽略，不要提交真实密码或 API Key。`-s` 会禁用用户 site-packages，避免用户目录中的同名包覆盖当前环境依赖。

## 常用命令

运行默认的离线测试集：

```powershell
python -s -m pytest -m "not integration"
```

运行单个测试文件或关注的测试组：

```powershell
python -s -m pytest tests/test_pcb_package.py
python -s -m pytest tests/test_symbol_contract.py
python -s -m pytest tests/test_symbol_contract.py tests/test_symbol_workflow.py tests/test_symbol_api.py tests/test_symbol_capture.py
python -s -m pytest tests/test_step_human_api.py tests/test_step_progress.py tests/test_step_latency.py
```

查看测试收集结果但不执行：

```powershell
python -s -m pytest --collect-only
```

在已激活的 `ima-agent` 环境中，把 `python` 替换为该环境的解释器路径；若使用项目启动脚本指定的解释器，可运行 `D:\Anaconda_envs\envs\ima-agent\python.exe -s -m pytest ...`。

## 测试覆盖

| 测试文件 | 覆盖内容 | 外部依赖 |
| --- | --- | --- |
| `tests/test_step_image_agent.py`、`test_step_drawing_golden_set.py`、`test_step_family_evidence_regressions.py`、`test_step_evidence_fusion_regressions.py` | 尺寸证据、器件族路由、Feature IR、CAD 几何与回读、图纸门禁 | 本地 Python/CadQuery；大多数模型调用使用 mock 或预置提取数据 |
| `tests/test_step_human_api.py`、`test_step_human_nodes.py`、`test_step_dimension_input.py` | API 暂停与恢复、回答校验、租户隔离、人工尺寸来源 | 模拟数据库/API；无需真实 PostgreSQL 服务 |
| `tests/test_step_progress.py`、`test_step_latency.py`、`test_step_preview_downloads.py`、`test_main_entrypoint.py` | 执行日志、超时和截断重试、预览下载及统一应用入口 | 本地运行时依赖；不调用真实模型 |
| `tests/test_pcb_package.py` | PCB 封装参数派生、规则断言、Allegro skill 文件生成、路由契约 | 单元测试不启动 Allegro；实机 PCB 工具链验证另行进行 |
| `tests/test_symbol_contract.py` | PDF 表格解析、引脚布局、Capture Tcl 生成与既有 Golden 文件对拍 | Golden fixture；不启动 Capture |
| `tests/test_symbol_image.py` | 图片解码与校验、真实图片 Golden 答案、主体比例、对侧对齐、清晰图片自动生成、仅询问不确定引脚、缺脚门禁 | 本地 Pillow、进程内 graph；模型与 Capture 使用模拟结果 |
| `tests/test_symbol_workflow.py`、`test_symbol_api.py` | 型号/封装状态传递、选列、真实 graph 暂停恢复、裁决写回、自检门禁、API 校验与租户隔离、停止原因、Tcl 下载、PDF 工作线程 | 进程内 checkpoint、模拟存储与工具；不连接真实 PostgreSQL/MinerU/视觉模型/Cadence |
| `tests/test_symbol_capture.py` | 本机/SSH 执行入口、同版本 DLL、Tcl 路径转义、独立目录、防旧产物误判、超时、许可失败、传输校验、非交互 SSH | 模拟子进程与 SSH；不调用真实 Cadence |
| `tests/test_symbol_capture_live.py` | 真实 agent 布局、Capture、收尾节点生成原生 OLB/DSN 并保存日志 | 已配置的本机 Cadence 或 Windows SSH Cadence；显式启用后运行 |
| `tests/test_agentic_web_search.py` | 搜索编排、结果处理和 STEP 参考模型解析 | 网络调用使用 mock |
| `tests/test_step_image_agent_dip_regression.py` | DIP 图纸真实识别、路由、建模、STEP 回读 | 真实 OCR、Qwen、Jev、Web Search、CadQuery 和本地 DIP 图片；默认跳过 |

`pytest.ini` 默认只收集 `tests/` 下的项目测试，避免收集 `tmp/` 中临时部署工具的上游测试。`integration` 标记仅用于需要真实外部服务的回归；默认命令 `-m "not integration"` 会排除它们。

### Symbol Agent 优化回归（2026-10-08）

符号流程现在先确认型号和封装，再选引脚表；人工裁决会写回实际引脚，更换封装会重选表格和引脚图。自检问题及历史答案会保留，自检仍有错误时进入 `stopped_check_failed`，不会调用 Capture。API 会在认领任务前校验回答，并返回自检报告和停止原因。PDF 定位和图页筛选在渲染工作线程执行；没有 Cadence 时仍生成、登记并允许下载 Tcl。Capture 还会检查输出文件非空、校验产物文件名并转义 Tcl 字符串。

本次运行结果：符号专项 **56 passed**（原有 16 项契约测试 + 新增 40 项工作流/API 回归）；项目离线测试 **355 passed、2 failed、1 deselected**。两项非符号失败为 `test_ti_qfn_package_reference_uses_matching_official_step`（STEP 参考搜索返回 `None`）和 `test_golden_set_paths_exist`（缺少 `sample/picture/ADC建模参考图2.png`）。相关 STEP 实现、测试和样例配置未在本次修改。

上述结果验证离线逻辑、真实 LangGraph interrupt/resume、Golden Tcl 字节一致性和模拟 API；未验证真实 PostgreSQL、MinerU、视觉模型服务或 Cadence 执行。完整离线测试的 JUnit 记录位于 `output/symbol_agent/tests/offline-regression.xml`。

### 真实 Cadence 生成回归

先按照 README 配置本机或 SSH 模式及有效许可，再运行：

```powershell
$env:RUN_SYMBOL_CAPTURE_LIVE = "1"
python -s -m pytest tests/test_symbol_capture_live.py -m integration -q -s
Remove-Item Env:RUN_SYMBOL_CAPTURE_LIVE
```

测试使用 ADS1115 Golden 引脚数据，直接调用 agent 的布局、Capture 和收尾节点，不需要 MinerU 或视觉模型。产物和 `agent_result.json` 保存到 `output/symbol_agent/live_regression/<唯一任务目录>`；输出目录会打印在测试结果中。该测试默认跳过，不会在普通离线测试中连接设备。

2026-10-09 已通过远端 Windows Cadence 17.2 完成此回归：真实生成 OLB、DSN，回传执行日志；退出码为 0，下载 SHA256 与远端一致，原生文档头通过校验。这验证 Capture 执行链路，不代表完整 PDF 识别链路或真实数据库 API 已完成实机验证。

本次符号离线测试 **74 passed**，真实 Capture 测试 **1 passed**；合并应用入口回归共 **86 passed**。应用启动测试已补齐 PCB/Symbol 恢复任务的 mock，验证三个恢复入口各调用一次，避免离线测试误连数据库。项目离线回归 **373 passed、2 failed、2 deselected**；两项既有失败仍为 TI STEP 参考搜索返回 `None`、Golden 样例缺少 `sample/picture/ADC数模参考图2.png`。记录分别位于 `output/symbol_agent/tests/capture-focused-regression.xml` 与 `output/symbol_agent/tests/capture-offline-regression.xml`。

## Symbol API 与前端回归

后端页面入口为 `/api/v1/symbol/ui`，源码在 `frontend/symbol/`。接口回归包括 multipart 字段绑定、任务 ID 校验、分页历史、布局与引脚投影、原始 PDF 的租户/目录隔离，以及固定名单中的静态资源。

```powershell
python -s -m pytest tests/test_symbol_api.py tests/test_symbol_workflow.py tests/test_main_entrypoint.py -q
node --test frontend/tests/symbol-api.test.mjs
```

`frontend/tests/symbol-ui.test.mjs` 使用 Playwright Chromium 和本地 mock API，测试真实浏览器中的上传、六阶段人工表单、刷新保留输入、过期问题、切换任务时忽略旧请求、登录过期、文本安全渲染、鉴权下载、页面重载恢复、布局缩放，以及移动端宽度。没有 Playwright 时浏览器测试会标记跳过；要运行它，可在 frontend 安装 Playwright 和 Chromium，或使用已有运行时：

```powershell
cd frontend
npm install --no-save playwright
npx playwright install chromium
npm test
```

若 Playwright 位于外部 Node 运行时，将 `NODE_PATH` 指向其 `node_modules`，然后在项目根目录运行 `node --test frontend/tests/*.test.mjs`。截图保存于 `output/symbol_agent/ui/`，使用测试数据，不代表真实 PDF 识别结果。测试不调用真实数据库、模型或 Cadence；真实生成链路使用上一节的 Capture 实机回归。

2026-10-09 本次运行：前端 **8 passed**（含真实 Chromium 的两项工作流测试，未跳过）；符号后端离线测试 **81 passed**。项目离线回归 **380 passed、2 failed、2 deselected**，失败仍为既有 TI STEP 参考搜索和 Golden 图片缺失。记录在 `output/symbol_agent/tests/ui-offline-regression.xml`。

2026-10-09 AD7682 解析修复：使用真实 MinerU 第 11–14 页表格缓存，以及使用者提供的 21 脚标准图，新增组合编号、型号名称列、相邻同编号续表、封装隔离、EPAD 校验、缺脚拦截与人工核对后布局的回归。运行 `python -s -m pytest tests/test_symbol_pin_tables.py tests/test_symbol_contract.py tests/test_symbol_workflow.py tests/test_symbol_api.py tests/test_symbol_capture.py -q`，**107 passed**。原始 OCR 的 `INO` 和手册名称 `EPAD` 在核对前保留；工作流测试显式裁决为标准图的 `IN0` 与 `EP`，验证左侧 1–10、右侧 21–11 的顺序，并保留生成前确认。记录为 `output/symbol_agent/tests/ad7682-regression.xml`；本次未调用真实 MinerU、视觉服务或 Cadence 生成。

随后优化自动复核：上述 AD7682 工作流测试现在直接通过规则校验到生成确认，无需逐条裁决 `INO/IN0` 和 `EPAD/EP`。新增 `tests/test_symbol_review_policy.py`，覆盖有证据的自动校正、仅询问剩余实质差异、审计记录、低置信度拒绝、真实通道差异、电气极性符号保留，以及完整外围引脚加独立 EPAD 时的布局规则；真实缺脚仍拦截。运行 `python -s -m pytest tests/test_symbol_review_policy.py tests/test_symbol_workflow.py tests/test_symbol_pin_tables.py tests/test_symbol_contract.py tests/test_symbol_api.py tests/test_symbol_capture.py -q`，**132 passed**。记录在 `output/symbol_agent/tests/automatic-review-regression.xml`；测试使用缓存与模拟视觉结果，不调用真实服务或 Cadence。

2026-10-09 删除与回溯：后端 **155 passed**（Symbol 各套件及 STEP 应用入口），前端 **11 passed**（含四个真实 Chromium 测试，未跳过）。覆盖可恢复删除、租户隔离、运行中/锁占用拒绝删除、已删除任务的投影及恢复保护、完整 state 导出、历史分页、错误 checkpoint 拒绝和前端鉴权下载。JUnit 为 `output/symbol_agent/tests/task-trace-regression.xml`。另在实际 PostgreSQL 中验证 CRCW040210K0FKED 失败任务的删除：6 个 checkpoint 数量未变，原始 PDF 的 SHA256 未变，完整快照导出到 `output/symbol_agent/trace/CRCW040210K0FKED-trace.json`；没有调用模型或 Cadence 重跑该任务。

## 真实模型回归

### Symbol 图片输入回归（2026-10-09）

新增封装引脚图片流程，支持 PNG/JPG/WEBP/BMP 上传。图片主链不经过 PDF 定位、MinerU、表格合并或二次视觉复核；图中信息清晰时自检后直接生成，低置信度名称或未知侧别只询问对应引脚。PDF checkpoint 保留原节点和恢复流程。

符号后端专项及 STEP 应用入口 **175 passed**，包含图片流程、API 原图下载与租户隔离、无效文件及配额校验，记录为 `output/symbol_agent/tests/image-input-regression.xml`。前端 **12 passed**（含五个真实 Chromium 测试），验证图片直接生成、实际 SVG 引脚对齐与可读性、原图下载，以及历史 PDF 的人工暂停、恢复、删除和下载。

使用使用者提供的 `tests/golden/symbol/mcp3021/pin-diagram.png` 完成真实模型、PostgreSQL 和远端 Windows Cadence 17.2 的完整提交验证。任务 `0e7ff9b4-4527-4745-abba-27f97bbc529d` 自动完成，没有人工 interrupt；自检 100 分，保存 11 个 checkpoint。识别为 MCP3021 / SOT-23A：左侧 1 VDD、2 VSS、3 AIN，右侧 5 SCL、4 SDA，1/5 与 3/4 分别对齐。SSH 运行退出码 0，日志 PASS，OLB 6656 字节、DSN 8704 字节，均通过原生 OLE 文件头验证。完整识别、坐标、执行记录和产物位于 `output/symbol_agent/0e7ff9b4-4527-4745-abba-27f97bbc529d/`。

### Symbol PDF 与图片双输入回归（2026-10-09）

同一提交接口与上传区同时接受 PDF 手册（64 MB）和封装图片（32 MB），自动选择对应工作流。PDF 保留 `strict_pages`、引脚核对及生成前确认；图片仍自动生成，忽略 PDF 专用选页参数。下载按来源返回原始 PDF 或图片。

后端专项 **180 passed**，记录为 `output/symbol_agent/tests/pdf-image-input-regression.xml`。新增真实 PDF 文件上传/下载、PDF 类型和人工生成策略传递、独立大小上限、文件签名与格式拒绝，以及旧客户端选页参数绑定验证。前端 **12 passed**（含五个真实 Chromium 测试），其中 PDF 测试完成上传及六种人工停点；图片测试从 PDF 切换为 PNG 后直接生成，确认 PDF 选项不带入图片请求。页面截图在 `output/symbol_agent/ui/symbol-upload.png`、`symbol-pdf-upload.png` 与 `symbol-image-result.png`。本次回归未重跑真实模型或 Cadence，上一节的 MCP3021 实机产物保留。

### EMI8142 PDF 实机回归（2026-10-09）

输入使用者提供的 WJ251969 PDF（9 页），目标 EMI8142MUTAG / XDFN-10。首轮虽然读出了完整 10 脚，但错误采用第 1 页电气示意图，把 3、8 接地脚放在底部；该轮未生成，作为失败基线保留（任务 `9b837ba5-e3be-45dc-9d03-8e09daaac3c0` 已取消）。真实 OCR 还将 I/O 写成 1/0 或 1/O。

修复后优先第 8 页 XDFN10 物理封装图，附第 2 页名称表进行独立复核，并将完整的 DFN 双排拓扑转换为符号左右排列。第二轮自动读出左侧 1–5、右侧 10–6，3/8 位于两边中间，独立复核清晰通过，八个信号的提取类型为 IO。标准图仅用于验收，未输入 PDF 提取或视觉识别调用。

手册名称保留为 In_1+、Out_1+、VN 等；位置验证通过后，按使用者明确提供的标准图将显示标签统一为 IN1+、OUT1+、GND，记录每个原值与调整原因。此步使用了标准图的名称，属于已提供的用户核对信息，不代表模型自动读出了 GND。原始名称仍在 `table_pins` 和调整前 checkpoint 中。

最终任务 `eb5d67c5-5dd4-4da4-9249-a5df7e424704` 完成真实 MinerU、视觉服务、PostgreSQL 与远端 Cadence 17.2 链路：自检 100 分、10 个最终名称/位置与标准相符、23 个 checkpoint、Capture 退出码 0/PASS，OLB/DSN 原生 OLE 文件头通过。产物和 `reference-comparison.json` 在 `output/symbol_agent/eb5d67c5-5dd4-4da4-9249-a5df7e424704/`。新增 `tests/test_symbol_emi8142.py`，符号专项及 STEP 入口 **189 passed**，JUnit 为 `output/symbol_agent/tests/emi8142-regression.xml`。

### STEP 图纸真实模型回归

该测试读取本地生成或提供的二值图：

```text
output/drawing_to_step/image_agent/DIP__2/vision/preprocessed_binary.png
```

确认图片存在、模型服务配置可用后，在 PowerShell 中显式启用：

```powershell
$env:RUN_STEP_IMAGE_LIVE_REGRESSION = "1"
python -s -m pytest -m integration tests/test_step_image_agent_dip_regression.py
```

运行可能产生模型服务费用，并写入 `output/` 中间文件。测试默认跳过是为了避免普通单元测试意外发起在线调用。

## Golden Set 与 API 端到端验证

`scripts/test_step_drawing_golden_set.py` 会处理 `tests/golden/step_drawing_golden_set.json` 指定的图纸。样例图片位于 Git 忽略的 `sample/` 目录，因此需要先准备本地样例；没有复用已有提取结果时，视觉提取会调用 Qwen。输出报告和 STEP 产物写入 `output/drawing_to_step/golden_set/`：

```powershell
python -s scripts/test_step_drawing_golden_set.py
```

真实 API 验证需要先启动后端、准备 PostgreSQL 业务表和 checkpoint、创建有效 API 用户，并配置模型服务。上传/验证脚本支持人工交互，任务和产物会持久化到数据库与 `output/`。完整命令、输入格式和退出码见 [STEP 人工交互指南](step-human-interaction.md) 中的“终端用法”和“API 调用”。

## 结果与故障定位

- 测试失败时先记录失败测试名和完整 traceback；区分本地依赖、Golden 样例缺失、数据库连接失败和模型服务错误。
- 外部模型回归失败时，检查对应 API Key、Base URL、模型名、网络连通性和样例图路径；不要通过填入未由图纸或操作员提供的尺寸来绕过失败门禁。
- Golden Set、端到端任务会写入 `output/` 和数据库。测试后按项目数据保留要求处理生成结果；不要将 API 凭据、用户数据或生产任务产物放入提交。
- 本文记录可执行的测试入口和依赖边界，不代表这些测试在每次文档更新时均已运行。


## 2026-10-14 仓库更新验证

项目离线测试使用 `python -s -m pytest -m "not integration"`：692 项通过，2 项既有失败，2 项集成测试未运行。失败为 TI QFN 封装参考搜索回归返回 `None`，以及 Golden 样例缺少 `sample/picture/ADC数模参考图2.png`；两项在前面的历史记录中已有记载，本次没有跳过或改写这些断言。JUnit 保存在本地 `output/github-update-20261014/project-backend-tests.xml`。

前端 `node --test frontend/tests/*.test.mjs`：32 项通过，包含实际 Chromium 页面回归。已固定 pytest 默认收集目录为 `tests/`，不再误收集 `tmp/` 中部署工具的上游测试；运行缓存、临时凭据和生成产物不随代码提交。此前发现的原生 CAD 进程退出异常仍需单独修复，断言通过不代表原生进程稳定。
