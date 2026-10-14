# PCB 封装前端

入口：`/api/v1/pcb/ui`。页面、PCB API 客户端和入口样式在本目录：

- `index.html`：主图／辅助图上传、设备选择、尺寸核对、预览与产物。
- `app.js`：PCB 页面入口。
- `api.js`：直接调用 `/api/v1/pcb`，不通过统一工作台转发任务。
- `styles.css`：沿用 STEP／Symbol 的视觉风格。

`frontend/shared/workspace.js` 和 `workspace.css` 是与统一工作台共用的交互和视觉组件。
PCB 页面固定为封装任务；历史选择按当前租户／账号保存，使用已有登录会话。
主图和最多四张辅助图通过 `generate-image` 上传，人工补充必须带当前 interrupt_id，
预览与 DRA、PSM、PAD 等文件通过鉴权下载。

API 及静态文件白名单在 `backend/api/v1/pcb.py`。共享设备信息在
`backend/api/cadence.py`，继续使用已实现的本机／SSH 调度队列。
`GET /api/v1/pcb/packages` 支持 `limit`、`offset` 与 `has_more`；状态响应提供
`artifact_urls`、`human_input_url` 和当前租户的 `queue`。

页面直接由后端同源提供，无需额外 npm 构建。浏览器回归包含主图／辅助图上传、
逐焊盘参数核对、人工采纳、下载鉴权、刷新恢复和手机布局。
焊盘表上方的“＋ 新增焊盘”用于手动补行，行末可删除；空表显示填写提示。
新增行保留空脚号、坐标和尺寸，操作后同步焊盘数量，完整表以 EXPLICIT 提交。
圆盘长度自动同步直径。完整连通网格需保留来源行数，相关增删入口禁用并解释原因。
