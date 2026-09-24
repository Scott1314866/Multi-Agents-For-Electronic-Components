"""晶体管模板占位。"""

from backend.agents.step.families._shared.unsupported import raise_unsupported_template

FAMILY_ID = "transistor"
IMPLEMENTED = False


def plan(*_args, **_kwargs):
    raise_unsupported_template(FAMILY_ID)


def plan_from_evidence(*_args, **_kwargs):
    raise_unsupported_template(FAMILY_ID)

