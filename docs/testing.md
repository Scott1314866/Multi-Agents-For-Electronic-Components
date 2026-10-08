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
| `tests/test_step_progress.py`、`test_step_latency.py`、`test_step_preview_downloads.py`、`test_step_entrypoints.py` | 执行日志、超时和截断重试、预览下载及应用入口 | 本地运行时依赖；不调用真实模型 |
| `tests/test_pcb_package.py` | PCB 封装参数派生、规则断言、Allegro skill 文件生成、路由契约 | 单元测试不启动 Allegro；实机 PCB 工具链验证另行进行 |
| `tests/test_symbol_contract.py` | PDF 表格解析、引脚布局、Capture Tcl 生成与既有 Golden 文件对拍 | Golden fixture；不启动 Capture |
| `tests/test_agentic_web_search.py` | 搜索编排、结果处理和 STEP 参考模型解析 | 网络调用使用 mock |
| `tests/test_step_image_agent_dip_regression.py` | DIP 图纸真实识别、路由、建模、STEP 回读 | 真实 OCR、Qwen、Jev、Web Search、CadQuery 和本地 DIP 图片；默认跳过 |

所有 pytest 测试默认由 `pytest.ini` 收集。`integration` 标记仅用于需要真实外部服务的回归；默认命令 `-m "not integration"` 会排除它们。

## 真实模型回归

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
