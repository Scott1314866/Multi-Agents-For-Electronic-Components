"""工程图产品信息对应的公开 STEP 参考文件检索。

本模块只处理公开网页和直接下载链接。网络结果属于不可信输入：不会执行文件，
不会绕过登录或授权，并在交给 CAD 内核前检查 URL、体积和 STEP 文件头。
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from backend.mcp.web_search_server import agentic_web_search, web_search


_STEP_URL_RE = re.compile(
    r"href\s*=\s*[\"']([^\"']+?\.(?:step|stp)(?:\?[^\"']*)?)[\"']",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9._+-]{3,39}")
_GENERIC_ID_PREFIXES = ("ISO", "JEDEC", "MO-", "REV", "FIG", "TABLE")
_GENERIC_ID_TERMS = (
    "FEMALE",
    "MALE",
    "CIRCUIT",
    "SELECTIVE",
    "PLATING",
    "GOLD",
    "BLACK",
    "BLUE",
    "MAX",
    "MIN",
    "REF",
    "BSC",
)
# 这是几个能够从官网查询的供应商及其网址
_MANUFACTURER_PATTERNS = {
    "Texas Instruments": re.compile(r"\b(?:TEXAS\s+INSTRUMENTS|TI)\b", re.I),
    "Analog Devices": re.compile(r"\bANALOG\s+DEVICES\b", re.I),
    "Microchip": re.compile(r"\bMICROCHIP\b", re.I),
    "STMicroelectronics": re.compile(r"\bSTMICROELECTRONICS\b|\bSTM32", re.I),
    "NXP": re.compile(r"\bNXP\b", re.I),
    "Infineon": re.compile(r"\bINFINEON\b", re.I),
    "onsemi": re.compile(r"\bONSEMI\b|\bON\s+SEMICONDUCTOR\b", re.I),
    "TXGA": re.compile(r"\bTXGA\b", re.I),
    "TE Connectivity": re.compile(r"\bTE\s+CONNECTIVITY\b", re.I),
    "Molex": re.compile(r"\bMOLEX\b", re.I),
    "Amphenol": re.compile(r"\bAMPHENOL\b", re.I),
}
# 这些供应商的网址是公开可访问的，且包含其产品信息
_MANUFACTURER_SEARCH_DOMAINS = {
    "Texas Instruments": "ti.com",
    "Analog Devices": "analog.com",
    "Microchip": "microchip.com",
    "STMicroelectronics": "st.com",
    "NXP": "nxp.com",
    "Infineon": "infineon.com",
    "onsemi": "onsemi.com",
    "TXGA": "m.txga.com",
    "TE Connectivity": "te.com",
    "Molex": "molex.com",
    "Amphenol": "amphenol.com",
}


def _identifier_score(value: str) -> int:
    """计算 OCR 片段作为产品料号的启发式分数。[加权求和]"""
    upper = value.upper()
    compact = re.sub(r"[^A-Z0-9]", "", upper)
    score = 0
    if re.match(r"^[A-Z]{2,}[A-Z0-9._+-]*\d", upper):
        score += 6
    if sum(character.isalpha() for character in compact) >= 2:
        score += 2
    if sum(character.isdigit() for character in compact) >= 2:
        score += 2
    if len(compact) >= 8:
        score += 2
    if any(separator in value for separator in ("-", ".", "+")):
        score += 1
    if any(term in upper for term in _GENERIC_ID_TERMS):
        score -= 8
    if re.fullmatch(r"\d+[A-Z]-[A-Z0-9]+", upper):
        score -= 5
    return score


def extract_product_identifiers(tokens: list[dict[str, Any]]) -> list[str]:
    """从 OCR 标题证据中提取可能的产品料号。

    Args:
        tokens: Agent State 中的完整 OCR token。

    Returns:
        按置信度和页面位置排序的产品标识，最多四个；不生成新文本。
    """
    candidates: list[tuple[int, float, int, str]] = []
    for token in tokens:
        confidence = float(token.get("confidence", 0.0))
        if confidence < 0.80:
            continue
        text = str(token.get("text", "")).strip()
        top = int((token.get("bbox") or [0, 0, 0, 0])[1])
        for part in re.split(r"[/,;\s]+", text):
            value = part.strip("()[]{}:，；")
            upper = value.upper()
            if (
                _IDENTIFIER_RE.fullmatch(value)
                and not upper.startswith(_GENERIC_ID_PREFIXES)
                and not re.fullmatch(r"\d+(?:[.,]\d+)?(?:MM)?", upper)
            ):
                score = _identifier_score(value)
                if score >= 4:
                    candidates.append((score, confidence, top, value))
    ordered = sorted(
        candidates,
        key=lambda item: (-item[0], -item[1], item[2], -len(item[3])),
    )
    return list(dict.fromkeys(item[3] for item in ordered))[:4]


def extract_manufacturer_names(tokens: list[dict[str, Any]]) -> list[str]:
    """从 OCR 原文中提取已知厂商名称，不根据料号猜测厂商。

    Args:
        tokens: Agent State 中的完整 OCR token。

    Returns:
        在图纸文字中明确出现的厂商名称，按预定义规范名称去重。
    """
    text = "\n".join(
        str(token.get("text", ""))
        for token in tokens
        if float(token.get("confidence", 0.0)) >= 0.70
    )
    return [
        manufacturer
        for manufacturer, pattern in _MANUFACTURER_PATTERNS.items()
        if pattern.search(text)
    ]


def classify_result_match(
    result: dict[str, Any],
    identifiers: list[str],
    package_type: str,
) -> str:
    """依据搜索结果文本判断相同产品或相似封装候选。"""
    text = " ".join(
        str(result.get(name, "")) for name in ("title", "url", "snippet", "content")
    ).casefold()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    primary_identifier = identifiers[0] if identifiers else ""
    primary_compact = re.sub(r"[^a-z0-9]+", "", primary_identifier.casefold())
    has_placeholder = "xx" in primary_compact
    if has_placeholder:
        placeholder_pattern = "".join(
            "[a-z0-9]" if character == "x" else re.escape(character)
            for character in primary_compact
        )
        exact_match = bool(re.search(placeholder_pattern, compact))
    else:
        exact_match = any(
            re.sub(r"[^a-z0-9]+", "", identifier.casefold()) in compact
            for identifier in identifiers
        )
    if exact_match:
        return "exact"
    package_terms = [
        re.sub(r"[^a-z0-9]+", "", term.casefold())
        for term in re.findall(r"[A-Za-z]+(?:-?\d+)?", package_type)
        if len(term) >= 3
    ]
    return "similar" if package_terms and any(term in compact for term in package_terms) else "none"


def _search_queries(
    identifiers: list[str],
    package_type: str,
    manufacturers: list[str],
) -> list[str]:
    """构造通用检索、官方域名检索和占位料号前缀检索。"""
    primary = identifiers[0] if identifiers else ""
    prefix = re.split(r"X{2,}", primary, flags=re.I)[0].rstrip("-_.+")
    identity_text = " ".join(identifiers[:2]).strip()
    manufacturer = manufacturers[0] if manufacturers else ""
    generic_query = " ".join(item for item in (
        manufacturer,
        identity_text,
        package_type,
        "STEP STP 3D CAD model",
    ) if item)
    queries: list[str] = []
    official_domain = _MANUFACTURER_SEARCH_DOMAINS.get(manufacturer)
    if official_domain and (prefix or primary):
        queries.append(f"site:{official_domain} {prefix or primary}")
    queries.append(generic_query)
    if prefix and prefix.casefold() != primary.casefold():
        queries.append(f'"{prefix}" STEP STP 3D CAD model')
    return list(dict.fromkeys(query for query in queries if query.strip()))[:3]


async def _is_public_http_url(url: str) -> bool:
    """拒绝本机、内网和非 HTTP(S) URL，降低 SSRF 风险。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except OSError:
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            return False
    return True


