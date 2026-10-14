# 统一工程工作台

启动 `backend.main` 后，默认首页跳转到 `/api/v1/workbench/ui`。
沿用 STEP 与 Symbol 的浅灰侧栏、白色内容区、蓝色操作按钮与同一登录会话。
无 npm 构建步骤，页面和资源由后端同源提供。

PCB 独立前端在 `frontend/pcb/`，入口 `/api/v1/pcb/ui`，直接使用 PCB API。
两个页面共用 `frontend/shared/workspace.js`、`workspace.css`，保持样式和核对行为一致。

工作台统一提交 STEP／Symbol／PCB 任务，展示全局排序和分页的历史任务、
参数核对、人工确认、几何预览、执行记录和鉴权下载。PCB 可附最多四张辅助尺寸图；
结果参考图不进入识别输入。STEP 使用后端 OpenCascade，不使用 Cadence 设备。

`backend/api/v1/workbench.py` 是输入和显示适配器，直接调用原来的三个 Agent API；
原来的任务表、租户校验、检查点、当前 interrupt 校验、审核历史与恢复逻辑继续生效。
设备列表仅表示服务端是否配置 SSH，不能代替实际连接或许可检查。
私钥、主机地址和账号不会出现在设备列表中。

Symbol、PCB 可以逐任务选择 `execution_mode=local|ssh`。同一设备上的 Capture 和
Allegro 共用 `backend/core/cadence_scheduler.py` FIFO 队列；本机与远程设备独立执行。
排队和原生执行不阻塞 HTTP；取消执行者时等原生进程结束再释放设备槽位。
识图、联网、人工核对不占用设备槽位。正常关闭等待三个 Agent 的后台任务保存状态。

当前设备队列属于单个后端进程，标准 `backend.main` 以单进程运行。任务本身由数据库
持久化并使用任务锁。部署多个 API worker 或多个调度节点时，应先将设备队列改为
数据库或外部队列协调，不能假定进程内队列能跨进程限流。

API：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/workbench/devices` | 模式默认值、连接配置状态、当前租户设备队列 |
| `POST /api/v1/workbench/jobs` | multipart：kind、file、message、execution_mode；按任务类型接收附加字段 |
| `GET /api/v1/workbench/jobs?kind=pcb&limit=30&offset=0` | 当前租户统一分页历史，隐藏已删除 Symbol 任务 |
| `GET /api/v1/workbench/jobs/{kind}/{id}` | 状态、队列、待答问题、工程数据与产物 |
| `POST .../{kind}/{id}/human-input` | 原有阶段回答格式，必须带当前 interrupt_id |
| `GET .../{kind}/{id}/artifacts/{name}` | 沿用 Agent 文件白名单与任务目录校验 |

验证：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_workbench.py tests/test_cadence_scheduler.py -q
node --test frontend/tests/workbench-ui.test.mjs
```

浏览器测试使用本地 mock API，覆盖图片上传、16 焊盘参数编辑、刷新保留编辑、
过期人工问题、任务切换、身份过期、下载鉴权和手机布局；不调用模型或 Cadence。
截图保存在 `output/workbench/ui/`。实机测试仍通过原有 Agent 工作流运行。
