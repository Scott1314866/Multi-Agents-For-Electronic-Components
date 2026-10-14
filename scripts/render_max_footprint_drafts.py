"""Render reviewed MAX projection parameters, including drafts without pin IDs."""
from __future__ import annotations
from html import escape
from pathlib import Path
import json


def render_draft(spec: dict, target: Path, title: str) -> dict:
    pads=spec['pads']; target.mkdir(parents=True,exist_ok=True)
    span=max(spec['body_x'],spec['body_y'],*[abs(p['x'])*2+p['size_x'] for p in pads],*[abs(p['y'])*2+p['size_y'] for p in pads])
    scale=530/span; cx,cy=350,390
    def xy(x,y):return cx+x*scale,cy-y*scale
    def rect(x,y,w,h,style):
        a,b=xy(x-w/2,y+h/2)
        return f'<rect x="{a}" y="{b}" width="{w*scale}" height="{h*scale}" {style}/>'
    e=['<svg xmlns="http://www.w3.org/2000/svg" width="1260" height="780" viewBox="0 0 1260 780">',
       '<rect width="1260" height="780" fill="white"/>',
       '<g font-family="Arial, sans-serif" fill="#172329">',
       f'<text x="40" y="45" font-size="28" font-weight="bold">{escape(title)}</text>',
       '<text x="40" y="75" font-size="17">MAX terminal projection draft | mm | zero added solder allowance</text>',
       '<text x="40" y="105" font-size="16">P1... are position labels, not electrical pin numbers. Top-view mapping requires review.</text>',
       rect(0,0,spec['body_x'],spec['body_y'],'fill="none" stroke="#717e85" stroke-width="2" stroke-dasharray="9 6"')]
    for x in [-1,0,1]:
        a,b=xy(x*span/2,-span/2); c,d=xy(x*span/2,span/2)
        e.append(f'<path d="M {a} {b} L {c} {d}" stroke="#eff2f4" stroke-width="1"/>')
    for i,p in enumerate(pads,1):
        e.append(rect(p['x'],p['y'],p['size_x'],p['size_y'],'fill="#178b66" stroke="#0c5d42" stroke-width="1.4"'))
        a,b=xy(p['x'],p['y'])
        e.append(f'<text x="{a}" y="{b+6}" font-size="18" text-anchor="middle" fill="white">P{i}</text>')
    e.extend([f'<text x="720" y="170" font-size="20">Body MAX: {spec["body_x"]:g} x {spec["body_y"]:g}</text>',
              f'<text x="720" y="204" font-size="20">Height MAX: {spec["body_height_max"]:g}</text>',
              '<text x="720" y="260" font-size="18" font-weight="bold">Position     Center (X, Y)      Size (X x Y)</text>'])
    for i,p in enumerate(pads,1):
        e.append(f'<text x="720" y="{260+i*42}" font-size="18" font-family="Consolas,monospace">P{i}    ({p["x"]:g}, {p["y"]:g})    {p["size_x"]:g} x {p["size_y"]:g}</text>')
    e.extend(['<text x="40" y="720" font-size="16">Dashed outline: body MAX. Green: draft copper geometry. No electrical numbering assigned.</text>',
              '<text x="40" y="748" font-size="16">Not a manufacturer recommended land pattern or an IPC solder-joint tolerance calculation.</text></g></svg>'])
    svg=target/'max-footprint.svg'; svg.write_text('\n'.join(e),encoding='utf-8')
    import pymupdf
    png=target/'max-footprint.png'
    with pymupdf.open('svg',svg.read_bytes()) as vector:
        with pymupdf.open('pdf',vector.convert_to_pdf()) as document:
            document[0].get_pixmap(alpha=False).save(str(png))
    (target/'max-footprint.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'svg':str(svg.resolve()),'png':str(png.resolve())}


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('input',type=Path);parser.add_argument('output',type=Path);parser.add_argument('--title',default='MAX footprint draft')
    args=parser.parse_args();value=json.loads(args.input.read_text(encoding='utf-8'))
    print(json.dumps(render_draft(value.get('candidate',value),args.output,args.title)))
