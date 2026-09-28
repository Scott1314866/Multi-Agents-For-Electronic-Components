"""SOIC、TSSOP 等双侧对称鸥翼引脚器件族规划器。"""

from __future__ import annotations

import re
from typing import Any, Iterable

from backend.agents.step.drawing.gate import extraction_dimensions
from backend.agents.step.ir.schemas import (
    CADFeature,
    DrawingFeatureIR,
    EvidenceCADFeature,
    EvidenceFeatureIR,
    EvidenceValue,
)
from backend.agents.step.vision.ocr import (
    parse_dimension_expression,
    parse_explicit_count_expression,
)
from backend.agents.step.vision.schemas import (
    FusedEvidence,
    FusedParameter,
    QwenViewSemanticResult,
    SemanticAssignment,
    parameter_has_traceable_source,
)


FAMILY_ID = "ic/gullwing_ic"

# 这里只声明参数名称和单位合同，不保存任何具体封装或 Golden 尺寸。
REQUIRED_PARAMETERS: tuple[str, ...] = (
    "nominal_pin_count",
    "terminal_pitch",
    "pin_span",
    "total_height",
    "housing_height",
    "body_standoff",
    "overall_width",
    "body_width",
    "body_length",
    "terminal_length",
    "terminal_thickness",
    "terminal_width",
)

OPTIONAL_PARAMETERS: tuple[str, ...] = (
    "mold_draft_angle_top_deg",
    "mold_draft_angle_bottom_deg",
    "lead_angle_deg",
)

REQUIRED_FEATURES: tuple[str, ...] = (
    "molded_body",
    "gullwing_lead",
    "two_side_lead_array",
)

PARAMETER_GUIDANCE: dict[str, str] = {
    "nominal_pin_count": "明确的 N PINS/N-Lead 总引脚数",
    "terminal_pitch": "相邻引脚中心间距或 boxed/basic pitch",
    "pin_span": "同侧首末引脚中心跨距；缺失时可由数量和间距派生",
    "total_height": "侧视图从 Seating Plane 到封装顶部的总体高度；无 NOM/TYP 时选明确 MAX 包络",
    "housing_height": "塑封本体高度；缺失时可由总体高度减离板高度派生",
    "body_standoff": "Seating Plane 到本体底面的距离；只有 MIN 时引用该明确值",
    "overall_width": "跨两排引脚外缘的总体宽度；无 NOM 时选 MAX",
    "body_width": "俯视图塑封本体两边之间的宽度范围；无 NOM 时选 MAX",
    "body_length": "沿引脚排列方向的塑封本体长度；引脚数矩阵选择当前引脚列 MAX",
    "terminal_length": "Gage Plane 细节中引脚脚长范围；无 NOM 时选 MAX",
    "terminal_thickness": "Gage Plane 细节中引脚板厚范围；无 NOM 时选 MAX，GD&T 共面度框不是板厚",
    "terminal_width": "引脚宽度；无 NOM 时选 MAX",
    "mold_draft_angle_top_deg": "可选；只接受塑封本体的明确拔模角或 α 标注，不能使用 Gage Plane 的引脚角",
    "mold_draft_angle_bottom_deg": "可选；只接受另一塑封方向的明确拔模角或 β 标注，不得复制首个角度",
    "lead_angle_deg": "可选；Gage Plane/Seating Plane 附近金属引脚脚部倾角，仅保留审计，不是塑封拔模角",
}

# 同一 Family 的不同图纸可能把尺寸分散在俯视、端视、侧视和局部详图中。
# 这里只声明字段职责，不包含任何型号或尺寸答案。
VIEW_PARAMETER_GROUPS: dict[str, tuple[str, ...]] = {
    "top": (
        "nominal_pin_count", "terminal_pitch", "pin_span",
        "overall_width", "body_width", "body_length",
    ),
    "front": (
        "nominal_pin_count", "total_height", "housing_height", "body_standoff",
        "terminal_pitch", "terminal_width", "terminal_thickness",
    ),
    "side": (
        "total_height", "housing_height", "body_standoff", "terminal_length",
        "terminal_thickness", "mold_draft_angle_top_deg",
        "mold_draft_angle_bottom_deg", "lead_angle_deg",
    ),
    "detail": (
        "terminal_length", "terminal_thickness", "terminal_width",
        "mold_draft_angle_top_deg", "mold_draft_angle_bottom_deg",
        "lead_angle_deg",
    ),
}

_EXPECTED_UNITS: dict[str, str] = {
    "nominal_pin_count": "count",
    "mold_draft_angle_top_deg": "deg",
    "mold_draft_angle_bottom_deg": "deg",
    "lead_angle_deg": "deg",
}


