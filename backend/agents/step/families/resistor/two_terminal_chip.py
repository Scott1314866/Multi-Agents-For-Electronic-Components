"""电阻分类下的两端片式模板入口。"""

from backend.agents.step.families._shared import two_terminal_chip as _shared


FAMILY_ID = "resistor/two_terminal_chip"
IMPLEMENTED = True
REQUIRED_PARAMETERS = _shared.REQUIRED_PARAMETERS
REQUIRED_FEATURES = _shared.REQUIRED_FEATURES


def plan_from_evidence(fused, *, source_image_sha256: str):
    if fused.family_id != FAMILY_ID:
        raise ValueError(f"电阻两端片式模板不能处理器件族：{fused.family_id}")
    return _shared.plan_from_evidence(
        fused.model_copy(update={
            "category_id": "resistor", "subcategory_id": None,
        }),
        source_image_sha256=source_image_sha256,
    )


def plan(case: dict, extraction: dict):
    if str(case.get("family_id") or "") != FAMILY_ID:
        raise ValueError(f"电阻两端片式模板不能处理器件族：{case.get('family_id')}")
    return _shared.plan({
        **case, "category_id": "resistor", "subcategory_id": None,
    }, extraction)
