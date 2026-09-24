"""PaddleOCR 适配器与工程尺寸表达式解析器。"""

from __future__ import annotations

import re
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

import cv2
import numpy as np

from backend.agents.step.vision.schemas import OCRToken, ParsedDimension, ViewRegion
from backend.agents.step.vision.view_splitter import region_for_bbox
from backend.core.logger import get_logger


logger = get_logger(__name__)


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_EXPLICIT_COUNT_PATTERNS = (
    re.compile(
        r"\b(?P<count>\d{1,3})\s*[-–—]?\s*(?:lead|pin|circuit)s?\b",
        re.I,
    ),
    # JEDEC 封装标识中的 Gnn 表示端子总数，例如 R-PDSO-G16。
    # 要求至少包含一个连字符段，避免把版本号或普通尾随数字当成计数。
    re.compile(r"\b(?:[A-Z0-9]+-)+G(?P<count>\d{1,3})\b", re.I),
)


def parse_explicit_count_expression(text: str) -> int | None:
    """从明确的引脚文字或 JEDEC 封装身份中解析端子总数。

    Args:
        text: OCR 原始文本，例如 ``28 PINS SHOWN`` 或 ``R-PDSO-G16``。

    Returns:
        证据明确时返回正整数；修订号、日期及普通数字返回 ``None``。
    """
    normalized = str(text).strip()
    for pattern in _EXPLICIT_COUNT_PATTERNS:
        match = pattern.search(normalized)
        if match:
            count = int(match.group("count"))
            return count if 0 < count <= 512 else None
    return None


def parse_dimension_expression(text: str) -> ParsedDimension:
    """解析英制/公制双标、直径、数量、公差和 REF。

    Args:
        text: OCR 原始文本，例如 ``.500(12.70±0.10)``。

    Returns:
        不带结构语义的尺寸数值；无法识别时 nominal_value 为 ``None``。
    """
    normalized = (
        str(text)
        .strip()
        .replace(",", ".")
        .replace("Φ", "Ø")
        .replace("⌀", "Ø")
        .replace("＋", "+")
        .replace("－", "-")
        .replace("−", "-")
        .replace(" ", " ")
    )
    # ``p1``、``A1`` 等是工程图符号，不是尺寸数值。只允许尺寸表达式中
    # 常见的 BSC、REF、mm、UNC 以及半径前缀 R，避免把符号尾号解析成 1。
    semantic_check = re.sub(
        r"\b(?:BSC|REF|MM|UNC|MIN|MAX|NOM|TYP)\b",
        "",
        normalized,
        flags=re.I,
    )
    semantic_check = re.sub(r"\bR\s*(?=\d)", "", semantic_check, flags=re.I)
    if re.search(r"[A-Za-z]", semantic_check):
        return ParsedDimension(raw_text=text, unit="unknown")
    quantity_match = re.match(r"\s*(\d+)\s*[-×x]\s*(?=[ØR.]|\d)", normalized, re.I)
    quantity = int(quantity_match.group(1)) if quantity_match else None
    symbol = "Ø" if "Ø" in normalized else ("R" if re.search(r"\bR\s*\d", normalized, re.I) else "")
    is_reference = bool(re.search(r"\bREF\b", normalized, re.I))
    parenthesized = re.findall(r"\(([^()]*)\)", normalized)
    if len(parenthesized) > 1:
        return ParsedDimension(
            raw_text=text,
            unit="mm",
            quantity=quantity,
            symbol=symbol,
            is_reference=is_reference,
        )
    value_text = parenthesized[-1] if parenthesized else normalized
    value_text = re.sub(r"\bREF\b", "", value_text, flags=re.I).strip()
    unit = "mm" if parenthesized or re.search(r"\bmm\b", normalized, re.I) else "unknown"

    symmetric = re.search(rf"({_NUMBER})\s*±\s*({_NUMBER})", value_text)
    if symmetric:
        nominal = float(symmetric.group(1))
        tolerance = abs(float(symmetric.group(2)))
        return ParsedDimension(
            raw_text=text,
            nominal_value=nominal,
            minimum_value=nominal - tolerance,
            maximum_value=nominal + tolerance,
            tolerance_plus=tolerance,
            tolerance_minus=-tolerance,
            unit=unit,
            quantity=quantity,
            symbol=symbol,
            is_reference=is_reference,
        )

    asymmetric = re.search(
        rf"({_NUMBER})\s*\+\s*({_NUMBER})\s*/\s*-\s*({_NUMBER})", value_text
    )
    if asymmetric:
        nominal = float(asymmetric.group(1))
        plus = abs(float(asymmetric.group(2)))
        minus = -abs(float(asymmetric.group(3)))
        return ParsedDimension(
            raw_text=text,
            nominal_value=nominal,
            minimum_value=nominal + minus,
            maximum_value=nominal + plus,
            tolerance_plus=plus,
            tolerance_minus=minus,
            unit=unit,
            quantity=quantity,
            symbol=symbol,
            is_reference=is_reference,
        )

    range_match = re.fullmatch(
        rf"\s*({_NUMBER})\s*(?:°|deg)?\s*[-–—]\s*({_NUMBER})\s*(?:°|deg)?\s*",
        value_text,
        flags=re.I,
    )
    if range_match:
        first = float(range_match.group(1))
        second = float(range_match.group(2))
        return ParsedDimension(
            raw_text=text,
            minimum_value=min(first, second),
            maximum_value=max(first, second),
            unit="deg" if "°" in normalized or "deg" in normalized.casefold() else unit,
            quantity=quantity,
            symbol=symbol,
            is_reference=is_reference,
        )

    numbers = re.findall(_NUMBER, value_text)
    nominal = float(numbers[0]) if len(numbers) == 1 else None
    return ParsedDimension(
        raw_text=text,
        nominal_value=nominal,
        unit=unit,
        quantity=quantity,
        symbol=symbol,
        is_reference=is_reference,
    )


