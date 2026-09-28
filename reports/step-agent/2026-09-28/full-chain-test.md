# STEP Agent 六张样图全链路运行记录

**最新结论：1 张有候选 STEP 待审核，1 张生成失败，4 张待路由确认。** ADC_图纸10 的两个独立任务均因提取证据冲突停止。ADC_图纸11 的 STEP 与四张视图预览均通过真实 API 下载及 SHA-256 对照，仍待人工审核。六张样图尚未全部跑通。当前 STEP 服务 PID 14848。

- 日期：2026-09-28（Asia/Shanghai）
- 初始提交阶段（08:43）：六张图各自进入 PostgreSQL `awaiting_input`，并等待封装选择。这是续跑前的历史快照；当前结论见页首及下方 ADC_图纸10 重跑和收尾核验记录。
- 初始服务：`python -m backend.step_main`，PID 31316；health 返回 `{"status":"ok","mode":"step"}`。后续按测试协调要求重启以加载冻结代码，详情见下文。
- 验证方式：使用现有正常登录流程取得 token，然后逐个 GET `/api/v1/step/drawings/{drawing_id}`；另外使用应用的 `get_postgres_step_snapshot("image", drawing_id)` 从 PostgreSQL checkpoint saver 独立读取快照。没有直接写入或伪造任务 state；获准的 package auto 仅通过匹配 interrupt_id 的正常 API 回答提交。没有修改 schema 或绕过认证。
- 安全边界：未在报告记录账号密码、JWT、数据库连接参数或其他凭据。没有代填尺寸或代替审核；续跑中仅使用经授权的 `auto` 测试策略，routing 和审核问题均保留等待明确答复。

## 六张图的初始暂停状态（续跑前）

| 图纸 | drawing_id | 状态 | checkpoint_id | interrupt_id | 待处理阶段 / 下游节点 | checkpoint / 产物 |
|---|---|---|---|---|---|---|
| ADC_图纸10.png | `1648c413-4c80-4207-866d-a51b3f189aa4` | `awaiting_input` | `1f1bad58-36b6-6a51-8000-14c5355b6714` | `19cf1ed7cbe15eb26fd11c8218c6b4d1` | package / `ask_package` | 可读；无 STEP/preview |
| ADC_图纸11.png | `d399c6b9-261c-4be8-879a-22efa7369fc8` | `awaiting_input` | `1f1bad58-36c1-667e-8000-e619fcffbae5` | `47cbe0d93da573a46b4c9435e63c7c13` | package / `ask_package` | 可读；无 STEP/preview |
| MCU_图纸8.png | `4f51a4c0-2153-4f7d-b022-20d2b84cca75` | `awaiting_input` | `1f1bad58-37b2-602d-8000-4e07da6d3049` | `ff7ef7111ce8844634f929289d27e6c3` | package / `ask_package` | 可读；无 STEP/preview |
| MISC-00008.png | `e29e0a96-5329-44f5-bacf-2e8d79e82720` | `awaiting_input` | `1f1bad58-3bc5-67a1-8000-024c653d4d02` | `0ebb2ddb7a911c003912f33d671ea2d4` | package / `ask_package` | 可读；无 STEP/preview |
| TR-00002.png | `379cafbf-6373-4acd-8881-09e147b41be7` | `awaiting_input` | `1f1bad58-3cbc-66f8-8000-df8d98fc57b8` | `fbe95696ea498bf9988ea3facd5d861e` | package / `ask_package` | 可读；无 STEP/preview |
| TR-00007-3.png | `71bae597-28a6-4a80-be3c-8b1417e6a589` | `awaiting_input` | `1f1bad58-3db9-6767-8000-395d515ea206` | `1c16c7e07fbb2752b18b88dd635bc17d` | package / `ask_package` | 可读；无 STEP/preview |

