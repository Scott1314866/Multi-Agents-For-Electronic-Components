"""Cropped mechanical diagrams: count external terminal rectangles, not mm."""
from __future__ import annotations
from io import BytesIO
from statistics import median
import cv2
import numpy as np
from PIL import Image


def has_dimension_table(content: bytes) -> bool:
    """Require an actual ruled table before the MAX-table reader can claim values."""
    image=np.asarray(Image.open(BytesIO(content)).convert('L'))
    ink=(image<=185).astype(np.uint8)*255
    h,w=image.shape
    horizontal=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(40,int(w*.4)),1)))
    vertical=cv2.morphologyEx(ink,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(1,max(35,int(h*.25)))))
    def components(mask):
        count,_,stats,_=cv2.connectedComponentsWithStats(mask)
        return sum(stats[i,cv2.CC_STAT_AREA]>20 for i in range(1,count))
    return components(horizontal)>=6 and components(vertical)>=3


def read_outline_rows(content: bytes) -> dict | None:
    image=np.asarray(Image.open(BytesIO(content)).convert('L'))
    if max(image.shape)>800:return None
    ink=image<=185
    contours,hierarchy=cv2.findContours(ink.astype(np.uint8)*255,cv2.RETR_TREE,cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:return None
    boxes=[]
    for i,c in enumerate(contours):
        x,y,w,h=cv2.boundingRect(c)
        if min(w,h)>=12 and hierarchy[0][i][3]>=0 and cv2.contourArea(c)/(w*h)>=.78:
            boxes.append((x,y,w,h))
    bodies=[b for b in boxes if b[2]>=80 and b[3]>=35 and 1.3<=b[2]/b[3]<=3.5]
    if not bodies:return None
    body=max(bodies,key=lambda b:b[2]*b[3]);bx,by,bw,bh=body
    rows={side:[b for b in boxes if bx-12<=b[0]+b[2]/2<=bx+bw+12 and
          (b[1]+b[3]<=by+3 if side=='top' else b[1]>=by+bh-3)] for side in ('top','bottom')}
    if not rows['top'] or not rows['bottom']:return None
    standard=median(sorted(b[2] for group in rows.values() for b in group)[:min(3,sum(map(len,rows.values())))])
    rows={side:sorted(group,key=lambda b:b[0]) for side,group in rows.items()}
    for side,group in rows.items():
        if all(.7*standard<=b[2]<=1.3*standard for b in group):
            sample=group[0]
            y=int(median(b[1]+b[3]*.35 for b in group));height=max(3,int(median(b[3] for b in group)*.25))
            edges=np.flatnonzero(ink[y:y+height,:].mean(axis=0)>.7)
            edges=[x for x in edges if bx-12<=x<=bx+bw+12]
            centers=[]
            for left in edges:
                for right in edges:
                    if .8*standard<=right-left<=1.2*standard:
                        center=(left+right)/2
                        if not any(abs(center-old)<standard*.6 for old in centers):centers.append(center)
            if 1<=len(centers)<=3:
                rows[side]=[(round(c-standard/2),sample[1],round(standard),sample[3]) for c in sorted(centers)]
    for side,group in rows.items():
        if len(group)==2 and all(.7*standard<=b[2]<=1.3*standard for b in group):
            center=median(b[0]+b[2]/2 for b in group)
            # An overprinted dimension line can open the central rectangle's
            # contour. Require both matching vertical edges in the same row.
            y=int(median(b[1]+b[3]*.35 for b in group));height=max(3,int(median(b[3] for b in group)*.25))
            strip=ink[y:y+height,:]
            edges=np.flatnonzero(strip.mean(axis=0)>.7)
            left=[x for x in edges if abs(x-(center-standard/2))<=standard*.2]
            right=[x for x in edges if abs(x-(center+standard/2))<=standard*.2]
            if left and right and any(.7*standard<=r-l<=1.3*standard for l in left for r in right):
                sample=group[0]
                group.append((round(center-standard/2),sample[1],round(standard),sample[3]))
                group.sort(key=lambda b:b[0])
    narrow=[b[0]+b[2]/2 for group in rows.values() for b in group if b[2]<1.8*standard]
    positions=[]
    for x in sorted(narrow):
        if not positions or x-positions[-1]>standard*.5:positions.append(x)
    if len(positions)!=3:return None
    pitch=median(b-a for a,b in zip(positions,positions[1:]))
    if pitch<=standard or abs((positions[1]-positions[0])-(positions[2]-positions[1]))>pitch*.15:return None
    center=(positions[0]+positions[-1])/2
    result={}
    for side,group in rows.items():
        result[side]=[{'pitch_index':round((b[0]+b[2]/2-center)/pitch), 'wide':b[2]>1.8*standard} for b in group]
    if {len(result['top']),len(result['bottom'])} not in ({1,3},{2,3}):return None
    heights=[b[3] for group in rows.values() for b in group]
    uniform=not any(p['wide'] for points in result.values() for p in points) and max(heights)/min(heights)<=1.3
    return {'rows':result,'uniform_standard_terminals':uniform,
            'evidence':'Primary-image closed rectangles and paired vertical edges; counts/pitch slots only, no pixel-to-mm conversion'}