def parse_table_nominal_expression(text: str) -> ParsedDimension:
    """解析明确尺寸表数值单元格中的名义值。

    PaddleOCR 有时会把 ``±`` 漏成空格，例如把 ``3.2±0.20 mm`` 识别成
    ``3.2 0.20 mm``。本函数只在调用方已确认 token 是尺寸表名义值单元格时
    使用，只取第一个明确数值，不补写或猜测丢失的公差。
    """
    parsed = parse_dimension_expression(text)
    if parsed.nominal_value is not None:
        return parsed
    normalized = str(text).strip().replace("−", "-").replace("－", "-")
    match = re.fullmatch(
        rf"\s*({_NUMBER})(?:\s+({_NUMBER}))?\s*(mm|BSC|REF)?\s*",
        normalized,
        flags=re.I,
    )
    if match is None:
        return parsed
    return ParsedDimension(
        raw_text=text,
        nominal_value=float(match.group(1)),
        unit="mm" if str(match.group(3) or "").casefold() == "mm" else "unknown",
        is_reference=str(match.group(3) or "").casefold() == "ref",
    )


def infer_document_unit_context(tokens: list[OCRToken]) -> list[OCRToken]:
    """依据图面单位文字或稳定的小数逗号格式补充文档单位上下文。

    该函数不生成尺寸，只给已有 OCR token 标记单位。明确出现 mm/millimeter
    时直接采用毫米；若图面没有英制标记但存在至少三个小数逗号尺寸，则按
    公制工程图记法标记为毫米。证据不足时保持 ``unknown``，交给门禁停止。
    """
    texts = [token.text for token in tokens if token.confidence >= 0.80]
    has_metric = any(re.search(r"\bmm\b|millimeter", text, re.I) for text in texts)
    has_inch = any(re.search(r"\binch(?:es)?\b|\bIN\.?\b", text, re.I) for text in texts)
    comma_decimal_count = sum(
        bool(re.search(r"\d+,\d+", text)) for text in texts
    )
    inferred = has_metric or (not has_inch and comma_decimal_count >= 3)
    if not inferred:
        return tokens
    return [
        token.model_copy(update={"unit_context": "mm"})
        if token.unit_context == "unknown" and re.search(r"\d", token.text)
        else token
        for token in tokens
    ]


def _result_payload(result: Any) -> dict[str, Any] | None:
    """兼容 PaddleOCR 3.x Result 和普通字典。"""
    if isinstance(result, dict):
        payload = result
    else:
        payload = getattr(result, "json", None)
        if callable(payload):
            payload = payload()
    if not isinstance(payload, dict):
        return None
    return payload.get("res", payload)


def _extract_prediction_items(results: Iterable[Any]) -> list[tuple[list[list[float]], str, float]]:
    """把不同 PaddleOCR 版本的结果统一为多边形、文本和置信度。"""
    items: list[tuple[list[list[float]], str, float]] = []
    for result in results:
        payload = _result_payload(result)
        if payload:
            polygons = payload.get("dt_polys") or payload.get("rec_polys") or []
            texts = payload.get("rec_texts") or []
            scores = payload.get("rec_scores") or []
            for polygon, text, score in zip(polygons, texts, scores):
                items.append((np.asarray(polygon).tolist(), str(text), float(score)))
            continue
        if not isinstance(result, (list, tuple)):
            continue
        for row in result:
            if not row or len(row) < 2:
                continue
            polygon, recognition = row[0], row[1]
            if isinstance(recognition, (list, tuple)) and len(recognition) >= 2:
                items.append((np.asarray(polygon).tolist(), str(recognition[0]), float(recognition[1])))
    return items


def _run_engine(engine: Any, image: np.ndarray) -> list[tuple[list[list[float]], str, float]]:
    """执行 PaddleOCR，并兼容 2.x 与 3.x API。"""
    if hasattr(engine, "predict"):
        return _extract_prediction_items(engine.predict(image))
    return _extract_prediction_items(engine.ocr(image, cls=True))


