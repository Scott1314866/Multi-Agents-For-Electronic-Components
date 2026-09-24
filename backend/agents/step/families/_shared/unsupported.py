"""未实现模板共享的安全停止行为。"""

from __future__ import annotations

from typing import NoReturn


def raise_unsupported_template(family_id: str) -> NoReturn:
    """拒绝生成空模型，交由工作流转换为 unsupported 状态。"""
    raise NotImplementedError(f"数模模板尚未实现：{family_id}")

