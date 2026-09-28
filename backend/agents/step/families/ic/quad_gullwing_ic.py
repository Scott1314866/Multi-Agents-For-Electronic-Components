"""LQFP、QFP 等四边鸥翼引脚封装规划器。"""

from __future__ import annotations

from typing import Any, Iterable

from backend.agents.step.features import gullwing_lead
from backend.agents.step.ir.schemas import EvidenceCADFeature, EvidenceFeatureIR, EvidenceValue
from backend.agents.step.vision.schemas import FusedEvidence, FusedParameter


FAMILY_ID = "ic/quad_gullwing_ic"

REQUIRED_PARAMETERS: tuple[str, ...] = (
    "nominal_pin_count",
    "total_height",
    "housing_height",
    "body_standoff",
    "overall_length",
    "overall_width",
    "body_length",
    "body_width",
    "terminal_span",
    "terminal_pitch",
    "terminal_length",
    "lead_projection",
    "terminal_width",
    "terminal_thickness",
)

REQUIRED_FEATURES: tuple[str, ...] = (
    "molded_body",
    "gullwing_lead",
    "four_side_lead_array",
)

PARAMETER_GUIDANCE: dict[str, str] = {
    "nominal_pin_count": "标题中的 N-pin 或明确引脚总数",
    "total_height": "符号 A；无 TYP 时选择明确 MAX 作为包络",
    "housing_height": "符号 A2，本体高度；仅在缺失时由总体高度 A 减离板高度 A1 派生，不覆盖明确 A2",
    "body_standoff": "符号 A1；选择明确 MAX 作为包络离板高度",
    "overall_length": "符号 D，含两侧引脚的总体长度",
    "overall_width": "符号 E，含两侧引脚的总体宽度",
    "body_length": "符号 D1，塑封本体长度",
    "body_width": "符号 E1，塑封本体宽度",
    "terminal_span": "符号 D3/E3，同一边首末引脚中心跨距",
    "terminal_pitch": "相邻引脚中心间距；可由跨距和每边数量确定性派生",
    "terminal_length": "符号 L，引脚脚底长度；必须引用脚部尺寸证据，不得用总体与本体尺寸差替代",
    "lead_projection": "引脚总体外伸，可由 (D-D1)/2 与 (E-E1)/2 一致时派生；不是脚底长度 L",
    "terminal_width": "符号 b；优先 TYP，否则选择明确 MAX",
    "terminal_thickness": "符号 c；优先 TYP，否则选择明确 MAX",
}


def _ids(parameters: Iterable[FusedParameter]) -> list[str]:
    return list(dict.fromkeys(
        evidence_id
        for parameter in parameters
        for evidence_id in parameter.evidence_ids
    ))


def _value(parameter: FusedParameter) -> EvidenceValue:
    return EvidenceValue(
        value=parameter.value,
        unit=parameter.unit,
        evidence_ids=list(parameter.evidence_ids),
    )


