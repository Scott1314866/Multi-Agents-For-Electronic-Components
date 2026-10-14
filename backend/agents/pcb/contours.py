"""Read relative rectangle topology; physical dimensions always come from labels."""
from __future__ import annotations

from io import BytesIO
from statistics import median

import cv2
import numpy as np
from PIL import Image


def _is_circular(contour) -> bool:
    perimeter = cv2.arcLength(contour, True)
    return perimeter > 0 and 4 * np.pi * cv2.contourArea(contour) / perimeter ** 2 >= .86


def read_four_row_topology(content: bytes) -> dict | None:
    """Detect four uniform perimeter rows; pixels establish counts, never dimensions."""
    image = np.asarray(Image.open(BytesIO(content)).convert("L"))
    _, ink = cv2.threshold(image, 150, 255, cv2.THRESH_BINARY_INV)
    contours, hierarchy = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return None
    boxes = []
    for i, contour in enumerate(contours):
        x, y, w, h = cv2.boundingRect(contour)
        if (hierarchy[0][i][3] >= 0 and min(w, h) >= 3
                and max(w, h) >= max(image.shape) * .025
                and max(w, h) <= max(image.shape) * .2
                and max(w, h) / min(w, h) >= 2
                and cv2.contourArea(contour) / (w * h) >= .6):
            boxes.append((x, y, w, h))
    if len(boxes) < 16:
        return None
    short = median(min(b[2:]) for b in boxes)
    long = median(max(b[2:]) for b in boxes)
    boxes = [b for b in boxes if .75 * short <= min(b[2:]) <= 1.4 * short
             and .8 * long <= max(b[2:]) <= 1.2 * long]

    def rows(vertical):
        groups = []
        axis = 1 if vertical else 0
        selected = [b for b in boxes if (b[3] > b[2]) == vertical]
        for box in sorted(selected, key=lambda b: b[axis] + b[axis + 2] / 2):
            center = box[axis] + box[axis + 2] / 2
            if not groups or abs(center - groups[-1][0]) > short:
                groups.append((center, [box]))
            else:
                groups[-1][1].append(box)
        return [g for _, g in groups if len(g) >= 4]

    horizontal_rows, vertical_rows = rows(True), rows(False)
    if len(horizontal_rows) != 2 or len(vertical_rows) != 2:
        return None
    groups = [*horizontal_rows, *vertical_rows]
    counts = {len(g) for g in groups}
    if len(counts) != 1:
        return None
    for group in groups:
        axis = 0 if group[0][3] > group[0][2] else 1
        centers = sorted(b[axis] + b[axis + 2] / 2 for b in group)
        differences = [b - a for a, b in zip(centers, centers[1:])]
        pitch = median(differences)
        if pitch <= 0 or any(abs(d - pitch) > pitch * .2 for d in differences):
            return None
    top, bottom = horizontal_rows
    left, right = vertical_rows
    if not (max(b[1] + b[3] for b in top) < min(b[1] for b in left + right)
            and min(b[1] for b in bottom) > max(b[1] + b[3] for b in left + right)
            and max(b[0] + b[2] for b in left) < min(b[0] for b in top + bottom)
            and min(b[0] for b in right) > max(b[0] + b[2] for b in top + bottom)):
        return None
    return {"pads_per_side": len(top),
            "evidence": "Four equal, regularly spaced perimeter rows of closed rectangles; pixel topology only"}


def read_two_row_topology(content: bytes) -> dict | None:
    image = np.asarray(Image.open(BytesIO(content)).convert("L"))
    _, ink = cv2.threshold(image, 150, 255, cv2.THRESH_BINARY_INV)
    contours, hierarchy = cv2.findContours(ink, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return None
    boxes = []
    for i, contour in enumerate(contours):
        x, y, w, h = cv2.boundingRect(contour)
        if (hierarchy[0][i][3] >= 0 and image.shape[1] * .012 <= w <= image.shape[1] * .12
                and image.shape[0] * .065 <= h <= image.shape[0] * .45
                and cv2.contourArea(contour) / (w * h) >= .72 and not _is_circular(contour)):
            boxes.append((x, y, w, h))
    if len(boxes) < 4 or len(boxes) % 2:
        return None
    boxes.sort(key=lambda b: b[1] + b[3] / 2)
    count = len(boxes) // 2
    top, bottom = boxes[:count], boxes[count:]
    typical_height = median(b[3] for b in boxes)
    if (max(b[1] for b in top) - min(b[1] for b in top) > typical_height * .5 or
            max(b[1] + b[3] for b in bottom) - min(b[1] + b[3] for b in bottom) > typical_height * .5):
        return None
    top.sort(key=lambda b: b[0] + b[2] / 2)
    bottom.sort(key=lambda b: b[0] + b[2] / 2)
    centers = [x + w / 2 for x, _, w, _ in top]
    pitch = median(b - a for a, b in zip(centers, centers[1:]))
    if pitch <= 0 or any(abs((b - a) - pitch) > pitch * .2 for a, b in zip(centers, centers[1:])):
        return None
    alignments = {}
    bridged = []
    for col, (a, b) in enumerate(zip(top, bottom)):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        if abs((ax + aw / 2) - (bx + bw / 2)) > pitch * .55:
            return None
        if by - (ay + ah) <= typical_height * .15:
            bridged.append(col)
        if abs(aw - bw) > max(2, median(z[2] for z in boxes) * .25):
            differences = {"left": abs(ax - bx), "right": abs(ax + aw - bx - bw),
                           "center": abs(ax + aw / 2 - bx - bw / 2)}
            aligned = min(differences, key=differences.get)
            if differences[aligned] <= max(2, pitch * .1):
                alignments[("bottom" if bw > aw else "top") + "_" + ("left" if col == 0 else "right" if col == count - 1 else str(col))] = aligned
    return {"columns": count, "bridged_columns": bridged, "wide_pad_alignments": alignments,
            "top_boxes": top, "bottom_boxes": bottom,
            "evidence": "Closed rectangle contours paired by row; coordinates are pixel topology, never millimeter measurements"}
