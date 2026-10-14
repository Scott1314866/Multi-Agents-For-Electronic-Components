# backend/config.py
# 全项目唯一的「配置中心」：从 .env.local 读取所有配置项，供任何模块取用。

from pydantic_settings import BaseSettings  # Pydantic 的「配置基类」，能自动从环境变量/.env 读取并做类型校验
from functools import lru_cache  # 标准库装饰器：缓存函数结果，让函数实际只执行一次
import os
from typing import Literal
from pydantic import Field, model_validator
from sqlalchemy.engine import URL

# 获取项目的根目录
live_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 获取 .env.local 文件的路径
env_local_path = os.path.join(live_path, ".env.local")


class Settings(BaseSettings):
    """配置模型：每个类属性对应 .env.local 里的一项配置。
    继承 BaseSettings 后，Pydantic 会自动把同名（大小写不敏感）的配置读进来并转成对应类型。"""

    # ── 数据库（PostgreSQL）──
    db_host: str = "localhost"  # 主机；写了默认值 = 可选项
    db_port: int = 5433  # 端口；本机 5432 已占用，隔离到 5433
    db_name: str = "agent"  # 库名
    db_user: str  # 用户名；没有默认值 = 必填，.env.local 缺了会启动报错
    db_password: str  # 密码；同样必填

    @property
    def database_url(self) -> str:
        """把上面几个散件拼成 SQLAlchemy 需要的连接串。
        用 @property 装饰后，可像访问属性一样 settings.database_url 取值，不用加括号调用。"""
        return URL.create("postgresql+asyncpg", username=self.db_user,
                          password=self.db_password, host=self.db_host,
                          port=self.db_port, database=self.db_name).render_as_string(hide_password=False)

    # ── Milvus 向量库 ──
    milvus_host: str = "localhost"
    milvus_port: int = 19531  # 本机 19530 已占用，隔离到 19531

    # ── 大模型（DeepSeek）──
    deepseek_api_key: str = ''  # 必填：DeepSeek 的 API Key
    deepseek_base_url: str = ""  # DeepSeek 接口地址
    deepseek_model_chat: str = ""  # 对话模型名
    deepseek_model_coder: str = ""  # 代码模型名

    # ── 大模型（Qwen）──
    qwen_api_key: str = ""
    qwen_base_url: str = ""
    qwen_model_vl: str = ""

    # ── Jev CAD 模板路由（可选；留空时自动回退现有 Qwen Family）──
    typesafe_api_key: str = ""
    typesafe_base_url: str = "https://api.typesafe.ai/v1"
    typesafe_model: str = "jev-latest"

    # ── 本地模型权重路径 ──
    reranker_model_path: str = "./models/reranker/bge-reranker-large"  # 精排模型
    classifier_model_path: str = "./models/classifier/all-MiniLM-L6-v2"  # 意图分类模型
    bge_m3_model_path: str = "./models/embedding/bge-m3"  # 嵌入模型
    finetuned_classifier_path: str = "./models/classifier/finetune"

    # ── JWT 认证 ──
    jwt_secret_key: str  # 必填：签发登录令牌用的密钥
    jwt_algorithm: str = "HS256"  # 签名算法
    jwt_access_token_expire_minutes: int = 10080  # 令牌有效期（分钟）

    # ── MCP Server 地址（第五章用）──
    kb_mcp_server_url: str = "http://localhost:8000/mcp/kb"
    web_search_mcp_url: str = "http://localhost:8000/mcp/web-search"

    # ── Web 搜索（Tavily 可选；留空则自动用免费的 DuckDuckGo）──
    tavily_api_key: str = ""

    # ── MinerU（OrCAD 符号生成用：datasheet PDF 的版面解析）──
    # 迁移后的符号 Agent 只从这里取令牌，不再读仓库里的 MinerU_API_KEY.md。
    mineru_api_key: str = ""  # 留空时符号 Agent 在真正需要联网时明确报错
    mineru_base_url: str = "https://mineru.net/api/v4"

    # ── Symbol Capture 执行：本机 Cadence 或已配置的 Windows SSH 工作机 ──
    symbol_capture_mode: Literal["local", "ssh"] = "local"
    symbol_capture_timeout: float = Field(default=300, gt=0, le=1800)
    cadence_install_dir: str = ""
    cadence_tclsh: str = ""
    cadence_dbo_dll: str = ""
    symbol_ssh_host: str = ""
    symbol_ssh_user: str = ""
    symbol_ssh_port: int = Field(default=22, ge=1, le=65535)
    symbol_ssh_private_key: str = ""
    symbol_ssh_known_hosts: str = ""
    symbol_ssh_remote_root: str = ""  # 留空使用远端 LOCALAPPDATA/symbol-agent/runs
    symbol_ssh_tclsh: str = "C:/Cadence/SPB_17.2/tools/bin/tclsh.exe"
    symbol_ssh_dbo_dll: str = "C:/Cadence/SPB_17.2/tools/bin/orDb_Dll_Tcl64.dll"

    # PCB 复用 Symbol 的 SSH 设备和身份；留空时执行模式也沿用 Symbol。
    pcb_execution_mode: Literal["local", "ssh"] | None = None
    pcb_allegro_exe: str = ""
    pcb_allegro_product: str = "Allegro_Enterprise_PCB_Designer"
    pcb_allegro_timeout: float = Field(default=600, gt=0, le=1800)
    pcb_ssh_remote_root: str = ""  # 留空使用远端 LOCALAPPDATA/pcb-agent/runs

    # STEP 的原生 OCR 可在独立进程运行，避免 Windows 推理阻塞 HTTP。
    step_ocr_isolate_process: bool = False
    step_ocr_runtime_dir: str = ""
    step_ocr_cpu_threads: int = Field(default=10, ge=1, le=16)
    step_ocr_enable_mkldnn: bool = False
    step_ocr_detection_model: str = "PP-OCRv6_medium_det"
    step_ocr_recognition_model: str = "PP-OCRv6_medium_rec"
    step_ocr_scan_timeout: float = Field(default=120, gt=0, le=600)
    step_ocr_startup_timeout: float = Field(default=300, gt=0, le=900)
    step_ocr_cache_dir: str = ""
    step_ocr_model_dir: str = ""  # PaddleX cache root; auto-detect project models/paddlex when present

    # ── 应用基础配置 ──
    app_env: str = "local"  # 运行环境标识
    app_debug: bool = False  # 是否调试模式
    app_host: str = "0.0.0.0"  # 监听地址
    app_port: int = 8000  # 监听端口
    log_level: str = "INFO"  # 日志级别
    default_tenant_id: str = "tenant_default"  # 多租户默认值
    registration_mode: Literal["closed", "invite", "open"] = "closed"

    @model_validator(mode="after")
    def production_settings(self):
        if self.app_env == "production":
            if len(self.jwt_secret_key) < 32:
                raise ValueError("生产环境 JWT_SECRET_KEY 至少需要 32 个字符")
            if self.app_debug:
                raise ValueError("生产环境必须关闭 APP_DEBUG")
        return self

    class Config:
        """Pydantic 的元配置：告诉 BaseSettings 该怎么读取配置。"""
        env_file = env_local_path  # 从这个文件读取配置
        env_file_encoding = "utf-8"  # 文件编码
        case_sensitive = False  # 大小写不敏感：环境变量 DB_HOST 能对应字段 db_host
        extra = "ignore"  # .env.local 里多出来的、模型没定义的字段一律忽略（不报错）


@lru_cache()  # 惰性加载：缓存：保证 get_settings() 只创建一次 Settings、只读一次文件
def get_settings() -> Settings:
    """获取全局唯一的配置对象。任何模块要用配置，都调用这个函数。"""
    return Settings()  # 首次调用时创建实例；之后每次都返回同一个缓存对象