def _explicit_mold_angle_text(name: str, texts: Iterable[str]) -> bool:
    """仅凭所引用的文字确认塑封角标注，不把裸角度范围当作塑封证据。"""
    text = " ".join(texts).casefold()
    symbol = "α" if name == "mold_draft_angle_top_deg" else "β"
    symbol_name = "alpha" if symbol == "α" else "beta"
    if symbol in text or re.search(rf"\b{symbol_name}\b", text):
        return True
    return bool(re.search(r"\bmold(?:ed|ing)?\s+draft\b|\bdraft\s+angle\b|塑封.*拔模|拔模角", text))


def reconcile_view_semantics(
    result: QwenViewSemanticResult,
    evidence: dict[str, Any],
) -> QwenViewSemanticResult:
    """按两侧鸥翼封装的投影关系校正 Qwen 证据绑定。

    Args:
        result: Qwen 对当前单视图的语义结果。
        evidence: 当前 Prompt 中的尺寸组和 OCR token 子集。

    Returns:
        只重排现有证据 ID 的语义结果；不创建或修改任何尺寸数值。
    """
    token_index = {
        item["token_id"]: item for item in evidence.get("ocr_tokens", [])
    }

    def group_values(group: dict[str, Any]) -> list[float]:
        values: list[float] = []
        for token_id in group.get("token_ids", []):
            token = token_index.get(token_id)
            if token is None:
                continue
            parsed = parse_dimension_expression(str(token.get("text", "")))
            value = (
                parsed.maximum_value
                if parsed.maximum_value is not None
                else parsed.nominal_value
            )
            if value is not None:
                values.append(float(value))
        return values

    groups = [
        group
        for group in evidence.get("dimension_groups", [])
        if group.get("evidence_type") == "dimension_line" and group_values(group)
    ]

    def overlaps(first: dict[str, Any], second: dict[str, Any]) -> bool:
        a = first["bbox"]
        b = second["bbox"]
        if not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3]):
            return True
        x_overlap = min(a[2], b[2]) - max(a[0], b[0])
        y_overlap = min(a[3], b[3]) - max(a[1], b[1])
        vertical_gap = max(0, max(a[1], b[1]) - min(a[3], b[3]))
        horizontal_gap = max(0, max(a[0], b[0]) - min(a[2], b[2]))
        average_height = ((a[3] - a[1]) + (b[3] - b[1])) / 2.0
        average_width = ((a[2] - a[0]) + (b[2] - b[0])) / 2.0
        return (
            x_overlap > 0 and vertical_gap <= max(24.0, average_height)
        ) or (
            y_overlap > 0 and horizontal_gap <= max(24.0, average_width)
        )

    def clusters_for(orientation: str) -> list[list[dict[str, Any]]]:
        clusters: list[list[dict[str, Any]]] = []
        for group in [item for item in groups if item.get("orientation") == orientation]:
            values = group_values(group)
            texts = [
                str(token_index[token_id]["text"]).strip()
                for token_id in group.get("token_ids", [])
                if token_id in token_index
            ]
            if values and texts and all(text.isdigit() for text in texts):
                continue
            target = next(
                (cluster for cluster in clusters if any(overlaps(group, item) for item in cluster)),
                None,
            )
            if target is None:
                clusters.append([group])
            else:
                target.append(group)
        return clusters

    def assignment(name: str, cluster: list[dict[str, Any]]) -> SemanticAssignment:
        return SemanticAssignment(
            canonical_name=name,
            token_ids=list(dict.fromkeys(
                token_id for group in cluster for token_id in group.get("token_ids", [])
            )),
            line_ids=list(dict.fromkeys(
                line_id
                for group in cluster
                for line_id in [
                    *group.get("dimension_line_ids", []),
                    *group.get("extension_line_ids", []),
                ]
            )),
            target_feature=f"family_projection_constraint:{name}",
            confidence=min(float(group.get("confidence", 0.9)) for group in cluster),
        )

    def cluster_max(cluster: list[dict[str, Any]]) -> float:
        return max(value for group in cluster for value in group_values(group))

    mold_names = {"mold_draft_angle_top_deg", "mold_draft_angle_bottom_deg"}
    lead_context = bool(re.search(
        r"gage\s*plane|seating\s*plane|lead\s*angle|引脚|脚部",
        " ".join(str(token.get("text", "")) for token in token_index.values()),
        re.I,
    ))
    assignments = []
    for item in result.assignments:
        if item.canonical_name not in mold_names:
            assignments.append(item)
            continue
        referenced_ids = list(item.token_ids)
        for group in evidence.get("dimension_groups", []):
            if set(item.token_ids).intersection(group.get("token_ids", [])):
                referenced_ids.extend(group.get("context_token_ids", []))
                referenced_ids.extend(group.get("row_label_token_ids", []))
        referenced_ids = list(dict.fromkeys(
            token_id for token_id in referenced_ids if token_id in token_index
        ))
        if _explicit_mold_angle_text(
            item.canonical_name,
            (str(token_index[token_id].get("text", "")) for token_id in referenced_ids),
        ):
            assignments.append(item.model_copy(update={
                "token_ids": referenced_ids,
                "target_feature": f"explicit_mold_draft:{item.canonical_name}",
            }))
        elif lead_context and not any(
            existing.canonical_name == "lead_angle_deg" for existing in assignments
        ):
            assignments.append(item.model_copy(update={
                "canonical_name": "lead_angle_deg",
                "target_feature": "gullwing_lead:foot_angle",
            }))
    normalized_view = result.view_type.casefold()
    identity_groups = [
        group
        for group in evidence.get("dimension_groups", [])
        if group.get("evidence_type") == "identity_text"
        and any(
            token_id in token_index
            and parse_explicit_count_expression(
                str(token_index[token_id].get("text", ""))
            ) is not None
            for token_id in group.get("token_ids", [])
        )
    ]
    identity_counts = {
        count
        for group in identity_groups
        for token_id in group.get("token_ids", [])
        if token_id in token_index
        for count in [parse_explicit_count_expression(
            str(token_index[token_id].get("text", ""))
        )]
        if count is not None
    }
    if len(identity_counts) == 1 and identity_groups:
        assignments = [
            item for item in assignments
            if item.canonical_name != "nominal_pin_count"
        ]
        assignments.append(assignment("nominal_pin_count", [identity_groups[0]]))
    if "top" in normalized_view:
        horizontal = clusters_for("horizontal")
        vertical = clusters_for("vertical")
        across, along = (
            (vertical, horizontal)
            if len(vertical) >= 2 and horizontal
            else (horizontal, vertical)
            if len(horizontal) >= 2 and vertical
            else ([], [])
        )
        if across and along:
            across = sorted(across, key=cluster_max, reverse=True)[:2]
            overall_cluster, body_cluster = across[0], across[1]
            along_cluster = max(along, key=cluster_max)
            replaced = {"overall_width", "body_width", "body_length"}
            assignments = [
                item for item in assignments if item.canonical_name not in replaced
            ]
            assignments.extend([
                assignment("overall_width", overall_cluster),
                assignment("body_width", body_cluster),
                assignment("body_length", along_cluster),
            ])

        texts = {
            token_id: str(token.get("text", "")).strip()
            for token_id, token in token_index.items()
        }
        has_pin_one = any("pin 1" in text.casefold() for text in texts.values())
        integer_groups = [
            group
            for group in groups
            if group_values(group)
            and all(
                texts.get(token_id, "").isdigit()
                for token_id in group.get("token_ids", [])
            )
        ]
        integer_values = [
            (max(group_values(group)), group) for group in integer_groups
        ]
        if has_pin_one and len(integer_values) >= 2:
            count_group = max(integer_values, key=lambda item: item[0])[1]
            assignments = [
                item for item in assignments if item.canonical_name != "nominal_pin_count"
            ]
            assignments.append(assignment("nominal_pin_count", [count_group]))

        # 某些封装图把局部 Gage Plane 详图排在主俯视图右侧，视图切分器会
        # 将二者保留在同一区域。Qwen 此时容易只返回主视图尺寸。这里仅按
        # 投影位置和尺寸簇关系补全语义绑定，所有数值仍来自已有 OCR token。
        assigned_names = {item.canonical_name for item in assignments}
        region_bbox = evidence.get("region", {}).get("bbox", [0, 0, 1, 1])
        region_left = float(region_bbox[0])
        region_top = float(region_bbox[1])
        region_width = max(1.0, float(region_bbox[2]) - region_left)
        region_height = max(1.0, float(region_bbox[3]) - region_top)

        def group_center(group: dict[str, Any]) -> tuple[float, float]:
            box = group["bbox"]
            return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

        def group_texts(group: dict[str, Any]) -> list[str]:
            return [
                str(token_index[token_id].get("text", "")).strip()
                for token_id in group.get("token_ids", [])
                if token_id in token_index
            ]

        # 视图切分器可能把主俯视图和其下方的高度投影视图合并为一个
        # ``top`` 裁区。此时 Qwen 容易把 ``A MAX`` 总高误写成脚长，并把
        # A1 离板高度误写成端子厚度。俯视裁区不负责脚长/板厚；这两个
        # 字段应由独立侧视或局部详图提供，因此先删除跨投影误绑定。
        assignments = [
            item for item in assignments
            if item.canonical_name not in {
                "terminal_length", "terminal_thickness"
            }
        ]
        assigned_names = {item.canonical_name for item in assignments}

        # ``MAX`` 是总体高度的明确工程语义，而不是尺寸答案。只在同一裁区
        # 已存在带尺寸线的数值组时，将距离 MAX 标记最近的尺寸组绑定为
        # total_height；实际数值仍完全来自 OCR token。
        max_markers = [
            token for token in token_index.values()
            if re.search(r"\bMAX\b", str(token.get("text", "")), re.I)
        ]
        total_height_group: dict[str, Any] | None = None
        if max_markers:
            height_candidates = [
                group for group in groups
                if 0.0 < cluster_max([group]) < 5.0
                and group_center(group)[1] > region_top + region_height * 0.55
            ]
            if height_candidates:
                def marker_distance(group: dict[str, Any]) -> float:
                    center_x, center_y = group_center(group)
                    return min(
                        (center_x - (token["bbox"][0] + token["bbox"][2]) / 2.0) ** 2
                        + (center_y - (token["bbox"][1] + token["bbox"][3]) / 2.0) ** 2
                        for token in max_markers
                    )

                total_height_group = min(height_candidates, key=marker_distance)
                assignments = [
                    item for item in assignments
                    if item.canonical_name != "total_height"
                ]
                assignments.append(
                    assignment("total_height", [total_height_group])
                )
                assigned_names.add("total_height")

        # A1 离板高度通常以 MIN/MAX 两个值堆叠在下方高度视图左侧。
        # 将同 X 列的尺寸组聚类，并选择位于总体高度左侧的最左双值列。
        # 这利用的是投影位置和尺寸线证据，不包含任何封装型号或默认尺寸。
        if total_height_group is not None:
            total_x, total_y = group_center(total_height_group)
            standoff_candidates = [
                group for group in groups
                if group is not total_height_group
                and 0.0 < cluster_max([group]) < cluster_max([total_height_group])
                and group_center(group)[0] < total_x - region_width * 0.10
                and group_center(group)[1] > total_y - region_height * 0.02
            ]
            standoff_columns: list[list[dict[str, Any]]] = []
            for group in sorted(
                standoff_candidates, key=lambda item: group_center(item)[0]
            ):
                center_x = group_center(group)[0]
                column = next(
                    (
                        items for items in standoff_columns
                        if abs(
                            center_x
                            - sum(group_center(item)[0] for item in items)
                            / len(items)
                        ) <= region_width * 0.035
                    ),
                    None,
                )
                if column is None:
                    standoff_columns.append([group])
                else:
                    column.append(group)
            paired_columns = [
                column for column in standoff_columns if len(column) >= 2
            ]
            if paired_columns:
                standoff_column = min(
                    paired_columns,
                    key=lambda column: sum(
                        group_center(group)[0] for group in column
                    ) / len(column),
                )
                assignments = [
                    item for item in assignments
                    if item.canonical_name != "body_standoff"
                ]
                assignments.append(
                    assignment("body_standoff", standoff_column)
                )
                assigned_names.add("body_standoff")

                # 靠近总体高度标注的另一组双值列对应引脚宽度上下限。
                # 该规则不依赖 Qwen 是否先识别出 pitch/terminal_width，
                # 只使用当前视图中的尺寸组位置和证据 ID。
                width_columns = [
                    column for column in paired_columns
                    if column is not standoff_column
                ]
                if width_columns and "terminal_width" not in assigned_names:
                    width_column = min(
                        width_columns,
                        key=lambda column: abs(
                            total_x
                            - sum(group_center(group)[0] for group in column)
                            / len(column)
                        ),
                    )
                    assignments.append(
                        assignment("terminal_width", width_column)
                    )
                    assigned_names.add("terminal_width")

        pitch_assignment = next(
            (
                item for item in assignments
                if item.canonical_name == "terminal_pitch"
            ),
            None,
        )
        if pitch_assignment is not None and "terminal_width" not in assigned_names:
            pitch_tokens = [
                token_index[token_id]
                for token_id in pitch_assignment.token_ids
                if token_id in token_index
            ]
            pitch_values = [
                value
                for token in pitch_tokens
                for value in [
                    parse_dimension_expression(str(token.get("text", ""))).maximum_value
                    or parse_dimension_expression(str(token.get("text", ""))).nominal_value
                ]
                if value is not None
            ]
            pitch_boxes = [token["bbox"] for token in pitch_tokens]
            if pitch_values and pitch_boxes:
                pitch_value = max(float(value) for value in pitch_values)
                pitch_center_x = sum(
                    (box[0] + box[2]) / 2.0 for box in pitch_boxes
                ) / len(pitch_boxes)
                pitch_center_y = sum(
                    (box[1] + box[3]) / 2.0 for box in pitch_boxes
                ) / len(pitch_boxes)
                width_candidates = [
                    group for group in groups
                    if 0.0 < cluster_max([group]) < pitch_value
                    and group_center(group)[0] > pitch_center_x + region_width * 0.08
                    and abs(group_center(group)[1] - pitch_center_y) <= region_height * 0.08
                ]
                if width_candidates:
                    width_cluster = max(
                        width_candidates,
                        key=lambda group: cluster_max([group]),
                    )
                    assignments.append(assignment("terminal_width", [width_cluster]))
                    assigned_names.add("terminal_width")

        angle_pattern = re.compile(
            r"^\s*\d+(?:[.,]\d+)?\s*[°º]?\s*[-–]\s*"
            r"\d+(?:[.,]\d+)?\s*[°º]?\s*$"
        )
        angle_groups = [
            group for group in groups
            if any(angle_pattern.match(text) for text in group_texts(group))
            and group_center(group)[0] > region_left + region_width * 0.5
        ]
        if angle_groups and lead_context and "lead_angle_deg" not in assigned_names:
            angle_cluster = max(angle_groups, key=lambda group: cluster_max([group]))
            assignments.append(assignment("lead_angle_deg", [angle_cluster]))
            assigned_names.add("lead_angle_deg")

        detail_groups = [
            group for group in groups
            if 0.0 < cluster_max([group]) < 2.5
            and group_center(group)[0] > region_left + region_width * 0.55
            and group_center(group)[1] > region_top + region_height * 0.18
            and group not in angle_groups
            and not all(text.isdigit() for text in group_texts(group))
        ]
        detail_columns: list[list[dict[str, Any]]] = []
        for group in sorted(detail_groups, key=lambda item: group_center(item)[0]):
            center_x = group_center(group)[0]
            target = next(
                (
                    column for column in detail_columns
                    if abs(
                        center_x
                        - sum(group_center(item)[0] for item in column) / len(column)
                    ) <= region_width * 0.06
                ),
                None,
            )
            if target is None:
                detail_columns.append([group])
            else:
                target.append(group)

        if len(detail_columns) >= 2:
            ordered_columns = sorted(detail_columns, key=cluster_max)
            if "terminal_thickness" not in assigned_names:
                assignments.append(
                    assignment("terminal_thickness", ordered_columns[0])
                )
                assigned_names.add("terminal_thickness")
            if "terminal_length" not in assigned_names:
                assignments.append(
                    assignment("terminal_length", ordered_columns[-1])
                )
                assigned_names.add("terminal_length")

        # 当前 top 裁区一旦通过明确的 MAX 上下文识别出下方高度投影，说明
        # 端子板厚和脚长应由独立 side/detail 区域负责。禁止上面的通用详图
        # 兜底再次添加这两个字段，避免与正确侧视证据形成随机冲突。
        if total_height_group is not None:
            assignments = [
                item for item in assignments
                if item.canonical_name not in {
                    "terminal_thickness", "terminal_length"
                }
            ]

    if ("side" in normalized_view or "detail" in normalized_view) and any(
        item.canonical_name.endswith("_deg") for item in assignments
    ):
        linear_clusters = [
            *clusters_for("horizontal"),
            *clusters_for("vertical"),
            *clusters_for("oblique"),
        ]
        linear_clusters = [
            cluster
            for cluster in linear_clusters
            if 0.0 < cluster_max(cluster) < 5.0
        ]
        if len(linear_clusters) >= 2:
            ordered = sorted(linear_clusters, key=cluster_max)
            thickness_cluster = ordered[0]
            length_cluster = ordered[-1]
            assigned_names = {item.canonical_name for item in assignments}
            if "terminal_thickness" not in assigned_names:
                assignments.append(assignment("terminal_thickness", thickness_cluster))
            if "terminal_length" not in assigned_names:
                assignments.append(assignment("terminal_length", length_cluster))

    if "composite" in normalized_view:
        # 低对比度图纸可能只能保留整页复合区域。主俯视图中的三个大尺寸簇
        # 通常按“沿引脚方向、本体跨排方向、引脚总宽”从左到右布置；右侧
        # 局部详图则包含板厚和脚长。以下规则只重排已有证据，不读取型号值。
        region_bbox = evidence.get("region", {}).get("bbox", [0, 0, 1, 1])
        region_left = float(region_bbox[0])
        region_top = float(region_bbox[1])
        region_width = max(1.0, float(region_bbox[2]) - region_left)
        region_height = max(1.0, float(region_bbox[3]) - region_top)

        def center_of_group(group: dict[str, Any]) -> tuple[float, float]:
            box = group["bbox"]
            return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)

        def raw_group_texts(group: dict[str, Any]) -> list[str]:
            return [
                str(token_index[token_id].get("text", "")).strip()
                for token_id in group.get("token_ids", [])
                if token_id in token_index
            ]

        major_groups = [
            group for group in groups
            if 2.5 < cluster_max([group]) < 15.0
            and center_of_group(group)[0] < region_left + region_width * 0.58
            and region_top + region_height * 0.20
            < center_of_group(group)[1]
            < region_top + region_height * 0.58
            and not all(text.isdigit() for text in raw_group_texts(group))
        ]
        major_clusters: list[list[dict[str, Any]]] = []
        for group in sorted(major_groups, key=lambda item: center_of_group(item)[0]):
            center_x = center_of_group(group)[0]
            target = next(
                (
                    cluster for cluster in major_clusters
                    if abs(
                        center_x
                        - sum(center_of_group(member)[0] for member in cluster)
                        / len(cluster)
                    ) <= region_width * 0.04
                ),
                None,
            )
            if target is None:
                major_clusters.append([group])
            else:
                target.append(group)

        if len(major_clusters) == 3:
            ordered_major = sorted(
                major_clusters,
                key=lambda cluster: sum(
                    center_of_group(group)[0] for group in cluster
                ) / len(cluster),
            )
            assignments = [
                item for item in assignments
                if item.canonical_name not in {
                    "body_length", "body_width", "overall_width"
                }
            ]
            assignments.extend([
                assignment("body_length", ordered_major[0]),
                assignment("body_width", ordered_major[1]),
                assignment("overall_width", ordered_major[2]),
            ])

        pitch_assignment = next(
            (
                item for item in assignments
                if item.canonical_name == "terminal_pitch"
            ),
            None,
        )
        if pitch_assignment is not None:
            pitch_tokens = [
                token_index[token_id]
                for token_id in pitch_assignment.token_ids
                if token_id in token_index
            ]
            pitch_values = [
                value
                for token in pitch_tokens
                for value in [
                    parse_dimension_expression(str(token.get("text", ""))).maximum_value
                    or parse_dimension_expression(str(token.get("text", ""))).nominal_value
                ]
                if value is not None
            ]
            if pitch_tokens and pitch_values:
                pitch_center_x = sum(
                    (token["bbox"][0] + token["bbox"][2]) / 2.0
                    for token in pitch_tokens
                ) / len(pitch_tokens)
                pitch_center_y = sum(
                    (token["bbox"][1] + token["bbox"][3]) / 2.0
                    for token in pitch_tokens
                ) / len(pitch_tokens)
                pitch_value = max(float(value) for value in pitch_values)
                width_candidates = [
                    group for group in groups
                    if 0.0 < cluster_max([group]) < pitch_value
                    and center_of_group(group)[0]
                    > pitch_center_x + region_width * 0.08
                    and abs(center_of_group(group)[1] - pitch_center_y)
                    <= region_height * 0.08
                ]
                if width_candidates:
                    width_group = max(
                        width_candidates,
                        key=lambda group: cluster_max([group]),
                    )
                    assignments = [
                        item for item in assignments
                        if item.canonical_name != "terminal_width"
                    ]
                    assignments.append(assignment("terminal_width", [width_group]))

        angle_pattern = re.compile(
            r"^\s*\d+(?:[.,]\d+)?\s*[°º]?\s*[-–]\s*"
            r"\d+(?:[.,]\d+)?\s*[°º]?\s*$"
        )
        detail_groups = [
            group for group in groups
            if 0.0 < cluster_max([group]) < 2.5
            and center_of_group(group)[0] > region_left + region_width * 0.58
            and center_of_group(group)[1] < region_top + region_height * 0.58
            and not any(
                angle_pattern.match(text) for text in raw_group_texts(group)
            )
        ]
        if detail_groups:
            thickness_group = min(
                detail_groups, key=lambda group: cluster_max([group])
            )
            length_group = max(
                detail_groups, key=lambda group: cluster_max([group])
            )
            assignments = [
                item for item in assignments
                if item.canonical_name not in {
                    "terminal_thickness", "terminal_length"
                }
            ]
            assignments.extend([
                assignment("terminal_thickness", [thickness_group]),
                assignment("terminal_length", [length_group]),
            ])

        lower_height_groups = [
            group for group in groups
            if 0.0 < cluster_max([group]) < 2.5
            and center_of_group(group)[1]
            > region_top + region_height * 0.60
        ]
        if len(lower_height_groups) >= 2:
            total_group = max(
                lower_height_groups, key=lambda group: cluster_max([group])
            )
            total_center = center_of_group(total_group)
            standoff_candidates = [
                group for group in lower_height_groups
                if group is not total_group
                and cluster_max([group]) < cluster_max([total_group])
            ]
            if standoff_candidates:
                standoff_group = min(
                    standoff_candidates,
                    key=lambda group: (
                        (center_of_group(group)[0] - total_center[0]) ** 2
                        + (center_of_group(group)[1] - total_center[1]) ** 2
                    ),
                )
                assignments = [
                    item for item in assignments
                    if item.canonical_name not in {
                        "total_height", "body_standoff"
                    }
                ]
                assignments.extend([
                    assignment("total_height", [total_group]),
                    assignment("body_standoff", [standoff_group]),
                ])

        assigned_names = {item.canonical_name for item in assignments}
        angle_groups = [
            group for group in groups
            if any(angle_pattern.match(text) for text in raw_group_texts(group))
        ]
        if angle_groups and lead_context and "lead_angle_deg" not in assigned_names:
            angle_cluster = max(angle_groups, key=lambda group: cluster_max([group]))
            assignments.append(assignment("lead_angle_deg", [angle_cluster]))
            assigned_names.add("lead_angle_deg")

    return result.model_copy(update={"assignments": assignments})