每个 API 查询均返回 `checkpoint_available=true`、`checkpoint_error=null`。API 快照中的 checkpoint_id 和 interrupt_id 与 PostgreSQL saver 直接读取结果相同，`next_nodes=["ask_package"]`。checkpoint state 中的 `image_path` 对应各任务上传图像路径；每个任务均有 2 条 checkpoint 记录、4 条 checkpoint_writes。六个任务均未提交回答，state 中人类回答历史为空。

## 初始封装问题（已用 auto 测试输入恢复）

六个图纸最初都返回同一个问题：“这张工程图需要生成什么封装？请填写封装名称；不确定可选择自动识别。” 可选操作是 `provide`（需提供封装名称）、`auto` 或 `cancel`。随后按本轮测试策略统一提交 `auto`，让 Agent 独立识别；该测试输入不是用户给出的封装或尺寸结论。后续 routing 和 review 均未代答。

## 与既有报告的关系

原始 baseline 保留在 [2026-09-24/report.md](../2026-09-24/report.md)，HITL 合成暂停/恢复验证保留在 [2026-09-24/verification-human-interaction.md](../2026-09-24/verification-human-interaction.md)。本报告记录 2026-09-28 六张真实样图的七个任务，其中 ADC_图纸10 独立重跑一次；合成测试不计作样图生成结果。

## 2026-09-28 第一轮自动识别续跑记录

### 重启、恢复与验证环境

- 为加载本轮冻结代码，在用户授权和 root 协调下停止原 PID 31316，使用项目隔离环境重启 `python -m backend.step_main`；新服务 PID 7816，health 持续返回 200。项目环境为 `.venv\Scripts\python.exe`。该环境使用已有 ima-agent site packages，并单独安装 checkpoint saver 依赖；安装器提示既有 industrial-injection-agents 与仓库 pin 版本存在依赖约束差异，本轮没有更改那些已继承包。
- 新服务启动恢复扫描把 7 个此前失去执行进程且没有 checkpoint 的旧 `step_drawings` 标成 `failed`，错误均为“任务失去执行进程且没有 PostgreSQL checkpoint，无法安全恢复；请重新提交新任务”。这 7 条是原有遗留任务，未清除或重建；初始 baseline 报告保持不变。
- 新增 family/worker 回归命令：`.\.venv\Scripts\python.exe -m pytest -q tests/test_step_family_evidence_regressions.py`，结果 **11 passed**、1 条 Pydantic 配置弃用 warning。API/HITL 人机逻辑测试另有 **45 passed**；均为合成/单元检查，不计入六图生成结果。
- 六个现有 drawing_id 均以正常登录后的 API `POST /drawings/{id}/human-input`、匹配 interrupt_id、`answer.action=auto` 恢复，全部 HTTP 202。`auto` 是此次测试策略要求 Agent 自行识别封装的测试输入，不代表用户给出的封装结论。没有人工指定尺寸，没有批准审核。
- 恢复后六个 API 状态 GET 都返回 200、`checkpoint_available=true`、`checkpoint_error=null`；API checkpoint_id/next_nodes/interrupt 与 `get_postgres_step_snapshot` 直接读 PostgreSQL 的结果匹配。表 `checkpoints` / `checkpoint_writes` 的逐任务计数见下表。business `step_drawings.status`、产物路径与 graph checkpoint state 是不同投影：报告分别记录 DB/API 状态和 live graph `status`/`next_nodes`，不会只凭缓存的 package_params 认定检查点存在。

### 第一轮六任务结果（保留原记录）

