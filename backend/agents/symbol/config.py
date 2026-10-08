"""符号生成 Agent 的配置（对应源程序的 ``app/config.py``）。

源程序的密钥加载链是「环境变量 → Windows 注册表 → ``MinerU_API_KEY.md``」，
其中注册表兜底是 Windows 专有的。**迁移后统一走
:func:`backend.config.get_settings`**，密钥只从环境变量 /
``.env.local`` 来，不再自读注册表，也不再依赖仓库里的密钥文件。

``OUTPUT_DIR`` 是用户的**正式库**（``D:\\Data\\01_Lib2023\\02_sch``）。源程序
对它设了确认闸（默认 N）。迁移后由人工停点接管，**agent 绝不自动写入**。
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.config import get_settings

#: 本 Agent 的工作根。产物一律落在它下面，不碰用户的正式库。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYMBOL_JOB_ROOT = PROJECT_ROOT / "output" / "symbol_agent"

#: Cadence 侧的可执行文件（与 pcb 的 allegro.exe 同理，本机通常没有）。
DEFAULT_TCLSH = Path(r"C:\Cadence\SPB_17.2\tools\bin\tclsh.exe")
TCLSH_ENV_VAR = "CADENCE_TCLSH"

#: DBO 写入所需的动态库，TCL 脚本里 load 它。
DBO_DLL = "C:/Cadence/SPB_17.2/tools/bin/orDb_Dll_Tcl64.dll"

#: MinerU 云端 API 的兜底地址（可在 .env.local 覆盖）。
DEFAULT_MINERU_BASE_URL = "https://mineru.net/api/v4"

#: 兼容别名 —— 搬来的 ``tools/mineru.py`` 把它用作函数默认值。
#: 改动地址请改 ``DEFAULT_MINERU_BASE_URL`` 或在 .env.local 覆盖。
MINERU_BASE_URL = DEFAULT_MINERU_BASE_URL


def mineru_base_url() -> str:
    """MinerU 服务地址。"""
    return get_settings().mineru_base_url or DEFAULT_MINERU_BASE_URL


def mineru_token() -> str:
    """MinerU 令牌。

    Raises:
        RuntimeError: 未配置。**明确报错而不是回落到仓库里的密钥文件** ——
            源程序把令牌明文放在 ``MinerU_API_KEY.md`` 里，那本身就是个
            待清理的安全债（本次迁移不继承它）。
    """
    token = (get_settings().mineru_api_key or os.environ.get("MINERU_API_KEY") or "").strip()
    if not token:
        raise RuntimeError(
            "未配置 MinerU 令牌：请在 .env.local 里设置 MINERU_API_KEY"
            "（源程序曾把它明文存放在 MinerU_API_KEY.md，迁移后不再从文件读取）"
        )
    return token


def resolve_tclsh(exe: str | Path | None = None) -> Path:
    """定位 ``tclsh.exe``。

    优先级：显式传入 → 环境变量 ``CADENCE_TCLSH`` → 常规安装路径。

    Raises:
        FileNotFoundError: 三处都找不到。这是**环境缺失**，调用方应把任务
            标记为"等待环境"而不是"生成失败"。
    """
    candidates: list[Path] = []
    if exe is not None:
        candidates.append(Path(exe))
    env = os.environ.get(TCLSH_ENV_VAR)
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_TCLSH)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "找不到 tclsh.exe。请在有 Cadence 的机器上运行，或设置环境变量 "
        f"{TCLSH_ENV_VAR} 指向可执行文件。已尝试：{', '.join(str(c) for c in candidates)}"
    )
