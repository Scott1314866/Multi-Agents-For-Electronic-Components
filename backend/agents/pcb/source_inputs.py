"""Bounded PDF/image supplements; page pixels and labels remain source evidence."""
import hashlib
from pathlib import Path
import re
import pymupdf
from backend.agents.pcb.image_input import normalize_image,MAX_IMAGE_BYTES

def decode_supplement(content: bytes,filename: str,hint: str='') -> dict:
    if not content or len(content)>MAX_IMAGE_BYTES:raise ValueError('补充资料为空或超过32 MB')
    filename=Path(filename.replace('\\','/')).name[:200]
    if not (content.startswith(b'%PDF-') or filename.lower().endswith('.pdf')):
        normalized,meta=normalize_image(content)
        return {'images':[normalized],'pages':[],'meta':meta,'filename':filename,'document':None}
    try:
        with pymupdf.open(stream=content,filetype='pdf') as document:
            if document.needs_pass:raise ValueError('PDF有密码，请提供可直接打开的手册')
            if not 1<=len(document)<=200:raise ValueError('PDF最多支持200页，请上传对应器件的手册')
            scored=[]
            tokens=re.findall(r'[a-z]+\d[a-z0-9]{3,}',(filename+' '+hint).lower())
            for i,page in enumerate(document):
                text=page.get_text()[:30000].lower()
                score=0
                for pattern,weight in [(r'(recommended|soldering|mounting|pcb|land).{0,40}(footprint|land pattern)',20),
                                       (r'(package|mechanical).{0,25}(dimension|outline)',12),
                                       (r'pinning information|pin configuration|pin assignment|pin connection',8),
                                       (r'推荐.*焊盘|焊盘.*尺寸',20),(r'封装尺寸|外形尺寸',12),(r'引脚定义|管脚配置',8)]:
                    if re.search(pattern,text,re.S):score+=weight
                if score:scored.append((score,i,any(token in text for token in tokens)))
            matched=[item for item in scored if item[2]]
            if len(matched)>=2:scored=matched
            selected=[i for _,i,_ in sorted(scored,key=lambda item:(-item[0],item[1]))[:4]]
            if not selected:raise ValueError('未找到焊盘／封装尺寸／引脚页，请截图对应页面后补充；扫描版PDF请先提供清晰图片')
            images=[];pages=[]
            for i in selected:
                page=document[i];scale=min(2,2400/max(page.rect.width,page.rect.height))
                if scale<=0:raise ValueError('PDF页面尺寸无效')
                pixels=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
                normalized,_=normalize_image(pixels.tobytes('png'));images.append(normalized);pages.append(i+1)
            return {'images':images,'pages':pages,'filename':filename,'document':content,
                    'meta':{'sha256':hashlib.sha256(content).hexdigest(),'page_count':len(document),'selected_pages':pages}}
    except ValueError:raise
    except Exception as exc:raise ValueError('PDF无法读取，请提供有效手册或尺寸图') from exc
