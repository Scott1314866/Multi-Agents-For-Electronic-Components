# Windows 工作电脑部署

## 2026-10-12 OCR 超时与机械外形改进

当日三项 STEP 记录在 OCR 120 秒限时处失败。使用原始图片在 CUI 复测，
SOT223 冷启动首轮再次在 120.34 秒超时；另两张五脚尺寸页横向扫描分别为
48.99 秒（重建进程）和 18.71 秒（热进程）。运行时已使用本地 v5 mobile
权重、Paddle 3.2.2、oneDNN 和 4 个线程，模型缓存环境变量也已生效。
堆栈采样位于 PaddleX text_recognition 的静态 CPU predictor，不能归因为
远程 OCR 网络请求失败。带水印的图纸会产生大量非尺寸文字；冷启动及首次推理
与正式识图混在同一截止时间内，使延迟波动直接变成任务失败。

独立预热实测 26.37 秒后，同一张 SOT223 图三个方向扫描共 74.30 秒，
分轮为 23.39、25.30、25.36 秒，保留 115 个证据 token。
修复将原生进程预热与正式扫描分开：`STEP_OCR_STARTUP_TIMEOUT` 默认 300 秒，
`STEP_OCR_SCAN_TIMEOUT` 保持 120 秒。每个进程只预热一次，预热直接运行
128×128 空白图，不能以缓存命中冒充预热；真实图片命中预测缓存时跳过预热。
初始化或扫描超时分别报告并重置所属进程。阶段日志记录模型加载、预热和推理耗时。
`STEP_OCR_MODEL_DIR` 可显式指定 PaddleX 缓存根；未指定时自动定位项目的
`models/paddlex`，本地完整模型直接传模型目录。启动器也设置本地缓存，避免
直接执行 `start_project.ps1` 时依赖外部启动环境。

PCB 增加无推荐焊盘图时的 MAX 机械投影路径，计算与型号/脚号证据分开保存。
SOT223 与左三右二的五脚草案及计算依据位于
`output/failure-analysis-20261012/`。它们用于参数核对，不代表已经完成焊接工艺
或电气脚号确认。当天五脚 PCB 失败还包含一次独立的 PostgreSQL 连接超时，
需与图片/OCR 错误区分。

新代码禁用预测缓存的远程冷启动实测为 104.91 秒，含 25.90 秒原生预热，
三个方向合计保留 115 个 token。两项原 STEP 失败任务已越过 OCR，分别停在
尺寸核对和封装路由确认。PCB 对裁切机械图增加闭合矩形/成对竖边的拓扑校验，
确认 SOT223 的三普通端子加一个宽端子、五脚的两排 3/2 分布；像素只提供
数量与间距槽位，不转换为毫米。视觉输出与主图拓扑不一致时在同一次读取
截止时间内纠错。空检查点不再投影为默认空参数，数据库在首次检查点之前
失败的图片任务可按原输入安全重试。
补图归一化先快照所有来源，修订使用独立文件名，防止覆盖旧主图和尺寸表；
机械读取源可来自辅助资料中的唯一裁切轮廓图。缺少数值表时仅保留可见布局，
等待资料补充，不使用型号常见尺寸。相关回归与 PCB、STEP、部署检查共 223 项通过。
最终补丁 `20261012-max-ocr-4` 已部署，健康检查通过；三项原 STEP 任务均越过
OCR，两项 PCB 原任务的 MAX 几何与交付草案一致，继续等待人工核对。

2026-10-12 公司第一版现使用 `http://172.16.10.156/`，支持邀请码注册。以下旧 DNS/IP 记录仅用于迁移历史；当前入口、账号及运维说明以 [公司试用版](company-pilot部署说明.md) 为准。

部署目标为工作电脑 `CUI`，通过 `cui.lan` 和已配置的 SSH 密钥连接。工作台在这台电脑运行，Symbol 和 PCB 使用本机 `C:/Cadence/SPB_17.2`，不再通过 SSH 回连设备。

## 目录与访问地址

- 项目：`D:/WorkSpace/Xien/project_01`
- Python 3.12 环境：`D:/Anaconda_envs/envs/ima-agent`
- 内网分享入口：`http://cui.lan/`（依赖公司 DNS 指向工作电脑；无需输入端口）
- IP 备用入口：`http://10.10.2.99:8000/`（2026-10-10 实测地址；IP 可能随网络变化）
- STEP：`http://cui.lan/api/v1/step/ui`
- Symbol：`http://cui.lan/api/v1/symbol/ui`
- PCB：`http://cui.lan/api/v1/pcb/ui`
- 配置：项目根目录 `.env.local`；包含凭据，不提交到 Git。
- 后端日志：`tmp/server.stdout.log`、`tmp/server.stderr.log`。

迁移使用当前可运行的 Windows 环境副本，并保持原来的绝对安装路径；不能随意移动环境目录。`requirements-runtime.lock.txt` 记录实际安装版本，原 `requirements.txt` 的两列格式不能直接用于 pip 安装。

## 启动

在工作电脑登录 Windows 后，计划任务 `Project01-Workstation` 启动 Docker 和后端。Cadence 使用当前用户会话及这台电脑已有的许可证配置。

