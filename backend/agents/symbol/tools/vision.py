"""多模态客户端（对应源程序的 ``app/vision.py``）。

源文件是密文。接口由唯一调用点反推 —— ``review.py:83``::

    payload = client.ask_json([image_path], _REVIEW_PROMPT.format(...))

即：**第一个参数是图片路径的列表**（可多张：整页图 + 该页文本层一并输入），
第二个是提示词，返回值是**已解析好的 dict**，且调用方直接 ``payload.get(...)``，
所以**绝不返回 None**。

两个行为契约来自文档（``Module1-Spec`` §8.2 / §11.4、``USAGE`` §常见问题）：

* **JSON 模式** —— 请求体要求模型输出 JSON（Qwen 侧即 ``response_format``）。
* **空内容重试** —— 模型偶发返回空（推理 token 耗尽）时由 ``ask_json`` 内部
  重试兜住；``review.py`` 侧不做重试，所以重试必须发生在这里。重试耗尽后抛异常。

**视觉通道只负责图形结构**（哪个脚在哪条边上），名称/类型/描述一律交给文本
通道 —— 让模型读密集小字表格是幻觉高发区。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import time
from typing import Any, Sequence

from langchain_core.messages import HumanMessage

from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger

logger = get_logger(__name__)

#: 在 llm_factory 里注册为 qwen-version（多模态）。
DEFAULT_AGENT_TYPE = "symbol_vision"
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 0.6

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


def parse_model_json(text: str) -> dict[str, Any] | None:
    """从模型回复里抠出 JSON 对象。

    容忍三种常见包装：裸 JSON、```json 代码块、以及前后带解释文字。
    """
    if not text:
        return None
    candidate = text.strip()
    if "```" in candidate:
        match = _JSON_BLOCK_RE.search(candidate)
        if match:
            candidate = match.group(1).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _data_url(path: Path) -> str:
    mime = _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


class VisionClient:
    """看图提问并拿回 JSON。"""

    def __init__(
        self,
        agent_type: str = DEFAULT_AGENT_TYPE,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        temperature: float = 0.0,
    ) -> None:
        self._llm = get_llm(agent_type, temperature=temperature)
        self._max_attempts = max(1, max_attempts)

    def ask_json(
        self, images: Sequence[str | Path], prompt: str
    ) -> dict[str, Any]:
        """把若干张图与一段提示词交给多模态模型，返回 JSON。

        Args:
            images: 图片路径列表。一次调用 = 一次模型调用（用量按张数计）。
            prompt: 提示词；``user`` 侧要求输出 JSON。

        Returns:
            解析好的 dict。**永不为 None** —— 调用方直接按 dict 用。

        Raises:
            RuntimeError: 连续 ``max_attempts`` 次都没拿到可用 JSON。
        """
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image in images:
            content.append(
                {"type": "image_url", "image_url": {"url": _data_url(Path(image))}}
            )

        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._llm.invoke([HumanMessage(content=content)])
                payload = parse_model_json(_as_text(response.content))
                if payload is not None:
                    return payload
                last_error = ValueError("模型返回空内容或非法 JSON")
                logger.warning(
                    "symbol.vision.empty_response", attempt=attempt, of=self._max_attempts
                )
            except Exception as exc:  # 网络抖动、限流、内容过滤都走这里
                last_error = exc
                logger.warning(
                    "symbol.vision.call_failed", attempt=attempt, error=str(exc)
                )
            if attempt < self._max_attempts:
                time.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))

        raise RuntimeError(
            f"视觉模型连续 {self._max_attempts} 次未返回可用 JSON：{last_error}"
        )


def _as_text(content: Any) -> str:
    """LangChain 的回复可能是 str，也可能是 content block 列表。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content or "")
