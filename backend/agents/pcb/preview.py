"""Render the generated geometry for review; never reconstruct a reference image."""
from __future__ import annotations

from html import escape
from pathlib import Path

from backend.agents.pcb.derive import DerivedPackage
from backend.agents.pcb.spec import PackageSpec


def write_preview(spec: PackageSpec, derived: DerivedPackage, directory: Path) -> dict[str, Path]:
    boxes = []
    for pin in derived.pins:
        pad = derived.pin_pads[pin.number]
        boxes.append((pin.number, pin.x, -pin.y, pad))
    if derived.ep_pad:
        boxes.append((spec.ep_pin_number, 0, 0, derived.ep_pad))
    min_x = min(x - p.mask[0] / 2 for _, x, _, p in boxes)
    max_x = max(x + p.mask[0] / 2 for _, x, _, p in boxes)
    min_y = min(y - p.mask[1] / 2 for _, _, y, p in boxes)
    max_y = max(y + p.mask[1] / 2 for _, _, y, p in boxes)
    min_x, max_x = min(min_x, -derived.silk_span), max(max_x, derived.silk_span)
    min_y, max_y = min(min_y, -derived.silk_half), max(max_y, derived.silk_half)
    margin = .45
    x0, y0 = min_x - margin, min_y - margin
    w, h = max_x - min_x + 2 * margin, max_y - min_y + 2 * margin
    pixel_width = 1200
    pixel_height = round(pixel_width * h / w)
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{pixel_width}" height="{pixel_height}" viewBox="{x0} {y0} {w} {h}">',
                f'<title>{escape(spec.name)} generated copper and soldermask geometry</title>',
                f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="black"/>']
    for number, x, y, pad in boxes:
        for size, color in ((pad.mask, "#740078"), (pad.copper, "#ff202b")):
            sx, sy = size
            if pad.shape == "CIRCLE":
                elements.append(f'<circle cx="{x}" cy="{y}" r="{sx/2}" fill="{color}"/>')
            else:
                elements.append(f'<rect x="{x-sx/2}" y="{y-sy/2}" width="{sx}" height="{sy}" fill="{color}"/>')
        elements.append(f'<text x="{x}" y="{y+.025}" fill="white" text-anchor="middle" font-family="Arial" font-size=".075">{escape(number)}</text>')
    for y in (-derived.silk_half, derived.silk_half):
        elements.append(f'<path d="M {-derived.silk_span} {y} H {derived.silk_span}" stroke="#ffe300" stroke-width="{spec.silk_line_width}" stroke-linecap="round" fill="none"/>')
    if derived.pin1_marker:
        west, east, top, bottom = derived.pin1_marker
        elements.append(f'<path d="M {west} {-top} L {east} {-top} L {west} {-bottom} Z" stroke="#ffe300" stroke-width="{spec.silk_line_width}" fill="none"/>')
    elements.append('</svg>')
    svg = directory / "footprint_preview.svg"
    svg.write_text("\n".join(elements), encoding="utf-8")
    # PyMuPDF is already used by the project's PDF and symbol image workflows.
    import pymupdf
    png = directory / "footprint_preview.png"
    with pymupdf.open("svg", svg.read_bytes()) as vector:
        with pymupdf.open("pdf", vector.convert_to_pdf()) as document:
            document[0].get_pixmap(alpha=False).save(str(png))
    return {"preview_svg": svg, "preview_png": png}