2026-10-10 已将 CUI 接通电源时的自动睡眠超时从 25 分钟设为“从不”，避免闲置后 SSH 和工作台无法连接。使用 `powercfg /change standby-timeout-ac 0` 配置；电池供电的超时仍为 25 分钟。服务用于内网共享期间需要电脑保持开机、登录并联网。

手动启动：

```powershell
cd D:/WorkSpace/Xien/project_01
./scripts/start_workstation.ps1
```

已运行时不会重复启动。8000 端口被其他应用占用时会明确报错。

脚本最终通过 `start_project.ps1` 执行 `python -s -m backend.main`。`backend/main.py` 是唯一应用入口，包含认证、STEP、PCB、Symbol 和工作台的路由、数据库迁移与任务恢复逻辑。

内网入口使用工作电脑已有的 `cui.lan` DNS 名称。Windows IP Helper 服务将 TCP 80 转发到本机 8000，转发配置持久保存，服务设为自动启动；防火墙规则 `Project01-Workbench-HTTP` 允许同一子网访问 80。该名称用于内网，不是公网注册域名。

工作电脑曾使用 `172.16.10.156`，2026-10-10 唤醒后公司 DNS 将 `cui.lan` 解析为 `10.10.2.99`，旧 IP 无法连接。通过既有 SSH 主机密钥核验了新地址属于同一台 `CUI`。源电脑的 `SYMBOL_SSH_HOST` 已改为 `cui.lan`，正在运行的源电脑后端需重新启动后读取新配置。IP 变化不需要修改上述本机端口转发。

工作电脑的数据库使用 `DB_HOST=127.0.0.1`，避开本机 `localhost` 连接的额外等待。`STEP_OCR_ISOLATE_PROCESS=true` 让 STEP 文字识别在独立进程中运行，保持网页响应。Qwen 的密钥、接口地址和模型名称在 `.env.local` 配置，修改后需重启服务；模型认证失败时工作台会显示中文说明。

浏览器使用代理时，将 `cui.lan` 加入直连例外。域名依赖公司内网 DNS；如果域名返回 502，先使用 IP 备用入口，并分别检查代理、DNS 和 `/health`，避免把代理报错当作后端任务错误。

2026-10-10 OCR 修复：CUI 原有 PP-OCRv6 medium 权重完整，但 CPU 配置下这张图的三轮识别累计约 55 分钟。工作电脑改用本地 PP-OCRv5 mobile 检测和中文识别模型，独立 OCR 进程加载 `models/ocr_runtime/paddle322` 中的 Paddle 3.2.2，启用 oneDNN、4 个 CPU 线程及被动线程等待。主环境 Paddle 3.3.1 保留。

工作电脑 `.env.local` 的 OCR 专用配置为：

```dotenv
STEP_OCR_ISOLATE_PROCESS=true
STEP_OCR_RUNTIME_DIR=D:/WorkSpace/Xien/project_01/models/ocr_runtime/paddle322
STEP_OCR_CPU_THREADS=4
STEP_OCR_ENABLE_MKLDNN=true
STEP_OCR_DETECTION_MODEL=PP-OCRv5_mobile_det
STEP_OCR_RECOGNITION_MODEL=PP-OCRv5_mobile_rec
STEP_OCR_SCAN_TIMEOUT=120
STEP_OCR_CACHE_DIR=D:/WorkSpace/Xien/project_01/output/ocr_cache
```

通过实际 `extract_ocr_tokens` 和 Windows spawn 进程验证：同一张 `ic.png` 三个方向冷启动共 87.244 秒，缓存重复调用 0.263 秒。模型单独加载及扫描测试共 84.273 秒，其中扫描累计 47.333 秒。这是该图片的实测值，其他图纸耗时会变化。尺寸表的毫米和英寸数值可分别识别；带水印的个别尺寸符号仍有误读，继续通过原有证据校验和人工核对处理，不能把 OCR 成功等同于建模结果已经正确。

缓存保存原始多边形、文本及置信度，以图片像素、模型文件与运行库签名作为键；图纸或模型变化后重新识别。单轮超过 120 秒会终止该 OCR 子进程并重置进程池，任务显示中文错误，避免后续任务继续等待失效进程。旋转扫描与坐标还原规则保持原逻辑。ONNX 转换工具的 DLL 导入试验失败，未启用该路线。

## 数据与依赖服务

迁移包含前后端代码、样例、任务产物、检查点、登录账号、OCR 缓存和知识库数据。源电脑服务保留。

| 服务 | Docker 容器 | 工作电脑端口 |
| --- | --- | --- |
| PostgreSQL 15 | `project01-postgres` | `127.0.0.1:5433` |
| Milvus 2.4 | `project01-milvus` | `127.0.0.1:19531` |
| etcd | `project01-etcd` | Docker 内网 |
| MinIO | `project01-minio` | Docker 内网 |

数据库和知识库采用独立 Docker 数据卷，容器配置为自动重启。OCR 缓存在 `models/paddlex/official_models`，启动脚本设置 `PADDLE_PDX_CACHE_HOME`。

## 验证记录

本机生成的部署检查记录位于 `output/deployment_verification/report.json`，对应测试产物也保存在该目录。检查覆盖 STEP 导出和四视图、OCR 图片识别、鉴权与工作台接口、Capture 原生文件和 Allegro 三阶段执行。PCB 执行验证重放既有真实图片识别记录，不表示重新调用了视觉模型。
