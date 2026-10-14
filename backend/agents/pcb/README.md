# PCB 图片输入

2026-10-12 新增机械外形 MAX 派生草案：缺少推荐焊盘图时，读取原尺寸表的
MIN/MAX/TYP/BSC、尺寸线角色及逐个端子布局，再由 `mechanical_outline.py`
计算矩形端子的铜盘草案。用户明确要求按 MAX 生成时使用精简的外形读取 schema。
本体、普通端子宽和接触长度用 MAX，间距保留 TYP/BSC；不能把总引脚跨度当本体。
SOT223 普通端子使用 b/L，宽端子用 D1；宽端子没有接触长度标注时，按
`(总跨度.MAX-本体纵长.MAX)/2` 给出外伸投影，计算来源明确标为设计规则。
可按用户要求旋转两排布局，保持脚号随端子移动。没有可见脚号证据时保持为空，
不按常见型号编造编号。顶视方向未知时继续追问。
派生图使用零额外焊接裕量，属于供核对的几何草案，不能称为厂商推荐或 IPC
公差分析结果；原有参数确认、铜箔间距校验、Allegro 回读与结果复核继续执行。
回归包括 `tests/test_pcb_mechanical_outline.py` 的 SOT223 与五脚非对称布局。
裁切机械主图通过闭合矩形和稳定成对竖边检测端子数量、宽端子与间距槽位；
辅助图不能覆盖主图的 3+1／3+2 布局，数字尺寸仍须从表格读取。相同的两排
普通端子不能一排使用已标注接触长度、另一排改用外伸投影。
首次检查点之前发生数据库错误时保留原始图片和说明，空状态不能覆盖输入；
通过认证的重试可重新开始图片提取，并照常停在参数核对。
补图先读取全部旧来源再写入，修订使用独立文件名，避免原图或尺寸表被同名覆盖。
原始主图是脚号图时，可从辅助资料中选出唯一的机械轮廓图作为几何读取源；
脚号图继续保留。仅有尺寸符号、没有数值表时保留端子拓扑，等待补充 MAX 表，
不得调用模型凭常见封装尺寸生成数值。

PCB 页面在 `frontend/pcb/`，入口 `/api/v1/pcb/ui`；API 在
`backend/api/v1/pcb.py`，与统一工作台复用 `frontend/shared/` 的交互与样式。

图片入口为 `POST /api/v1/pcb/generate-image`，接收 multipart 表单：

- `file`：PNG、JPG、WEBP 或 BMP 封装尺寸图／推荐焊盘图，最多 32 MB、3200 万像素。
- `message`：可选背景说明，最多 4000 字符。
- `context_images`：最多四张辅助名称、本体尺寸或编号图。结果参考截图只用于结果对照，不作为识别输入。
- `mode`：`full`（默认，运行 Allegro）或 `emit_only`（只生成 SKILL 和运行脚本）。
- `execution_mode`：可选 `local`／`ssh`。不填时优先使用 `PCB_EXECUTION_MODE`，再沿用 `SYMBOL_CAPTURE_MODE`。
- `allegro_timeout`：可选单阶段秒数，大于 0 且不超过 1800；默认 600 秒。

原有 `POST /api/v1/pcb/generate` JSON 参数入口继续保留。

本机／远程执行共用正式工作流，不需要另跑测试脚本。SSH 直接复用 Symbol 的
`SYMBOL_SSH_HOST/USER/PORT/PRIVATE_KEY/KNOWN_HOSTS` 配置，无需再填写一套连接信息。
私钥不写入任务状态；三个阶段固定同一设备和工作目录，续跑时设备身份变更会停止。
远端每次 build 使用 `%LOCALAPPDATA%/pcb-agent/runs/<唯一目录>`，与 Symbol 隔离。
SKILL 内的工作路径指向远端；上传和回传逐文件校验 SHA256，日志、DRA、PSM、PAD
回到原任务目录，沿用已有判据、复核和下载接口。缺少工具链返回停止，超时或执行失败返回失败。

可在服务端 `.env.local` 按需覆盖：

