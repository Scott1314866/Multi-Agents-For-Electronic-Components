"""工程图文件读取和 Golden Case 加载工具。"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_GOLDEN_SET_PATH = PROJECT_ROOT / "tests" / "golden" / "step_drawing_golden_set.json"


def load_golden_cases(path: Path = DEFAULT_GOLDEN_SET_PATH) -> dict[str, dict[str, Any]]:
    """读取 Golden Set，并按 case_id 建立索引。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {case["case_id"]: case for case in payload["cases"]}


def image_as_data_url(path: Path) -> str:
    """把本地 PNG/JPEG 转为视觉模型可接收的 data URL。"""
    suffix = path.suffix.casefold()
    mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