| 图纸 | drawing_id | DB/API 状态与 graph 状态 | Checkpoint rows / writes；live next_nodes | 产物与结果 |
|---|---|---|---|---|
| ADC_图纸10.png | `1648c413-4c80-4207-866d-a51b3f189aa4` | `stopped` / `stopped_insufficient_extraction` | 26 / 128；`[]` | 无 STEP/preview。`dimension_gate.json` 显示缺 `body_standoff`、`housing_height`，`body_standoff` 有冲突；未补尺寸、未重试。
| ADC_图纸11.png | `d399c6b9-261c-4be8-879a-22efa7369fc8` | `pending_review` / `review_required` | 33 / 159；`[review_result]` | 已生成真实 STEP 和4张视图预览，几何验证通过；Golden reference 未配置，且缺完整塑封拔模证据，需人工审核。等待 `approve` 或 `reject`，未代审。
| MCU_图纸8.png | `4f51a4c0-2153-4f7d-b022-20d2b84cca75` | `awaiting_input` / `views_detected` | 8 / 41；`[confirm_template]` | 无 STEP/preview；等待 routing 身份确认（见下）。
| MISC-00008.png | `e29e0a96-5329-44f5-bacf-2e8d79e82720` | `awaiting_input` / `views_detected` | 8 / 41；`[confirm_template]` | 无 STEP/preview；等待 routing 身份确认（见下）。
| TR-00002.png | `379cafbf-6373-4acd-8881-09e147b41be7` | `awaiting_input` / `views_detected` | 8 / 41；`[confirm_template]` | 无 STEP/preview；分类为 transistor 但目前无可执行模板，不能确认跑 IC/其他不匹配模板（见下）。
| TR-00007-3.png | `71bae597-28a6-4a80-be3c-8b1417e6a589` | `awaiting_input` / `views_detected` | 8 / 41；`[confirm_template]` | 无 STEP/preview；等待 routing 身份确认（见下）。

ADC_图纸11 的 Feature IR/验证值为：28 pins；本体 10.5 × 5.6 mm；外形宽 8.2 mm；采用图纸总高 2.0 MAX 和离板高度 0.05 MIN，派生本体高度 1.95 mm（图纸未直接标注 A2=1.95）；pitch=0.65 mm；首末脚中心跨距=8.45 mm；端子脚长 L=0.95 mm。STEP 检查得到 29 solids、510 faces、volume 120.992146 mm³，bbox x=±5.25、y=±4.1、z≈0..2.0 mm，验证 passed。上述数值是范围内的包络建模选值，不能称为图纸给定的精确标称模型。模型使用 `molded_body_box` 表达本体，假设明确要求人工审核；Golden 比较状态为 `review_required`，原因为 `golden_reference_not_configured`。

ADC_图纸11 API 结果为 HTTP 200、checkpoint 可读、next_nodes 为 review_result，review interrupt id 为 8d4147e05780cd1a3efdf06d7c83b5d5。STEP 与四张预览共五项下载及 SHA-256 对照见本报告后续“ADC_图纸11 五项真实 API 下载复核”。

### 等待用户决定的实际问题

以下任务保持原 interrupt/checkpoint，没有代替用户答复：

- MCU_图纸8.png：interrupt `a134f991b704934dbf1038abe31c69a4`，候选 `ic/quad_gullwing_ic` / `LQFP64`；Agent 发现 Figure 71 封装外形与 Figure 72 推荐 footprint 同图，询问确认、更改或取消。
- MISC-00008.png：interrupt `a05bb34946e87d5e5c153e1719f47075`，Agent 候选为 `resistor/two_terminal_chip`，但图纸没有明确支持电阻类别。Agent 关于“0603尺寸”的解释不作为验证结论；原图红框型号是 `SMD1206P010TF/30`，不能只根据尺寸断言器件类别。保留暂停，未用电阻模板生成。
- TR-00002.png：interrupt `2a189c1475480bf088744525e2a9641b`；识别为 transistor，但类别置信度低于 0.80，且没有已实现的 transistor 模板。旧 checkpoint 中候选 family_id 为空、选项曾包含 confirm；当前 API 展示层会移除不可执行的 confirm，并附说明，checkpoint 原值保持不变。只能改选明确适用且已实现的模板或取消；没有用不相容模板绕过。
- TR-00007-3.png：interrupt `02202500f11263e85e43c6817515cd52`，候选 `ic/gullwing_ic` / `SOP16`；图中还包含 PIN NO. 与内部连接图，等待用户确认/更改/取消。
- ADC_图纸11.png：review interrupt `8d4147e05780cd1a3efdf06d7c83b5d5`，选项 `approve` / `reject`。候选产物已可供人工查看和审核；没有证据确认用户已打开。