```dotenv
PCB_EXECUTION_MODE=ssh
PCB_ALLEGRO_EXE=C:/Cadence/SPB_17.2/tools/bin/allegro.exe
PCB_ALLEGRO_PRODUCT=Allegro_Enterprise_PCB_Designer
PCB_ALLEGRO_TIMEOUT=600
```

`PCB_ALLEGRO_EXE` 不填时，本机沿用 `CADENCE_INSTALL_DIR/tools/bin/allegro.exe`、
`ALLEGRO_EXE` 或常用路径；远端从 `SYMBOL_SSH_TCLSH` 的同目录寻找 Allegro。
`PCB_SSH_REMOTE_ROOT` 可覆盖远端工作根目录，必须是 Windows 绝对路径。
状态查询提供 `execution` 和 `runs`，用于检查设备模式及三个阶段的退出码。
`emit_only` 不连接设备，最终结果仍需通过原有人工复核步骤。

处理链路：图片解码和方向规范化 → `pcb_vision` 多模态结构化提取 →
`image_parameters` 参数核对/补充 → 参数及铜箔间距校验 → SKILL 生成 →
Allegro build/verify/props → 判据与人工复核。

## 工作台分步追问

图片参数核对页会根据缺失字段、编号依据、顶视方向、连通地焊盘和工艺选择生成中文追问，
每轮最多四个问题。可以只回答已知的部分，点击“保存回答并继续追问”，系统保存回答并刷新剩余问题。
回答不会自动批准封装；最终仍要确认完整参数，再运行原来的铜箔间距检查和 Allegro 回读。

不清楚时，可在当前任务上传补充图片或 PDF 手册并重新识别。推荐焊盘主图仍使用图片；PDF 可放在辅助资料中。
PDF 最多32 MB、200页，按章节标题和器件名称筛选最多四页，保留原始手册、页码及 SHA256。
扫描版或无法定位尺寸章节的 PDF 会要求提供截图，不会从像素估算尺寸。
当前输入版本支持最多四张辅助图；补充资料后保留最近四页，之前文件与回答继续保留。
新来源重新识别后，旧回答只作为历史，需要重新核对本次生成参数。

追问沿用 `image_parameters` interrupt 与 `human-input` API，无需创建新任务。
回答例：`{"action":"clarify","values":{"ep_present":"no","manufacturing":{"mode":"source"}}}`。
工艺调整例：`{"action":"clarify","values":{"manufacturing":{"mode":"process","from":0.25,"to":0.20}}}`。
只接受当前问题提供的字段和选项；镜像与工艺修改由确定性代码应用，模型不解释或执行用户指令。
`human_history` 保存问题、回答和认证身份；`image_source_spec` 保留识别原值，工艺改动另行记录。
源图补充接口为 `POST /api/v1/pcb/packages/{id}/sources`，multipart 字段为 `file`、`role=primary/context` 和当前 `interrupt_id`。
过期问题、其他租户、正在运行及已经生成的任务不能通过该接口修改；接收资料版本先持久化，再从 checkpoint 续跑。

回归：`tests/test_pcb_clarification.py` 验证多轮回答、工艺审计、编号与方向、PDF选页和来源补充恢复；
`frontend/tests/workbench-ui.test.mjs` 验证浏览器追问、上传、记录、移动端及 STEP/Symbol 兼容性。

模型通过 `backend/core/llm_factory.py` 获取，使用已有 Qwen VL 配置。
识别请求关闭思考模式并限制输出长度。双排矩形图把主图尺寸和辅助图的本体/编号
分开读取，两路调用共享 120 秒截止时间；普通路径的格式错误最多重试一次。
小尺寸主图在送模型前放大，以便读取标注。
闭合轮廓检测只判断相对拓扑：每排列数、上下连通列、宽盘对齐边缘。
毫米尺寸来自标注，不能从轮廓像素换算。坐标由 `pads_from_grid` 根据已读尺寸计算。
只有角落编号时，首尾差值及实际列数一致才派生中间连续编号；不一致则保留缺失。
图片字段保留标注、视图和置信度；原始识别结果写入 `image_reading.json`。
图中没有的器件尺寸、焊盘尺寸、单位和脚号不会用 WSON 示例补齐。
尺寸均为毫米，底视图的编号需转换到顶视 PCB 坐标。

