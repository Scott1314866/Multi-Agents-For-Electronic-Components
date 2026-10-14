"""Evidence-based follow-up questions and deterministic human corrections."""
from copy import deepcopy
import math
import re

LABELS={'name':'封装名称','family':'封装类型','body_x':'本体 X 尺寸','body_y':'本体 Y 尺寸',
        'body_height_max':'本体最大高度','ep_x':'散热盘宽度','ep_y':'散热盘长度','ep_pin_number':'散热盘脚号'}

def plan_questions(state: dict) -> list[dict]:
    spec=state.get('spec') or {};reading=state.get('image_reading') or {}
    facts=state.get('human_clarification_facts') or {};questions=[]
    def add(key,title,kind,help,**extra):
        if key not in facts:questions.append(dict(id=key,title=title,type=kind,help=help,**extra))
    if reading.get('unsupported_reason'):
        return []
    if reading.get('source_units','unknown')=='unknown':
        add('units','核对表中的所有数值是否已换算为毫米？','choice','请核对原图单位；不能仅确认按钮就把英寸或 mil 当作毫米。',
            options=[{'value':'mm','label':'已确认，表中数值均为毫米'}])
    pads=spec.get('pads') or []
    if pads and not reading.get('grid'):
        rows=[i+1 for i,p in enumerate(pads) if p.get('shape') not in {'RECTANGLE','CIRCLE'}]
        if rows:add('pad_shape','这些未确认形状的铜焊盘是圆形还是矩形？','choice','形状按正式推荐焊盘图确认；圆盘的宽和长均表示直径。',rows=rows,
                    options=[{'value':'CIRCLE','label':'圆形铜焊盘'},{'value':'RECTANGLE','label':'矩形铜焊盘'}])
    uncertain=set(reading.get('uncertain_fields') or [])
    missing_numbers=[i+1 for i,p in enumerate(pads) if not p.get('number')]
    numbering_evidence=(reading.get('evidence') or {}).get('pin_numbering') or {}
    if missing_numbers or pads and ('pin_numbering' in uncertain or numbering_evidence.get('confidence',0)<.9):
        add('pin_numbers','这些焊盘的脚号分别是什么？','numbers','按下方焊盘表的行顺序填写全部脚号，用逗号或空格分隔。不知道时请补充顶视引脚图，不按数量猜编号。',
            count=len(pads),rows=missing_numbers,example=', '.join(str(p.get('number') or '?') for p in pads))
    pin_evidence=(reading.get('evidence') or {}).get('pin_numbering') or {}
    evidence_text=(pin_evidence.get('text','')+' '+pin_evidence.get('view','')).lower()
    explicitly_top=any(word in evidence_text for word in ('top','顶视','mapped','converted','转换')) and pin_evidence.get('confidence',0)>=.9
    if reading.get('mechanical_outline'):
        explicitly_top=reading['mechanical_outline'].get('view')=='top' and pin_evidence.get('confidence',0)>=.9
    if reading.get('pin_reference'):
        explicitly_top=True
    if pads and not reading.get('grid') and not explicitly_top:
        first=pads[0]
        x=first.get('x') if isinstance(first.get('x'),(int,float)) else 0
        y=first.get('y') if isinstance(first.get('y'),(int,float)) else 0
        position=('左' if x<0 else '右' if x>0 else '中')+('上' if y>0 else '下' if y<0 else '中')
        add('view','现在的焊盘坐标是否已经转换为 PCB 顶视图？','choice',
            f'当前第1行焊盘 {first.get("number") or "未编号"} 在{position}侧；对照透明顶视引脚图核对。镜像时脚号随焊盘移动，不重新猜编号。',
            options=[{'value':'top','label':'已是顶视，保留当前坐标'},
                     {'value':'mirror_x','label':'按编号图左右镜像（X 取反）'},
                     {'value':'mirror_y','label':'按编号图上下镜像（Y 取反）'}])
    for field in ('name','family','body_x','body_y','body_height_max'):
        evidence=(reading.get('evidence') or {}).get(field) or {}
        if spec.get(field) is None or spec.get(field)=='' or field in uncertain or evidence and evidence.get('confidence',0)<.9:
            add(field,f'请补充{LABELS[field]}'+('（mm）' if field.startswith('body_') else ''),
                'number' if field.startswith('body_') else 'text',
                '请从器件本体尺寸表读取，长度用标称值、高度用最大值；不能用焊盘外缘跨度代替本体。' if field.startswith('body_') else '请按手册封装标题填写，型号不能代替封装尺寸。',field=field)
    if spec.get('ep_present') is None:
        add('ep_present','器件是否有独立的中央散热焊盘？','choice','普通信号盘或地焊球不等于独立散热盘。',
            options=[{'value':'no','label':'没有独立散热盘'},{'value':'yes','label':'有，需要填写尺寸'}])
    if spec.get('ep_present'):
        for field in ('ep_x','ep_y','ep_pin_number'):
            if not spec.get(field):add(field,'请补充'+LABELS[field],'text' if field=='ep_pin_number' else 'number','按手册填写；散热盘脚号不能与信号脚重复。',field=field)
    for field,label in (('size_x','铜焊盘宽度／直径'),('size_y','铜焊盘长度')):
        rows=[i+1 for i,p in enumerate(pads) if p.get(field) is None]
        if rows:
            add('pad_'+field,f'第 {"、".join(map(str,rows[:6]))}'+(' 等' if len(rows)>6 else '')+f'行的{label}是多少（mm）？',
                'number','只有这些缺失行采用同一尺寸时才填写统一值；尺寸不同请逐行修改下方表格。请使用正式推荐焊盘尺寸，器件焊球直径不能代替铜盘直径。',field=field,rows=rows)
    if (state.get('image_reading') or {}).get('grid',{}):
        grid=reading.get('grid') or {}
        if grid.get('bridged_columns'):
            add('pad_layout_policy','图中的上下连通地焊盘怎样生成？','choice','拆分只改变连通列的纵向坐标和长度，保留原始来源及已核对脚号。',
                options=[{'value':'independent','label':'按普通盘长度拆分为独立焊盘'},{'value':'source','label':'保留原图连通结构（当前不支持不同脚号重叠）'}])
    known_widths=sorted({p['size_x'] for p in pads if isinstance(p.get('size_x'),(int,float)) and p['size_x']>0})
    if known_widths:
        add('manufacturing','本次铜焊盘尺寸采用图纸值还是工艺调整值？','manufacturing',
            '图纸原值始终保留。工艺调整仅修改匹配原宽度的焊盘，圆形盘同时同步直径；不改变间距，也不修改其他宽度的特殊盘。',widths=known_widths)
    return questions[:4]