原始六任务统计：1 张形成候选 STEP 并待审核、1 张因证据不足停止、4 张等待 routing 答复；本报告新增一条 ADC_图纸10 独立重跑后，六张图对应 7 个任务，其中 2 个 ADC_图纸10 任务均停止。尚有 4 个 routing 中断和 ADC_图纸11 审核未完成。服务当前 PID 14848 保持运行，待用户作出对应决定后继续原 checkpoint。


## 2026-09-28 ADC_图纸10 新任务独立重跑

- 为加载入口共享代码重启服务，项目虚拟环境为 .venv\Scripts\python.exe，运行模块 backend.step_main；Uvicorn 子进程 PID 14848，health 返回 HTTP 200，状态为 ok / step。日志位于 output/step_agent/step-main-20260928.stdout.log 与 output/step_agent/step-main-20260928.stderr.log。
- 通过认证 API 上传 sample/picture/ADC_图纸10.png，得到新 drawing_id：548bc036-80b0-4946-8420-1e042d690be0；原图 SHA-256：e953c4140a7ee5f4ca6ed238b2f5c6252e4756ca3e036ddcb132a2c01495a2c3。旧任务 1648c413-4c80-4207-866d-a51b3f189aa4 保持 stopped，未恢复或覆盖。
- 新任务在 package interrupt 6ce0411b5bcb256b14d5071e2c17051c 收到经授权的测试输入 action=auto，API 返回 202。该输入要求 Agent 自动识别，没有提供尺寸或人工封装结论。
- 终态为业务状态 stopped、checkpoint state stopped_insufficient_extraction；checkpoint_id 为 1f1badd3-17e4-66f0-8018-23fc384bc1ee，next_nodes 为空且无 live interrupts。业务 projection 与 checkpoint 的 human_history 一致，包含唯一 package-auto 操作。没有 STEP 或预览产物。
- dimension_gate.json 显示缺少 body_standoff、housing_height、total_height，冲突为 body_standoff 与 total_height。冲突证据如下：

| 视图区 | OCR token 与原始文字 | Qwen 映射 | 核验 |
|---|---|---|---|
| region_005 | ocr_0028 = 1.20 | total_height | 与总体高度 MAX 标注一致 |
| region_005 | ocr_0030 = 0.15；ocr_0032 = 0.05 | body_standoff | 前视离板高度上下限，映射正确 |
| region_006 | ocr_0034 = 0.75；ocr_0040 = 0.60 | 同一对 token 同时分配给 total_height 和 terminal_length | 属于脚长 L 上限与典型值，却又污染总体高度 |
| region_006 | ocr_0044 = 0.45 | body_standoff | 属于脚长 L 下限，却污染离板高度 |

本次失败来自 region_006 侧视脚长 L 的 0.75 / 0.60 / 0.45 被映射到本体总体高度或离板高度；前视 0.15 / 0.05 并非失败来源。融合输出 conflicting_fields 为 body_standoff、total_height。同图 6.40 BSC token 还被映射到 pin_span 与 overall_width；该提取风险没有通过后续几何门禁验证，不能声称其正确。

- 认证方面没有注册路由。本次通过现有密码哈希逻辑建立唯一 step_test_ 前缀账号，再用正常 POST /api/v1/auth/login 取得令牌，并通过已认证 API 上传任务、提交 auto、下载产物。账号 user_id 为 5c71955e-c84d-49c2-870c-d815952fefc9；收尾只将该精确账号设为 is_active=false，保留用户行以保留 human_history actor ID，不删除任务或文件。禁用会阻止新登录；代码只读审查发现现有 get_current_user 仅检查 JWT 签名和过期时间、不回查 users，因此本轮不声称禁用即时撤销已签发令牌。本轮认证覆盖仅限此前报告的 5 项认证验证和这里真实成功的登录/API 操作。