任务返回 `drawing_id` 和 `status_url`。查询状态时，`pending_input` 提供识别出的
`spec`、`issues` 和 `evidence`。使用现有 human-input 接口补充参数：

```json
{
  "interrupt_id": "当前 pending_input.interrupt_id",
  "answer": {
    "action": "submit",
    "spec": {
      "name": "MY_PACKAGE",
      "family": "用户确认的封装族",
      "body_x": 1.0,
      "body_y": 0.8,
      "body_height_max": 0.5
    },
    "comment": "已按器件图核对"
  }
}
```

这里的尺寸仅用于说明接口格式。`spec` 是对识别结果的字段补充；若脚号仍缺失，
需要同时提供完整 `pin_numbering` 和 `pads`。参数不完整时返回 422，任务继续等待。
`action=cancel` 可终止。回答通过认证、当前 interrupt 校验后持久化，续跑不重复识图。
铜箔间距不合规会停止，不能通过人工确认绕过。
主图有上下连通地焊盘时，来源保留连通结构。确认回答中明确指定
`pad_layout_policy=independent`，才按普通短焊盘长度拆成独立盘；该选择记入审核历史。
`source` 仅保留原始坐标，当前生成器会拒绝重叠的不同编号铜焊盘，尚未支持共用铜箔。

核对表支持人工工艺调整：填写原焊盘宽度和生成宽度，点击“批量修改相同宽度焊盘”，
只修改当前宽度匹配的行。例如把 15 个 0.25 mm 普通盘改为 0.20 mm，不影响 0.55 mm 的宽盘。
提交前仍需补齐每行脚号。拆分连通焊盘只重算相关列的 Y 坐标和长度，保留已填写的编号、
宽度及其他独立修改；表的行数必须与来源网格一致。
原始识别和网格尺寸保留，尺寸修改另存为回答中的 `manufacturing_adjustments`，记录行号、
脚号、尺寸字段、原值、生成值和毫米单位，随人工回答进入审核历史。修改后的参数继续接受
几何与间距校验，不能用工艺调整绕过校验。

`DUAL` 保留原来的双列等距生成方式。`EXPLICIT` 支持逐个矩形或圆形铜焊盘：

```json
{
  "pin_layout": "EXPLICIT",
  "pin_count": 2,
  "pin_numbering": ["1", "2"],
  "pads": [
    {"number": "1", "x": -0.525, "y": -0.45, "size_x": 0.50, "size_y": 0.65},
    {"number": "2", "x": 0.0, "y": -0.45, "size_x": 0.25, "size_y": 0.65}
  ],
  "ep_present": false,
  "pin1_marker": "NONE"
}
```

这是局部参数示例，仍需封装名、本体尺寸和最大高度。X 向右、Y 向上。
EXPLICIT 不混入 `pin_pitch`、`pin_columns_x`、`land_x`、`land_y`；各焊盘独立
生成 padstack，按各自阻焊尺寸回读验证，铜箔间距在所有焊盘对之间检查。
矩形本体的 X/Y 装配框尺寸分别派生；EXPLICIT 的丝印横线避开全部阻焊开口。
独立焊盘使用 `padstacks_zip` 下载。固定左上角三角形标记不适用于任意布局，
EXPLICIT 默认关闭该标记。
产物新增 `preview_svg` / `preview_png`，由实际派生几何渲染，并非参考图复刻。

Allegro 使用 `-symbol package -nographic -noopengl`，build、verify、props 均进入
PACKAGE 符号编辑器模式。`-sq` 只是 SI 许可证筛选，不能代替无图形启动开关。

