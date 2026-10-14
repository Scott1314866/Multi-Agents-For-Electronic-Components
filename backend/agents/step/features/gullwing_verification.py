"""Check native STEP lead rows; a matching total bbox cannot prove topology."""
from __future__ import annotations
from typing import Any
from backend.agents.step.features.gullwing_layout import ROW_PARAMETERS, ROW_WIDTH_PARAMETERS, row_positions, row_widths, ROW_LENGTH_PARAMETERS, ROW_THICKNESS_PARAMETERS, row_profile_values


def verify_gullwing_rows(shape: Any, parameters: dict, *, require_explicit: bool = False, tolerance: float = .05) -> dict:
    if require_explicit:
        missing=[name for name in (*ROW_PARAMETERS,*ROW_WIDTH_PARAMETERS) if name not in parameters]
        if missing:return {'passed':False,'issues':['来源未确认每侧排布与宽度：'+','.join(missing)],'rows':[]}
    expected_rows=row_positions(parameters);expected_widths=row_widths(parameters)
    lengths=row_profile_values(parameters,ROW_LENGTH_PARAMETERS,'foot_length') if any(n in parameters for n in ROW_LENGTH_PARAMETERS) else None
    thicknesses=row_profile_values(parameters,ROW_THICKNESS_PARAMETERS,'lead_thickness') if any(n in parameters for n in ROW_THICKNESS_PARAMETERS) else None
    solids=list(shape.Solids());bodies=[s for s in solids if abs(s.Center().y)<1e-6]
    if len(bodies)!=1:return {'passed':False,'issues':['无法从回读实体中唯一分离居中本体与两侧引脚'],'rows':[]}
    leads=[s for s in solids if abs(s.Center().y)>=1e-6];issues=[];rows=[]
    for index,((side,positions),width) in enumerate(zip(expected_rows,expected_widths)):
        observed=sorted([s for s in leads if s.Center().y*side>0],key=lambda s:s.Center().x)
        actual_positions=[s.Center().x for s in observed];actual_widths=[s.BoundingBox().xlen for s in observed]
        label='下侧' if side<0 else '上侧'
        if len(observed)!=len(positions):issues.append(f'{label}引脚数量不符：来源 {len(positions)}，回读 {len(observed)}')
        elif any(abs(a-b)>tolerance for a,b in zip(actual_positions,positions)):issues.append(label+'引脚中心位置与来源排布不符')
        if any(abs(w-width)>tolerance for w in actual_widths):issues.append(f'{label}引脚宽度与来源 {width:g} mm 不符')
        measured_lengths=[];measured_thicknesses=[]
        if lengths is not None or thicknesses is not None:
            import cadquery as cq
            outer=float(parameters['overall_width'])/2
            projection=(float(parameters['overall_width'])-float(parameters['body_width']))/2
            for solid in observed:
                x=solid.Center().x
                if lengths is not None:
                    ymin=outer-projection-.1 if side>0 else -outer-.1
                    slab=cq.Solid.makeBox(width+.2,projection+.2,.01,cq.Vector(x-width/2-.1,ymin,.01))
                    measured_lengths.append(solid.intersect(slab).BoundingBox().ylen)
                if thicknesses is not None:
                    ymin=outer-.11 if side>0 else -outer+.09
                    slab=cq.Solid.makeBox(width/2,.02,float(parameters['body_height'])+1,cq.Vector(x-width/4,ymin,0))
                    measured_thicknesses.append(solid.intersect(slab).BoundingBox().zlen)
            if lengths is not None and any(abs(length-lengths[index])>tolerance for length in measured_lengths):
                issues.append(f'{label}脚底长度与来源 {lengths[index]:g} mm 不符')
            if thicknesses is not None and any(abs(thickness-thicknesses[index])>tolerance for thickness in measured_thicknesses):
                issues.append(f'{label}金属厚度与来源 {thicknesses[index]:g} mm 不符')
        rows.append({'side':side,'expected_count':len(positions),'actual_count':len(observed),
                     'expected_positions':positions,'actual_positions':actual_positions,
                     'expected_width':width,'actual_widths':actual_widths,
                     'expected_foot_length':lengths[index] if lengths is not None else None,'actual_foot_lengths':measured_lengths,
                     'expected_thickness':thicknesses[index] if thicknesses is not None else None,'actual_thicknesses':measured_thicknesses})
    return {'passed':not issues,'issues':issues,'rows':rows}