def _positive(value,label):
    if isinstance(value,bool):raise ValueError(label+'必须是大于零的毫米数值')
    try:number=float(value)
    except (TypeError,ValueError):raise ValueError(label+'必须填写数值') from None
    if not math.isfinite(number) or number<=0:raise ValueError(label+'必须是大于零的毫米数值')
    return number

def validate_clarification(request: dict,answer: dict) -> dict:
    values=answer.get('values')
    if not isinstance(values,dict) or not values:raise ValueError('请回答至少一个追问，或上传补充资料')
    questions={q['id']:q for q in request.get('questions') or []}
    if set(values)-set(questions):raise ValueError('追问已变化，请刷新当前问题后回答')
    normalized={}
    for key,value in values.items():
        question=questions[key];kind=question['type'];label=question['title']
        if kind=='number':value=_positive(value,label)
        elif kind=='choice':
            if value not in {o['value'] for o in question['options']}:raise ValueError('请选择当前追问提供的选项')
        elif kind=='manufacturing':
            if not isinstance(value,dict) or value.get('mode') not in {'source','process'}:raise ValueError('请选择图纸值或工艺调整')
            value={'mode':value['mode'],**({'from':_positive(value.get('from'),'原宽度'),'to':_positive(value.get('to'),'工艺宽度')} if value['mode']=='process' else {})}
            if value['mode']=='process' and not any(abs(value['from']-w)<1e-6 for w in question['widths']):raise ValueError('原宽度不在当前图纸尺寸中，请核对')
        elif kind=='numbers':
            if isinstance(value,str):value=re.split(r'[,，、;；\s]+',value.strip())
            if not isinstance(value,list) or len(value)!=question['count'] or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,32}',n) for n in value):raise ValueError(f'请按焊盘表顺序填写 {question["count"]} 个有效脚号')
            if len(set(value))!=len(value):raise ValueError('脚号重复，请按顶视引脚定义核对')
            source=(request.get('spec') or {}).get('pads') or []
            certain=(request.get('evidence') or {}).get('pin_numbering',{}).get('confidence',0)>=.9
            if certain and any(p.get('number') and p['number']!=n for p,n in zip(source,value)):raise ValueError('已有来源脚号也发生修改，请先在完整焊盘表核对后提交')
        else:
            if not isinstance(value,str) or not value.strip() or len(value)>80:raise ValueError(label+'不能为空且最多80字符')
            value=value.strip()
            if key in {'name','ep_pin_number'} and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}' if key=='name' else r'[A-Za-z0-9_]{1,32}',value):raise ValueError('名称／脚号只能使用字母、数字或下划线')
        normalized[key]=value
    return normalized

