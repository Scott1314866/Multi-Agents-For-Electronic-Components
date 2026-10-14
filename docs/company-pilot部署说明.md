# 公司第一版试用

目标是已有 Cadence 的 Windows 工作机 `172.16.10.156`。第一版包括账号登录、邀请码注册、STEP／Symbol／PCB 任务、参数核对、结果复核和文件下载，入口为 `http://172.16.10.156/`。同一公司租户共享任务与文件；邀请码只能加入管理员所属租户，注册者始终是普通用户。

## 注册与管理

管理员登录统一工作台后点击“邀请码管理”，设置人数（1–100）、有效期（1–30 天）和备注。完整邀请码仅在创建时显示；数据库保存其 SHA-256 摘要。可随时撤销未用完的邀请码。用户打开 `/api/v1/auth/ui`，填写用户名、邮箱、至少 12 个字符的密码和邀请码，注册后直接进入工作台。邮箱用于登录，第一版没有邮件验证和密码找回。

`REGISTRATION_MODE=invite` 开放邀请码注册；`closed` 关闭注册；`open` 为无需邀请码注册，本次部署不启用。用户名、邮箱按大小写不敏感检查，不能由客户端指定角色或租户。每次认证都检查账号有效性和当前角色，禁用账号后旧令牌也失效。登录和注册有单进程限流，第一版使用一个应用进程。

首次管理员通过项目 Python 执行下列命令创建，随机密码和首批邀请码写入私有文件，控制台不输出凭据。不要在公司环境运行 `seed_data.py`。

```powershell
python -s -m scripts.bootstrap_access --email company-admin@example.com --server-url http://172.16.10.156 --credentials-file tmp/company-pilot-access.json --invite-uses 20 --disable-demo-users
```

`--disable-demo-users` 只禁用仍使用种子脚本原始密码的已知演示账号，保留账号行、任务和审核记录。真实修改过密码的账号保持原样。

## 服务与更新

通过 `scripts/start_workstation.ps1` 启动，唯一应用入口是 `backend.main`。PostgreSQL、Milvus、etcd、MinIO 使用工作机现有 Docker 数据卷；Cadence 在本机执行，OCR 使用工作机既有独立运行库及缓存。

`/health` 检查 Python 服务存活；`/api/v1/workbench/ready` 在 5 秒内检查数据库，异常返回 503，不暴露连接凭据。依赖服务异常时，监听端口不能说明工作台可用。Docker Desktop 需要在已登录的工作机用户会话中运行。

发布包含文件哈希清单，应用前核验全部路径和 SHA-256。`scripts/deploy_workstation.py` 在没有运行任务时备份源码、环境设置和 PostgreSQL，再检查一次运行任务，核验 PID 后更新。数据库、模型、知识库和已有任务文件不随源码发布覆盖。生产环境关闭调试并使用至少 32 字符的签名密钥，访问令牌为 8 小时。

备份位于项目 `tmp/releases/`，属于私有运维文件，不能通过静态页面提供。回滚恢复对应应用 ZIP 与 `.env.local`，停止经核验的应用 PID 后重新启动；新增邀请码表可保留。数据库备份只用于灾难恢复，日常源码回滚不覆盖上线后的用户和任务。

工作机的 PowerShell 文件使用正式 `.ps1` 文件名直接发布，并检查运行环境看到的 SHA-256 和语法，避免临时扩展名重命名后脚本不可读。更新前还需结束旧应用所属的空闲 OCR 子进程，释放重定向日志句柄；必须先核验父 PID 和任务状态，不结束其他 Python 进程。

## Docker Compose 依赖服务配置

根目录 `docker-compose.yml` 对应 Windows 工作机 `172.16.10.156`。它管理 PostgreSQL、etcd、MinIO、Milvus；应用通过 `backend.main` 在 Windows 上运行，以使用本机 Capture、Allegro、OCR 环境和许可证。网页入口仍为 `http://172.16.10.156/`，数据库和向量库只绑定 `127.0.0.1`，不向公司网络开放。

Compose 使用现有 `project01-*-data` 四个外部数据卷及 `project01-network` 外部网络。卷和网络缺失时启动会报错，避免创建空数据库。首次初始化挂载真实存在的 `scripts/init_db_package.sql`；已有 PostgreSQL 卷不会重跑初始化脚本，应用启动负责业务表迁移。不要运行种子账号脚本。

