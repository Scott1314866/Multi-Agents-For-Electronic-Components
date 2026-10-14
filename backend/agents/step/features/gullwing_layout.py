"""Validate two gullwing rows without inferring odd-count topology."""
from __future__ import annotations

import math
from typing import Any

ROW_PARAMETERS = (
    'negative_side_pin_count', 'positive_side_pin_count',
    'negative_side_pin_span', 'positive_side_pin_span',
)
ROW_WIDTH_PARAMETERS = ('negative_side_terminal_width', 'positive_side_terminal_width')
ROW_LENGTH_PARAMETERS = ('negative_side_terminal_length', 'positive_side_terminal_length')
ROW_THICKNESS_PARAMETERS = ('negative_side_terminal_thickness', 'positive_side_terminal_thickness')
ROW_PROFILE_PARAMETERS = (*ROW_LENGTH_PARAMETERS,*ROW_THICKNESS_PARAMETERS)


def row_profile_values(parameters: dict[str, Any], names: tuple[str,str], fallback: str) -> list[float]:
    explicit=any(name in parameters for name in names)
    if explicit and not all(name in parameters for name in names):
        raise ValueError('上下侧尺寸需要分别确认：'+','.join(names))
    values=[float(parameters[name]) for name in names] if explicit else [float(parameters[fallback])]*2
    if any(not math.isfinite(value) or value<=0 for value in values):
        raise ValueError(','.join(names)+':尺寸必须是大于零的有限毫米值')
    return values


def validate_profile_envelopes(values: dict[str, Any]) -> None:
    pairs=((ROW_LENGTH_PARAMETERS,'terminal_length'),(ROW_THICKNESS_PARAMETERS,'terminal_thickness'))
    for pair,default in pairs:
        if default in values and (not any(name in values for name in pair) or all(name in values for name in pair)):
            row_profile_values(values,pair,default)
    if not all(name in values for name in ROW_LENGTH_PARAMETERS) or not {'overall_width','body_width','terminal_thickness'}.issubset(values):
        return
    if any(name in values for name in ROW_THICKNESS_PARAMETERS) and not all(name in values for name in ROW_THICKNESS_PARAMETERS):
        return
    lengths=row_profile_values(values,ROW_LENGTH_PARAMETERS,'terminal_length')
    thicknesses=row_profile_values(values,ROW_THICKNESS_PARAMETERS,'terminal_thickness')
    projection=(float(values['overall_width'])-float(values['body_width']))/2
    for name,thickness_name,length,thickness in zip(ROW_LENGTH_PARAMETERS,ROW_THICKNESS_PARAMETERS,lengths,thicknesses):
        if length<=thickness or length>=projection+min(.03,thickness*.2):
            raise ValueError(f'{name},{thickness_name},body_width,overall_width:脚底长度应大于金属厚度且位于该侧外伸范围内')


def row_widths(parameters: dict[str, Any]) -> list[float]:
    """Each row can have its own strip width; legacy uniform arrays still work."""
    explicit=any(name in parameters for name in ROW_WIDTH_PARAMETERS)
    if explicit and not all(name in parameters for name in ROW_WIDTH_PARAMETERS):
        raise ValueError('两侧独立引脚宽度需要同时填写：'+','.join(ROW_WIDTH_PARAMETERS))
    widths=[float(parameters[name]) for name in ROW_WIDTH_PARAMETERS] if explicit else [float(parameters['lead_width'])]*2
    if any(not math.isfinite(w) or w<=0 for w in widths):
        raise ValueError('两侧引脚宽度必须是大于零的有限毫米值')
    return widths


def validate_row_envelopes(parameters: dict[str, Any]) -> None:
    for (side,positions),width,name in zip(row_positions(parameters),row_widths(parameters),ROW_WIDTH_PARAMETERS):
        span_name='negative_side_pin_span' if side<0 else 'positive_side_pin_span'
        count_name='negative_side_pin_count' if side<0 else 'positive_side_pin_count'
        if len(positions)>1 and width>=min(b-a for a,b in zip(positions,positions[1:]))-1e-9:
            raise ValueError(f'{name}>={span_name}/({count_name}-1):同排引脚宽度不能达到或超过相邻中心间距')
        if 'body_length' in parameters and positions[-1]-positions[0]+width>float(parameters['body_length'])+.05:
            raise ValueError(f'{span_name}+{name}>body_length:该侧首末中心跨度加宽度超过本体长度')


def row_positions(parameters: dict[str, Any]) -> list[tuple[int, list[float]]]:
    count_value=float(parameters['count'])
    if not math.isfinite(count_value) or not count_value.is_integer() or not 2<=count_value<=1000:
        raise ValueError('nominal_pin_count:引脚总数必须是 2 到 1000 的整数')
    count=int(count_value)
    pitch=float(parameters['pitch']);span=float(parameters['row_span'])
    if not math.isfinite(pitch) or pitch<=0 or not math.isfinite(span) or span<0:
        raise ValueError('pin_span/terminal_pitch:跨度或间距无效')
    explicit=any(name in parameters for name in ROW_PARAMETERS)
    if explicit:
        missing=[name for name in ROW_PARAMETERS if name not in parameters]
        if missing:raise ValueError('两排独立布局缺少参数：'+','.join(missing))
        counts=[float(parameters[name]) for name in ROW_PARAMETERS[:2]]
        if any(not math.isfinite(n) or not n.is_integer() or n<1 for n in counts):
            raise ValueError('negative_side_pin_count/positive_side_pin_count:每排数量必须为正整数')
        if sum(counts)!=count:
            raise ValueError('negative_side_pin_count+positive_side_pin_count!=nominal_pin_count')
        spans=[float(parameters[name]) for name in ROW_PARAMETERS[2:]]
        if any(not math.isfinite(s) or s<0 for s in spans):
            raise ValueError('negative_side_pin_span/positive_side_pin_span:每排跨度不能为负数')
        if abs(max(spans)-span)>.05:
            raise ValueError('pin_span!=max(negative_side_pin_span,positive_side_pin_span)')
    else:
        if count%2:raise ValueError('奇数引脚需要明确两排的数量和首末中心跨度，不能补成偶数')
        expected_span=(count//2-1)*pitch
        counts=[count//2]*2;spans=[expected_span]*2
        if abs(span-expected_span)>.05:
            raise ValueError('pin_span!=terminal_pitch*(pins_per_side-1)')
    rows=[]
    for side,n,s,name in zip((-1,1),counts,spans,ROW_PARAMETERS[2:]):
        n=int(n)
        if n==1:
            if abs(s)>1e-9:raise ValueError(name+':单脚排的首末中心跨度必须为 0')
            positions=[0.0]
        else:
            spacing=s/(n-1)
            multiple=round(spacing/pitch)
            if multiple<1 or abs(spacing-multiple*pitch)>.05:
                raise ValueError(name+':同排间距必须为 terminal_pitch 的正整数倍')
            positions=[-s/2+i*spacing for i in range(n)]
        rows.append((side,positions))
    return rows