def _restore_point(
    point: tuple[float, float], rotation_deg: int, original_width: int, original_height: int
) -> tuple[float, float]:
    """把旋转 OCR 坐标恢复到预处理图坐标系。"""
    x, y = point
    if rotation_deg == 90:
        return y, original_height - 1.0 - x
    if rotation_deg == -90:
        return original_width - 1.0 - y, x
    return x, y


def _bbox_from_polygon(polygon: list[tuple[float, float]]) -> tuple[int, int, int, int]:
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))


def _iou(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection == 0:
        return 0.0
    area_first = max(1, first[2] - first[0]) * max(1, first[3] - first[1])
    area_second = max(1, second[2] - second[0]) * max(1, second[3] - second[1])
    return intersection / float(area_first + area_second - intersection)


def create_paddle_engine() -> Any:
    """延迟创建 PaddleOCR CPU 引擎，避免单元测试强制加载模型。"""
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PaddleOCR：请在 ima-agent 环境安装 paddleocr 与 paddlepaddle。"
        ) from exc
    try:
        return PaddleOCR(
            lang="en",
            ocr_version="PP-OCRv4",
            device="cpu",
            enable_mkldnn=False,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    except TypeError:
        return PaddleOCR(lang="en", use_angle_cls=True, show_log=False)


def extract_ocr_tokens(
    gray_path: Path,
    regions: list[ViewRegion],
    *,
    engine: Any | None = None,
    rotations: tuple[int, ...] = (0, 90, -90),
) -> list[OCRToken]:
    """提取横向和竖向文字，并保存 bbox、旋转角和置信度。

    Args:
        gray_path: 预处理灰度图路径。
        regions: 本地视图区域。
        engine: 可注入的 OCR 引擎，单元测试可传假实现。
        rotations: OCR 方向，默认原图和两个九十度方向。

    Returns:
        去重并分配稳定 token_id 的 OCR 证据。
    """
    image = cv2.imdecode(np.fromfile(gray_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError(f"无法读取 OCR 图片：{gray_path}")
    height, width = image.shape
    paddle_image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    engine = engine or create_paddle_engine()
    candidates: list[OCRToken] = []
    for scan_round, rotation in enumerate(rotations, start=1):
        if rotation == 90:
            target = cv2.rotate(paddle_image, cv2.ROTATE_90_CLOCKWISE)
        elif rotation == -90:
            target = cv2.rotate(paddle_image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        else:
            target = paddle_image
        started_at = perf_counter()
        predictions = _run_engine(engine, target)
        scan_items: list[dict[str, Any]] = []
        for item_index, (polygon_raw, text, confidence) in enumerate(
            predictions, start=1
        ):
            if not text.strip() or confidence <= 0.0:
                scan_items.append({
                    "token_id": f"scan_{scan_round}_{item_index:04d}",
                    "text": text,
                    "confidence": confidence,
                    "bbox": None,
                    "rotation_deg": rotation,
                    "accepted": False,
                })
                continue
            restored = [
                _restore_point((float(point[0]), float(point[1])), rotation, width, height)
                for point in polygon_raw
            ]
            bbox = _bbox_from_polygon(restored)
            accepted = True
            if rotation != 0:
                restored_width = max(1, bbox[2] - bbox[0])
                restored_height = max(1, bbox[3] - bbox[1])
                # 旋转通道只补充原图中的竖排文字，避免重复识别全部横排内容。
                if restored_height <= restored_width * 1.20:
                    accepted = False
            scan_items.append({
                "token_id": f"scan_{scan_round}_{item_index:04d}",
                "text": text.strip(),
                "confidence": confidence,
                "bbox": bbox,
                "rotation_deg": rotation,
                "accepted": accepted,
            })
            if not accepted:
                continue
            candidates.append(OCRToken(
                token_id="pending",
                text=text.strip(),
                bbox=bbox,
                polygon=restored,
                rotation_deg=rotation,
                confidence=confidence,
                source_region_id=region_for_bbox(bbox, regions),
            ))
        logger.ocr_scan_result(
            scan_round=scan_round,
            source=str(gray_path),
            rotation_deg=rotation,
            elapsed_ms=(perf_counter() - started_at) * 1000.0,
            items=scan_items,
        )
    deduplicated: list[OCRToken] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        duplicate = any(
            candidate.text.casefold() == existing.text.casefold()
            and _iou(candidate.bbox, existing.bbox) >= 0.45
            for existing in deduplicated
        )
        if not duplicate:
            deduplicated.append(candidate)
    deduplicated.sort(key=lambda item: (item.bbox[1], item.bbox[0]))
    finalized = [
        token.model_copy(update={"token_id": f"ocr_{index:04d}"})
        for index, token in enumerate(deduplicated, start=1)
    ]
    logger.ocr_scan_result(
        scan_round="final",
        source=str(gray_path),
        rotation_deg=None,
        items=[
            {
                "token_id": token.token_id,
                "text": token.text,
                "confidence": token.confidence,
                "bbox": token.bbox,
                "rotation_deg": token.rotation_deg,
                "accepted": True,
            }
            for token in finalized
        ],
        event="ocr.finalized",
    )
    return finalized
