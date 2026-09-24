"""CN 排针器件族预留入口。"""

from backend.agents.step.families._shared.unsupported import raise_unsupported_template


FAMILY_ID = "connector/cn/pin_header"
IMPLEMENTED = False


def plan(*_args, **_kwargs):
    """迁移旧排针模板前禁止生成占位模型。"""
    raise_unsupported_template(FAMILY_ID)


def plan_from_evidence(*_args, **_kwargs):
    """图片证据链同样禁止生成占位模型。"""
    raise_unsupported_template(FAMILY_ID)
