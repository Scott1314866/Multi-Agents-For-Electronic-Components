"""工程图图片预处理：灰度、放大、纠偏、二值化和连通区域清理。"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def image_as_data_url(path: Path) -> str:
    """把本地 PNG/JPEG 编码为视觉模型可读取的 data URL。供视觉大模型（多模态 LLM）作为提示词输入读取

    Args:
        path: 仅限当前工程图链路生成或接收的图片路径。

    Returns:
        包含正确 MIME 类型和 Base64 数据的 URL。

    Raises:
        FileNotFoundError: 图片不存在时抛出。
    """
    if not path.is_file():
        raise FileNotFoundError(f"视觉提示图片不存在：{path}")
    suffix = path.suffix.casefold()
    mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def read_image(path: Path) -> np.ndarray:
    """把图片读入内存为 OpenCV 图像数组，供本地图像处理/预处理管线（如裁剪、缩放、标注检测等）使用

    Args:
        path: 输入工程图路径。

    Returns:
        OpenCV BGR 图片。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件不是可解码图片。
    """
    if not path.is_file():
        raise FileNotFoundError(f"工程图图片不存在：{path}")
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法解码工程图图片：{path}")
    return image


def _estimate_skew(binary: np.ndarray) -> float:
    """根据前景像素估计小角度倾斜。"""
    points = np.column_stack(np.where(binary < 128))
    if len(points) < 100:
        return 0.0
    angle = float(cv2.minAreaRect(points[:, ::-1].astype(np.float32))[-1])
    if angle > 45.0:
        angle -= 90.0
    return angle if abs(angle) <= 8.0 else 0.0


def _remove_tiny_components(binary: np.ndarray) -> tuple[np.ndarray, int]:
    """移除孤立噪点，同时保留文字笔画和尺寸线。"""
    foreground = (binary < 128).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
    keep = stats[:, cv2.CC_STAT_AREA] >= 3
    keep[0] = False  # Background is not a foreground component.
    cleaned = np.where(keep[labels], 0, 255).astype(np.uint8)
    kept = int(np.count_nonzero(keep))
    return cleaned, kept


def save_image(path: Path, image: np.ndarray) -> None:
    """用支持中文路径的方式保存 OpenCV 图片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix or ".png"
    ok, encoded = cv2.imencode(suffix, image)
    if not ok:
        raise RuntimeError(f"图片编码失败：{path}")
    encoded.tofile(path)


def preprocess_image(
    image: np.ndarray,
    output_dir: Path,
    *,
    scale: float = 2.0,
) -> dict[str, Any]:
    """生成 OCR 和几何检测共用的预处理图片。

    Args:
        image: 原始 RGB 图片。
        output_dir: 预处理证据输出目录。
        scale: 放大倍率，默认两倍。

    Returns:
        图片路径、像素尺寸、纠偏角度和连通区域数量。
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    enlarged = cv2.resize(
        gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
    )
    initial_binary = cv2.adaptiveThreshold(
        enlarged,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        41,
        15,
    )
    angle = _estimate_skew(initial_binary)
    height, width = enlarged.shape
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    deskewed = cv2.warpAffine(
        enlarged,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    binary = cv2.adaptiveThreshold(
        deskewed,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        41,
        15,
    )
    cleaned, component_count = _remove_tiny_components(binary)
    output_dir.mkdir(parents=True, exist_ok=True)
    gray_path = output_dir / "preprocessed_gray.png"
    binary_path = output_dir / "preprocessed_binary.png"
    save_image(gray_path, deskewed)
    save_image(binary_path, cleaned)
    return {
        "gray_path": str(gray_path),
        "binary_path": str(binary_path),
        "width": width,
        "height": height,
        "scale": scale,
        "deskew_angle_deg": angle,
        "connected_component_count": component_count,
    }