def plan_from_evidence(
    fused: FusedEvidence,
    *,
    source_image_sha256: str,
) -> dict[str, Any]:
    """把四边鸥翼封装证据规划为本体与四边引脚 Feature IR。"""
    if fused.family_id != FAMILY_ID:
        raise ValueError(f"四边鸥翼规划器不能处理器件族：{fused.family_id}")
    indexed = {item.canonical_name: item for item in fused.parameters}
    missing = [name for name in REQUIRED_PARAMETERS if name not in indexed]
    if missing:
        raise ValueError(f"四边鸥翼 Feature IR 缺少关键参数：{missing}")
    for name in REQUIRED_PARAMETERS:
        item = indexed[name]
        expected_unit = "count" if name == "nominal_pin_count" else "mm"
        if item.unit != expected_unit or not item.evidence_ids or not item.token_bboxes:
            raise ValueError(f"四边鸥翼参数证据或单位无效：{name}")

    count_value = indexed["nominal_pin_count"].value
    count = int(round(count_value))
    if count < 4 or count % 4 or abs(count_value - count) > 1e-9:
        raise ValueError("四边鸥翼引脚数量必须是四的正整数倍")
    if any(indexed[name].value <= 0.0 for name in REQUIRED_PARAMETERS if name != "body_standoff"):
        raise ValueError("四边鸥翼尺寸必须大于零")
    if indexed["body_standoff"].value < 0.0:
        raise ValueError("四边鸥翼离板高度不能为负数")

    body = EvidenceCADFeature(
        feature_id="molded_body",
        feature_type="molded_body_box",
        parameters={
            "length": _value(indexed["body_length"]),
            "width": _value(indexed["body_width"]),
            "height": _value(indexed["housing_height"]),
            "standoff": _value(indexed["body_standoff"]),
        },
    )
    leads = EvidenceCADFeature(
        feature_id="four_side_terminal_array",
        feature_type="quad_gullwing_lead_array",
        parameters={
            "count": _value(indexed["nominal_pin_count"]),
            "pitch": _value(indexed["terminal_pitch"]),
            "terminal_span": _value(indexed["terminal_span"]),
            "overall_length": _value(indexed["overall_length"]),
            "overall_width": _value(indexed["overall_width"]),
            "body_length": _value(indexed["body_length"]),
            "body_width": _value(indexed["body_width"]),
            "foot_length": _value(indexed["terminal_length"]),
            "lead_width": _value(indexed["terminal_width"]),
            "lead_thickness": _value(indexed["terminal_thickness"]),
            "body_standoff": _value(indexed["body_standoff"]),
            "body_height": _value(indexed["housing_height"]),
        },
    )
    overall_length = indexed["overall_length"].value
    overall_width = indexed["overall_width"].value
    total_height = indexed["total_height"].value
    # 四边引脚特征复用相同的 YZ 截面，绕 Z 旋转不改变竖直包络。
    # 直接消费该特征的实际轮廓，避免复制引出高度/板厚等内部几何规则。
    lead_profile = gullwing_lead._profile({
        "lead_thickness": indexed["terminal_thickness"].value,
        "body_width": indexed["body_width"].value,
        "overall_width": overall_width,
        "foot_length": indexed["terminal_length"].value,
        "body_standoff": indexed["body_standoff"].value,
        "body_height": indexed["housing_height"].value,
    }, side=1)
    body_top = indexed["housing_height"].value + indexed["body_standoff"].value
    modeled_zmin = min(indexed["body_standoff"].value, *(z for _, z in lead_profile))
    modeled_zmax = max(body_top, *(z for _, z in lead_profile))
    if modeled_zmax > total_height + 1e-6:
        raise ValueError("按本体及引脚实际尺寸构造的高度超过图纸 A 包络上限")
    return EvidenceFeatureIR(
        source_image_sha256=source_image_sha256,
        family_id=FAMILY_ID,
        category_id="ic",
        subcategory_id=None,
        package_type=fused.package_type,
        coordinate_system=(
            "X/Y 分别沿封装两组相邻边，Z 向上；PCB 安装面为 Z=0，"
            "四边引脚按逆时针排列。"
        ),
        features=[body, leads],
        expected_geometry={
            "solid_count": count + 1,
            "minimum_solid_count": count + 1,
            "minimum_face_count": (count + 1) * 4,
            "height_upper_bound": total_height,
            "bounding_box": {
                "xmin": -overall_length / 2.0,
                "xmax": overall_length / 2.0,
                "ymin": -overall_width / 2.0,
                "ymax": overall_width / 2.0,
                "zmin": modeled_zmin,
                "zmax": modeled_zmax,
            },
        },
        source_dimensions={name: _value(indexed[name]) for name in REQUIRED_PARAMETERS},
        assumptions=[
            "尺寸表没有 TYP 时使用图纸明确 MAX 构建包络模型。",
            "本体按图纸 A2 与 A1 所选名义/实际尺寸建模，顶部为 A2+A1；A 仅作包络高度上限，不回写 A2。",
            "预期竖直包络同时计入本体与实际引脚轮廓，引脚超出本体时使用引脚极值并检查 A 上限。",
            "terminal_pitch 由引脚总数与同边首末引脚跨距确定性计算。",
            "terminal_length 采用图纸脚底长度 L 的证据；lead_projection 表示独立的总体外伸约束。",
            "图纸未标注的丝印、引脚1凹点尺寸和底部散热焊盘不进入模型。",
        ],
    ).model_dump()