def _evidence_ids(parameters: Iterable[FusedParameter]) -> list[str]:
    """按原顺序合并派生数值依赖的证据 ID。"""
    return list(dict.fromkeys(
        evidence_id
        for parameter in parameters
        for evidence_id in parameter.evidence_ids
    ))


def _value(
    value: float,
    unit: str,
    parameters: Iterable[FusedParameter],
) -> EvidenceValue:
    """创建保留 OCR、表格或几何证据来源的 IR 数值。"""
    return EvidenceValue(
        value=float(value),
        unit=unit,
        evidence_ids=_evidence_ids(parameters),
    )


def plan_from_evidence(
    fused: FusedEvidence,
    *,
    source_image_sha256: str,
) -> dict[str, Any]:
    """把通过门禁的鸥翼封装证据规划为参数化 Feature IR。

    Args:
        fused: 已完成数值解析、语义关联和冲突检查的图片证据。
        source_image_sha256: 唯一输入工程图图片的 SHA256。

    Returns:
        完整塑封角证据使用 ``drafted_body_loft``，否则使用明确待审核的
        ``molded_body_box`` 包络；引脚主要尺寸始终要求证据完整。

    Raises:
        ValueError: 器件族不匹配、缺少关键字段、单位不符、证据为空，
            或参数不能形成有效几何时抛出。
    """
    if fused.family_id != FAMILY_ID:
        raise ValueError(
            f"鸥翼封装规划器不能处理器件族：{fused.family_id or '<empty>'}"
        )
    if not source_image_sha256:
        raise ValueError("鸥翼封装 Feature IR 缺少源图片 SHA256")

    indexed = {parameter.canonical_name: parameter for parameter in fused.parameters}
    missing = [name for name in REQUIRED_PARAMETERS if name not in indexed]
    if missing:
        raise ValueError(f"鸥翼封装 Feature IR 缺少关键参数：{missing}")

    for name in REQUIRED_PARAMETERS:
        parameter = indexed[name]
        if not parameter_has_traceable_source(parameter):
            raise ValueError(f"鸥翼封装参数缺少可追溯证据：{name}")
        expected_unit = _EXPECTED_UNITS.get(name, "mm")
        if parameter.unit != expected_unit:
            raise ValueError(
                f"鸥翼封装参数单位错误：{name}={parameter.unit}，"
                f"预期 {expected_unit}"
            )

    def source(name: str) -> EvidenceValue:
        """读取一个已验证参数，并保留其原始证据链。"""
        parameter = indexed[name]
        return _value(parameter.value, parameter.unit, [parameter])

    count_value = indexed["nominal_pin_count"].value
    count = int(round(count_value))
    if count < 4 or count % 2 or abs(count_value - count) > 1e-9:
        raise ValueError("双侧对称鸥翼封装引脚数量必须是大于等于 4 的偶数")

    positive_dimensions = (
        "terminal_pitch",
        "pin_span",
        "total_height",
        "housing_height",
        "overall_width",
        "body_width",
        "body_length",
        "terminal_length",
        "terminal_thickness",
        "terminal_width",
    )
    invalid = [name for name in positive_dimensions if indexed[name].value <= 0.0]
    if invalid:
        raise ValueError(f"鸥翼封装参数必须大于零：{invalid}")
    if indexed["body_standoff"].value < 0.0:
        raise ValueError("鸥翼封装本体离板高度不能为负数")

    valid_optional = {
        name: indexed[name]
        for name in OPTIONAL_PARAMETERS
        if name in indexed
        and indexed[name].unit == "deg"
        and indexed[name].evidence_ids
        and indexed[name].token_bboxes
        and 0.0 <= indexed[name].value < 90.0
        and (name == "lead_angle_deg" or _explicit_mold_angle_text(name, indexed[name].raw_texts))
    }
    draft_names = ("mold_draft_angle_top_deg", "mold_draft_angle_bottom_deg")
    has_explicit_draft = all(
        name in valid_optional
        and _explicit_mold_angle_text(name, valid_optional[name].raw_texts)
        for name in draft_names
    )
    if has_explicit_draft:
        top_tokens = set(valid_optional[draft_names[0]].token_ids)
        bottom_tokens = set(valid_optional[draft_names[1]].token_ids)
        # 不能把同一组角度证据复制成两个独立塑封角。
        has_explicit_draft = bool(top_tokens - bottom_tokens and bottom_tokens - top_tokens)
    body_parameters = {
        "length": source("body_length"),
        "width": source("body_width"),
        "height": source("housing_height"),
        "standoff": source("body_standoff"),
    }
    assumptions = []
    if has_explicit_draft:
        body_parameters.update({
            "draft_top_deg": source("mold_draft_angle_top_deg"),
            "draft_bottom_deg": source("mold_draft_angle_bottom_deg"),
        })
    else:
        assumptions.append("未标注完整塑封拔模细节，输出本体包络，需人工审核。")
    if "lead_angle_deg" in valid_optional:
        assumptions.append("引脚倾角仅保留为审计证据，未用作塑封拔模参数。")
    body = EvidenceCADFeature(
        feature_id="molded_body",
        feature_type="drafted_body_loft" if has_explicit_draft else "molded_body_box",
        parameters=body_parameters,
    )
    leads = EvidenceCADFeature(
        feature_id="two_side_terminal_array",
        feature_type="gullwing_lead_array",
        parameters={
            "count": source("nominal_pin_count"),
            "pitch": source("terminal_pitch"),
            "row_span": source("pin_span"),
            "overall_width": source("overall_width"),
            "body_width": source("body_width"),
            "foot_length": source("terminal_length"),
            "lead_width": source("terminal_width"),
            "lead_thickness": source("terminal_thickness"),
            "body_standoff": source("body_standoff"),
            "body_height": source("housing_height"),
        },
    )

    body_length = indexed["body_length"].value
    overall_width = indexed["overall_width"].value
    total_height = indexed["total_height"].value
    expected_geometry = {
        "solid_count": count + 1,
        "minimum_solid_count": count + 1,
        # 任意闭合三维实体至少包含四个面；这里只做与型号无关的拓扑下限检查。
        "minimum_face_count": (count + 1) * 4,
        "bounding_box": {
            "xmin": -body_length / 2.0,
            "xmax": body_length / 2.0,
            "ymin": -overall_width / 2.0,
            "ymax": overall_width / 2.0,
            "zmin": 0.0,
            "zmax": total_height,
        },
    }
    return EvidenceFeatureIR(
        source_image_sha256=source_image_sha256,
        family_id=FAMILY_ID,
        category_id="ic",
        subcategory_id=None,
        package_type=fused.package_type,
        coordinate_system=(
            "X 沿单侧引脚排列方向，Y 沿两排引脚跨距方向，Z 向上；"
            "PCB 安装面为 Z=0。"
        ),
        features=[body, leads],
        expected_geometry=expected_geometry,
        source_dimensions={
            name: source(name) for name in (*REQUIRED_PARAMETERS, *valid_optional)
        },
        assumptions=assumptions,
    ).model_dump()