### ADC_图纸11 五项真实 API 下载复核

| 产物 | HTTP | SHA-256 |
|---|---:|---|
| STEP | 200 | 359e9fbf82f4977af4eb27a4585b664c1a68a811e2e94591216ca88de9521451 |
| 等轴预览 isometric | 200 | e4c4021158cd82c53df49f4ce5a86a0d0e9d10184c789c63704d68e364ba5046 |
| front 预览 | 200 | 7249a15ae7abc8615dda8994f1f3513d2343691522f10e4250e3c80bdec92587 |
| top 预览 | 200 | e0116a88dd88e42e861a3ce8c3808057b9b862ce63ae342bd33fde8f8749b53e |
| right 预览 | 200 | 97ad28f5b5e0a86ed1a7aa93b9a47b6f0b42ff53b3c384d3f93403a594e13984 |

五项响应的 SHA-256 均与 PostgreSQL step_drawings 路径引用的本地文件一致。下载前后 ADC_图纸11 仍为 pending_review，interrupt 8d4147e05780cd1a3efdf06d7c83b5d5 与 checkpoint_id 1f1bad88-6b03-60c4-801f-eea16fcbdecc 未变；没有提交 human-input，也没有批准审核。

### 本轮独立回归结果

- .venv\Scripts\python.exe -m pytest -q tests/test_step_preview_downloads.py tests/test_step_evidence_fusion_regressions.py tests/test_step_family_evidence_regressions.py tests/test_step_human_api.py tests/test_step_human_nodes.py tests/test_step_image_agent.py：185 passed，4 warnings。覆盖 synthetic/API 下载安全与哈希、证据融合边界、family 尺寸约束、HITL 与图形回归；不计作样图生成通过。
- 入口隔离测试由入口代理另行执行：.venv\Scripts\python.exe -m pytest tests/test_step_entrypoints.py -q：11 passed，1 warning。两组不是一次 196 项全套运行结果。


### 收尾时仍待用户决定的 checkpoint

终核时下列五个人工中断的 API interrupt_id 与 PostgreSQL snapshot interrupt_id 一致，checkpoint next_nodes 仍为 confirm_template（四张 routing）或 review_result（ADC_图纸11）；各任务 human_history 仍仅有初始 package-auto，没有额外路由回答或审核决定：

| 图纸 | drawing_id | interrupt_id | checkpoint_id | stage / next_nodes |
|---|---|---|---|---|
| ADC_图纸11.png | d399c6b9-261c-4be8-879a-22efa7369fc8 | 8d4147e05780cd1a3efdf06d7c83b5d5 | 1f1bad88-6b03-60c4-801f-eea16fcbdecc | review / review_result |
| MCU_图纸8.png | 4f51a4c0-2153-4f7d-b022-20d2b84cca75 | a134f991b704934dbf1038abe31c69a4 | 1f1bad80-be1d-649b-8006-ec38c1d97149 | routing / confirm_template |
| MISC-00008.png | e29e0a96-5329-44f5-bacf-2e8d79e82720 | a05bb34946e87d5e5c153e1719f47075 | 1f1bad83-c43e-6468-8006-5f1a0345f057 | routing / confirm_template |
| TR-00002.png | 379cafbf-6373-4acd-8881-09e147b41be7 | 2a189c1475480bf088744525e2a9641b | 1f1bad84-42c7-6a78-8006-61322e0564dd | routing / confirm_template |
| TR-00007-3.png | 71bae597-28a6-4a80-be3c-8b1417e6a589 | 02202500f11263e85e43c6817515cd52 | 1f1bad85-3f81-68d2-8006-0aed78ee5d37 | routing / confirm_template |

ADC_图纸10 的旧 stopped 任务与本节新增的 ADC_图纸10 stopped 重跑是两个独立 drawing_id。另发现一个既有非样图任务，其输入哈希与本次六图均不相符，因此未纳入统计，也未在本轮对其提交任何回答。