示例图 `tests/golden/pcb/recommended_six_pad/footprint.png` 中，一脚宽 0.50，
另外五脚宽 0.25，各脚 Y 长 0.65；0.40 是 X 向中心距，1.55 是上下外缘跨距。
因此两排 Y 中心距为 `1.55 - 0.65 = 0.90`，中心可写为 ±0.45。
一脚边缘与上方窄焊盘对齐时，中心为 `-0.40 - (0.50 - 0.25)/2 = -0.525`；
该对齐关系仍须人工核对。图中没有完整编号、本体尺寸或最大高度，需要补充。
测试夹具中这些补充参数是测试输入，不是从图片提取的事实。

圆形盘填写 `shape="CIRCLE"`，`size_x`、`size_y` 均为铜盘直径。核对表选择圆形后自动同步两轴；
圆盘之间按圆周计算铜箔间距，圆盘与矩形之间按实际边缘计算。推荐图给出铜盘 SL 和阻焊 SR 时，
单边 `mask_expansion=(SR-SL)/2`；器件外形图的焊球直径 b 不能代替铜盘 SL。
Allegro 圆盘创建时两轴均填写直径，避免 17.2 的零轴尺寸报错；回读必须包含 `CIRCLE` 形状与铜盘实际直径。

当前生成无旋转矩形／圆形 SMD 焊盘和可选居中矩形 EP。通孔、异形焊盘、
偏置或分割 EP 不支持。EXPLICIT 路线已有 EMI8143 矩形盘和 PCMF3USB3B/C 圆形盘的 Allegro 实机回读验证；圆形盘真实日志保存在 `tests/golden/pcb/pcmf3usb3b/`。
每个新任务回读验证之前
不能把 SKILL 已生成视为封装已通过验证。没有 Allegro 时明确返回停止状态。

规则四边推荐焊盘图先检测四排矩形的相对拓扑，再分别读取尺寸和本体信息。
模型只读取间距、铜箔宽/长、外缘跨距和各排端点编号；Python 按标注计算坐标，
仅在端点编号跨度与可见焊盘数量一致时展开连续编号。轮廓像素不用于估算毫米。
本体尺寸和高度缺失时保留为空，进入参数核对；用户明确给定的数值单独记录来源。

图片结构化响应无效时最多纠错重试一次，两次共享 120 秒期限。连续失败显示简短说明；
通过 `POST /api/v1/pcb/packages/{drawing_id}/retry` 或页面“重新识别原图”重新读取同一任务。
重试仅适用于尚未成功提取参数的失败图片任务，保留原图、检查点和重试记录，并按租户和任务锁校验。
参数核对时直接拦截不合规铜箔间距，允许修正后再次提交；后续硬校验仍照常执行。

运行回归：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_pcb_execution.py tests/test_pcb_emi8143.py tests/test_pcb_image.py tests/test_pcb_package.py tests/test_main_entrypoint.py -q
```

用已保存的真实识别记录验证配置好的 SSH 设备（不再次调用模型）：

```powershell
.venv/Scripts/python.exe scripts/test_pcb_emi8143.py --reading tests/golden/pcb/emi8143/real_reading.json --confirmed-spec tests/golden/pcb/emi8143/confirmed_fields.json --output output/pcb_agent/emi8143/normal_workflow --ssh
```

该脚本通过正式 PCB 图执行 build/verify/props，并停在结果复核 interrupt；
只有三阶段和全部判据通过才打包原生文件，不把自动测试结果记为人工审批。


图片输入的两侧机械 MAX 草案会独立读取电气 TOP VIEW 编号图，再按物理排布绑定脚号。编号图使用自然阅读顺序（上下排从左到右，左右排从上到下），程序负责 Y 轴转换及唯一可证明的旋转。脚号不得由表格行号、型号或引脚数量派生；对称排布不能唯一确认方向时保留人工核对。高置信度且完整的来源脚号绑定后，参数提交和生成前校验都比对位置与脚号，修改尺寸或表格行序不会改变这项核对。生成结果继续逐脚回读位置，并报告与来源图的一致性。

参数表显示随编辑更新的顶视预览。有效提交清除当前识别错误，原始识别问题保留在 source_extraction_issues 和来源报告；旧已完成任务的历史问题不再作为当前未解决错误返回。已批准任务的参数与审核记录不会因此改写。