def plan(case: dict[str, Any], extraction: dict[str, Any]) -> dict[str, Any]:
    """把已验证的鸥翼封装尺寸规划为本体和两侧引脚特征。"""
    indexed = extraction_dimensions(extraction)
    values = {name: float(item["nominal_value"]) for name, item in indexed.items()}
    body = CADFeature(
        feature_id="molded_body",
        feature_type="drafted_body_loft",
        parameters={
            "length": values["body_length"],
            "width": values["body_width"],
            "height": values["housing_height"],
            "standoff": values["body_standoff"],
            "draft_top_deg": values["mold_draft_angle_top_deg"],
            "draft_bottom_deg": values["mold_draft_angle_bottom_deg"],
        },
    )
    leads = CADFeature(
        feature_id="two_side_terminal_array",
        feature_type="gullwing_lead_array",
        parameters={
            "count": int(values["nominal_pin_count"]),
            "pitch": values["terminal_pitch"],
            "row_span": values["pin_span"],
            "overall_width": values["overall_width"],
            "body_width": values["body_width"],
            "foot_length": values["terminal_length"],
            "lead_width": values["terminal_width"],
            "lead_thickness": values["terminal_thickness"],
            "body_standoff": values["body_standoff"],
            "body_height": values["housing_height"],
        },
    )
    expected = {
        "solid_count": 1 + int(values["nominal_pin_count"]),
        "minimum_face_count": int(case["expected_topology"]["minimum_face_count"]),
        "bounding_box": {
            "xmin": -values["body_length"] / 2.0,
            "xmax": values["body_length"] / 2.0,
            "ymin": -values["overall_width"] / 2.0,
            "ymax": values["overall_width"] / 2.0,
            "zmin": 0.0,
            "zmax": values["total_height"],
        },
    }
    return DrawingFeatureIR(
        case_id=case["case_id"],
        family_id=FAMILY_ID,
        category_id="ic",
        subcategory_id=None,
        part_type=case["expected_part_type"],
        package_type=case["expected_package_type"],
        coordinate_system=(
            "X 沿单侧引脚排列方向，Y 沿两排引脚跨距方向，Z 向上；PCB 安装面为 Z=0。"
        ),
        features=[body, leads],
        expected_geometry=expected,
        source_dimensions=values,
        assumptions=list(case.get("modeling_assumptions", [])),
    ).model_dump()