def apply_clarification(state: dict,request: dict,answer: dict) -> dict:
    spec=deepcopy(state.get('spec') or {});facts=dict(state.get('human_clarification_facts') or {})
    changes=[];questions={q['id']:q for q in request['questions']}
    pads=spec.get('pads') or []
    for key,value in answer['values'].items():
        q=questions[key];facts[key]=value
        if key=='pin_numbers':
            for p,n in zip(pads,value):p['number']=n
            spec['pin_numbering']=value
        elif key=='pad_shape':
            for row in q['rows']:
                p=pads[row-1];p['shape']=value
                if value=='CIRCLE' and p.get('size_x') is not None and p.get('size_y') is not None and p['size_x']!=p['size_y']:
                    raise ValueError('已有宽／长不同，不能直接改为圆盘；请补充正确直径并在焊盘表核对')
        elif key=='ep_present':spec['ep_present']=value=='yes'
        elif key=='view' and value!='top':
            axis='x' if value=='mirror_x' else 'y'
            for p in pads:
                if isinstance(p.get(axis),(int,float)):p[axis]=-p[axis]
        elif key.startswith('pad_size_'):
            field=q['field']
            for row in q['rows']:
                p=pads[row-1];p[field]=value
                if p.get('shape')=='CIRCLE':p['size_x']=p['size_y']=value
        elif key=='manufacturing' and value['mode']=='process':
            for i,p in enumerate(pads):
                if isinstance(p.get('size_x'),(int,float)) and abs(p['size_x']-value['from'])<1e-6:
                    fields=('size_x','size_y') if p.get('shape')=='CIRCLE' else ('size_x',)
                    for field in fields:
                        if p.get(field)!=value['to']:changes.append({'row':i+1,'number':p.get('number'),'field':field,'source_value':p.get(field),'generated_value':value['to'],'unit':'mm','source':'human_input'})
                        p[field]=value['to']
        elif q.get('field') in LABELS:spec[q['field']]=value
    if facts.get('pad_layout_policy')=='independent':
        from backend.agents.pcb.image_input import FootprintGrid,pads_from_grid
        grid=FootprintGrid.model_validate(request['grid']);calculated=pads_from_grid(grid,split_bridges=True)
        for i,p in enumerate(pads):
            if i%len(grid.bottom_numbers) in grid.bridged_columns:
                p.update(y=calculated[i]['y'],size_y=calculated[i]['size_y'])
    # Once enough fields exist, reject physical conflicts rather than treating an
    # answer as permission to bypass the ordinary generation checks.
    from backend.agents.pcb.image_input import validate_image_spec
    from backend.agents.pcb.derive import check_clearances
    try:complete=validate_image_spec(spec)
    except ValueError:complete=None
    if complete:
        issues=check_clearances(complete)
        if issues:raise ValueError('工艺调整未通过铜箔间距校验：'+'；'.join(issues[:3]))
    return {'spec':spec,'human_clarification_facts':facts,
            'human_manufacturing_adjustments':[*state.get('human_manufacturing_adjustments',[]),*changes]}