async def _discover_step_urls(page_url: str) -> list[str]:
    """从公开落地页提取 STEP/STP 直链，最多读取 2 MiB HTML。"""
    if not await _is_public_http_url(page_url):
        return []
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "DrawingToStepAgent/1.0"},
        ) as client:
            response = await client.get(page_url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").casefold()
            if "html" not in content_type or len(response.content) > 2 * 1024 * 1024:
                return []
            markup = response.text
    except Exception:
        return []
    return list(dict.fromkeys(
        urljoin(page_url, html.unescape(match.group(1)))
        for match in _STEP_URL_RE.finditer(markup)
    ))[:6]


async def _download_public_step(url: str, output_path: Path) -> bool:
    """下载公开 STEP 直链并执行大小和文件头检查。"""
    if not await _is_public_http_url(url):
        return False
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        first_bytes = bytearray()
        async with httpx.AsyncClient(
            timeout=25.0,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "DrawingToStepAgent/1.0"},
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with output_path.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > 50 * 1024 * 1024:
                            raise ValueError("公开 STEP 超过 50 MiB 限制")
                        if len(first_bytes) < 256:
                            first_bytes.extend(chunk[: 256 - len(first_bytes)])
                        handle.write(chunk)
        if b"ISO-10303-21" not in bytes(first_bytes).upper():
            output_path.unlink(missing_ok=True)
            return False
        return total > 128
    except Exception:
        output_path.unlink(missing_ok=True)
        return False


async def search_public_step_candidates(
    *,
    identifiers: list[str],
    package_type: str,
    output_dir: Path,
    manufacturers: list[str] | None = None,
    max_results: int = 6,
) -> dict[str, Any]:
    """搜索并下载少量公开 STEP 候选，搜索失败时返回空结果。"""
    manufacturers = manufacturers or []
    queries = _search_queries(identifiers, package_type, manufacturers)
    query = queries[0] if queries else ""
    if not queries:
        return {"status": "not_found", "query": "", "candidates": []}
    results: list[dict[str, Any]] = []
    search_errors: list[str] = []
    agentic_report: dict[str, Any] = {}
    try:
        agentic_report = await agentic_web_search(
            task=(
                "查找与工程图厂商、料号和封装对应的官方或可信第三方 STEP/STP "
                "三维 CAD 文件"
            ),
            context=(
                f"厂商：{' '.join(manufacturers) or '未知'}；"
                f"料号：{' '.join(identifiers) or '未知'}；"
                f"封装：{package_type or '未知'}"
            ),
            max_rounds=2,
            max_queries_per_round=3,
            max_results_per_query=max_results,
            max_pages=6,
            file_extensions=["step", "stp"],
        )
        for evidence in agentic_report.get("evidence", []):
            results.append({
                "title": str(evidence.get("title", "")),
                "url": str(evidence.get("url", "")),
                "snippet": str(evidence.get("snippet", "")),
                "content": str(evidence.get("page_excerpt", "")),
            })
        for file_url in agentic_report.get("file_candidates", []):
            results.append({
                "title": Path(urlparse(str(file_url)).path).name,
                "url": str(file_url),
                "snippet": "Agentic Search 发现的公开 STEP 文件",
                "content": "",
            })
    except Exception as exc:
        search_errors.append(f"agentic_web_search: {exc}")

    # 领域层始终保留首条厂商官网限定查询，以弥补通用规划器不了解订购码
    # 占位符规则的情况；未发现文件时再执行其余确定性查询。
    deterministic_queries = queries[:1] if agentic_report.get("file_candidates") else queries
    for current_query in deterministic_queries:
        try:
            results.extend(
                await web_search(query=current_query, max_results=max_results) or []
            )
        except Exception as exc:
            search_errors.append(f"{current_query}: {exc}")
    if not results and search_errors:
        return {
            "status": "search_unavailable",
            "query": query,
            "queries": queries,
            "candidates": [],
            "error": "; ".join(search_errors),
        }
    unique_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for result in results:
        url = str(result.get("url", ""))
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(result)

    candidates: list[dict[str, Any]] = []
    cache_dir = output_dir / "web_reference"
    for result_index, result in enumerate(unique_results):
        if not isinstance(result, dict):
            continue
        match_type = classify_result_match(result, identifiers, package_type)
        if match_type == "none":
            continue
        page_url = str(result.get("url", ""))
        direct_urls = (
            [page_url]
            if re.search(r"\.(?:step|stp)(?:\?|$)", page_url, re.I)
            else await _discover_step_urls(page_url)
        )
        for link_index, step_url in enumerate(direct_urls[:3]):
            local_path = cache_dir / f"candidate_{result_index:02d}_{link_index:02d}.step"
            if not await _download_public_step(step_url, local_path):
                continue
            candidates.append({
                "match_type": match_type,
                "title": str(result.get("title", "")),
                "source_page": page_url,
                "download_url": step_url,
                "local_path": str(local_path),
            })
            break
        if len(candidates) >= 3:
            break
    candidates.sort(key=lambda item: 0 if item["match_type"] == "exact" else 1)
    return {
        "status": "candidates_found" if candidates else "not_found",
        "query": query,
        "queries": queries,
        "agentic_search": {
            "status": agentic_report.get("status", "unavailable"),
            "answer_summary": agentic_report.get("answer_summary", ""),
            "confidence": agentic_report.get("confidence", 0.0),
            "citations": agentic_report.get("citations", []),
            "file_candidates": agentic_report.get("file_candidates", []),
            "trace": agentic_report.get("trace", []),
        },
        "identifiers": identifiers,
        "manufacturers": manufacturers,
        "package_type": package_type,
        "candidates": candidates,
    }