在工作机 `.env.local` 保留现有 `DB_USER`、`DB_PASSWORD`、`DB_NAME` 和存储凭据，并补充以下配置。`MINIO_ACCESS_KEY` 和 `MINIO_SECRET_KEY` 必须与当前 MinIO / Milvus 容器一致，不要临时替换；`MINIO_IMAGE` 使用现有已验证镜像的固定标签或仓库摘要。

```dotenv
DB_HOST=127.0.0.1
DB_PORT=5433
MILVUS_HOST=127.0.0.1
MILVUS_PORT=19531
# MINIO_IMAGE=minio/minio@sha256:实际已验证的仓库摘要
# MINIO_ACCESS_KEY 和 MINIO_SECRET_KEY 从现有私有配置保留，不提交到 Git。
```

可在工作机读取当前 MinIO 镜像摘要，此命令不输出存储口令：

```powershell
$imageId = docker inspect --format '{{.Image}}' project01-minio
docker image inspect --format '{{json .RepoDigests}}' $imageId
```

如果原镜像通过离线导入没有仓库摘要，应先为该已验证镜像设置专用固定标签，再将标签填入 `MINIO_IMAGE`；不得直接切换到新的 `latest`。

先做静态配置检查，避免在终端展开真实口令：

```powershell
docker compose --env-file .env.local config --quiet
```

**现有工作机的容器由 `docker run` 创建，不能直接用 `compose up` 接管。** 日常仍使用 `scripts/start_workstation.ps1` 启动它们。迁移 Compose 前应确认没有执行中的任务、备份 PostgreSQL 和知识库卷，核验实际卷名、网络名及镜像版本与文件一致；停止应用和依赖，只移除原容器，保留全部数据卷。然后由 Compose 重建依赖容器，检查健康及数据，再启动应用。此过程需要维护窗口，本次源码修改不自动执行迁移。

已完成上述迁移后使用：

```powershell
docker compose --env-file .env.local up -d --wait --wait-timeout 300
./scripts/start_workstation.ps1
```

全新工作机则需先显式创建上述四个数据卷和网络，再启动 Compose；准备 Windows Python、Cadence、OCR 和模型配置，启动应用迁移后通过 `bootstrap_access` 创建管理员和邀请码。Compose 不替代这些安装与账号初始化步骤。

日志按每容器 3 个、每个 10 MB 轮转；依赖容器自动重启，Milvus 等待 etcd、MinIO 健康后启动。

## 试用检查

上线验证包含管理员登录、创建邀请码、普通用户注册和登录、邀请码并发名额检查、过期／撤销／权限检查、三类任务接口与原始资料和产物下载。实际工程文件生成仍以 Agent 的执行记录和人工复核为准。

第一版使用公司内网 IP 的 HTTP 入口，尚未配置域名、邮件服务或外网访问。工作机须保持开机、登录并连接公司网络。

## 2026-10-12 上线验收

版本 `1.0.0-pilot.1` 已部署。真实网络验收 30 项通过，包括单次邀请码两人并发注册仅成功一人、普通用户不能管理邀请码、撤销及用完的邀请码拒绝注册。STEP 文件、Symbol OLB／DSN 的既有任务下载成功。注册页 DOM 已显示邀请码模式和可用注册按钮；HTML、JS、CSS 与发布源码逐字节一致。

新提交的 PCB 样例 `241d2693-a306-4f27-8559-50534b3c1876` 使用自动验收账号，经真实模型识别 64 个焊盘和参数核对后在本机 Allegro 执行。149 项判据全部通过；DRA、PSM、焊盘库及预览下载均返回 200。样例保留在待复核阶段，没有记录为用户人工采纳。

验收报告在 `output/deployment_verification/company-pilot-20261012/report.json`，回归报告在同目录 `regression.xml`。部署前数据库备份约 8.46 MB，存放于工作机 `tmp/releases/pilot-20261012-before2/`。第一版管理员和首批邀请码在私有 `tmp/company-pilot-access.json`，该文件不进入发布包，也不提供网页下载。
