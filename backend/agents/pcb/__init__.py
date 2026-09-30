"""PCB 封装生成助手。

把「封装参数 → Cadence Allegro 封装（``.dra`` / ``.psm`` / ``.pad``）」这条
链路，从源程序 ``Module_02`` 的硬编码 SKILL 脚本，改造成参数化、可校验、
可人工暂停的 LangGraph 工作流。

与 :mod:`backend.agents.step` 一样，这里只导出「图构造器 + 持久化入口」；
节点、路由与 schema 从具体模块全路径 import。
"""

from backend.agents.pcb.graph import build_pcb_graph
from backend.agents.pcb.persistence import (
    get_postgres_pcb_snapshot,
    invoke_postgres_pcb_graph,
    open_postgres_pcb_graph,
    pcb_checkpoint_config,
    resume_postgres_pcb_graph_at_node,
)
from backend.agents.pcb.spec import PackageSpec, wson8_3x3

__all__ = [
    "PackageSpec",
    "build_pcb_graph",
    "get_postgres_pcb_snapshot",
    "invoke_postgres_pcb_graph",
    "open_postgres_pcb_graph",
    "pcb_checkpoint_config",
    "resume_postgres_pcb_graph_at_node",
    "wson8_3x3",
]
