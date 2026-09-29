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
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from backend.mcp.web_search_server import agentic_web_search, web_search


_STEP_URL_RE = re.compile(
    r"href\s*=\s*[\"']([^\"']+?\.(?:step|stp)(?:\?[^\"']*)?)[\"']",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9][A-Za-z0-9._+-]{3,39}")
_GENERIC_ID_PREFIXES = (
    "ISO", "JEDEC", "MO-", "REV", "FIG", "TABLE", "PIN", "QFN",
    "VQFN", "UFQFPN",
)
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


def _package_pin_count(package_type: str) -> int | None:
    match = re.search(
        r"(?:QFN|VQFN|UFQFPN)[- ]?(\d{1,3})", package_type, re.I
    )
    return int(match.group(1)) if match else None


def extract_explicit_package_code(
    tokens: list[dict[str, Any]],
    manufacturers: list[str],
    package_type: str,
) -> str | None:
    """提取与已识别封装类型一起出现的显式厂商封装代码。"""
    if not manufacturers:
        return None
    pin_count = _package_pin_count(package_type)
    if pin_count is None:
        return None
    codes = [
        str(token.get("text", "")).strip().upper()
        for token in tokens
        if float(token.get("confidence", 0.0)) >= 0.80
        and re.fullmatch(r"[A-Z]{2,8}", str(token.get("text", "")).strip())
    ]
    return codes[0] if codes else None


def _ti_cad_part_number_candidates(
    identifiers: list[str], package_code: str | None
) -> list[str]:
    """为 TI CAD 目录生成系列号候选，始终保留原始订购码作回退。"""
    candidates: list[str] = []
    code = (package_code or "").upper()
    for identifier in identifiers:
        value = str(identifier).strip()
        upper = value.upper()
        if code:
            stem = upper
            for reel_suffix in ("R", "T"):
                package_suffix = f"{code}{reel_suffix}"
                if stem.endswith(package_suffix):
                    stem = stem[:-len(package_suffix)]
                    break
            else:
                if stem.endswith(code):
                    stem = stem[:-len(code)]
            if stem and stem != upper:
                # WebBench may index a shared device family without the
                # single-letter orderable variant preceding the package code.
                if stem[-1:].isalpha() and len(stem) > 1:
                    candidates.append(stem[:-1])
                candidates.append(stem)
        candidates.append(value)
    return list(dict.fromkeys(candidates))


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
    parsed_url = urlparse(str(result.get("url", "")))
    is_ti_package_library = (
        (parsed_url.hostname or "").casefold() == "webench.ti.com"
        and parsed_url.path.casefold() == "/cad/cad.cgi"
    )
    # TI's CAD endpoint returns package-level geometry,
    # so even a product-number query must go through Feature IR adaptation.
    if exact_match and not is_ti_package_library:
        return "exact"
    if exact_match and is_ti_package_library:
        return "similar"
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
    package_code: str | None = None,
) -> list[str]:
    """构造通用检索、官方域名检索和占位料号前缀检索。"""
    primary = identifiers[0] if identifiers else ""
    manufacturer = manufacturers[0] if manufacturers else ""
    if manufacturer == "Texas Instruments" and package_code:
        ti_part_numbers = _ti_cad_part_number_candidates(identifiers, package_code)
        if ti_part_numbers:
            primary = ti_part_numbers[0]
    prefix = re.split(r"X{2,}", primary, flags=re.I)[0].rstrip("-_.+")
    identity_text = " ".join(identifiers[:2]).strip()
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
    if manufacturer == "Texas Instruments" and primary and package_code:
        queries.append(
            f'site:vendor.ultralibrarian.com/TI/embedded/ "{primary}" {package_code}'
        )
        queries.append(
            f'site:webench.ti.com/cad/cad.cgi "{primary}" {package_code} STEP'
        )
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


async def _download_ti_qfn_package_reference(
    *,
    product_identifiers: list[str],
    package_code: str | None,
    package_type: str,
    output_dir: Path,
) -> dict[str, Any] | None:
    """从 TI 公开 CAD 页面发现并下载匹配封装代码/针数的 STEP。"""
    pin_count = _package_pin_count(package_type)
    if not product_identifiers or not package_code or not pin_count:
        return None
    family_identifiers = _ti_cad_part_number_candidates(
        product_identifiers, package_code
    )
    for family_index, family_identifier in enumerate(family_identifiers):
        source_page = "https://webench.ti.com/cad/cad.cgi?" + urlencode({
            "partno": family_identifier,
        })
        step_urls = await _discover_step_urls(source_page)
        for link_index, download_url in enumerate(step_urls):
            file_stem = Path(urlparse(download_url).path).stem.upper()
            package_match = re.match(
                rf"{re.escape(package_code.upper())}(\d{{2,3}})", file_stem
            )
            if not package_match or int(package_match.group(1)) != pin_count:
                continue
            local_path = (
                output_dir / "web_reference"
                / f"candidate_ti_{package_code.upper()}_{pin_count}_{family_index}_{link_index}.step"
            )
            if not await _download_public_step(download_url, local_path):
                continue
            return {
                "match_type": "similar",
                "candidate_type": "official_package_reference",
                "title": f"Texas Instruments {package_code.upper()} {pin_count}-pin package model",
                "source_page": source_page,
                "download_url": download_url,
                "local_path": str(local_path),
            }
    return None


async def search_public_step_candidates(
    *,
    identifiers: list[str],
    package_type: str,
    output_dir: Path,
    manufacturers: list[str] | None = None,
    package_code: str | None = None,
    max_results: int = 6,
) -> dict[str, Any]:
    """搜索并下载少量公开 STEP 候选，搜索失败时返回空结果。"""
    manufacturers = manufacturers or []
    queries = _search_queries(identifiers, package_type, manufacturers, package_code)
    query = queries[0] if queries else ""
    primary_identifier = identifiers[0] if identifiers else ""
    ti_catalog_url = None
    if (
        "Texas Instruments" in manufacturers
        and package_code
        and _package_pin_count(package_type)
        and identifiers
    ):
        ti_part_numbers = _ti_cad_part_number_candidates(identifiers, package_code)
        ti_catalog_url = "https://vendor.ultralibrarian.com/TI/embedded/?" + urlencode({
            "gpn": ti_part_numbers[0] if ti_part_numbers else primary_identifier,
            "package": package_code,
            "pin": str(_package_pin_count(package_type)),
        })
    if not queries:
        return {"status": "not_found", "query": "", "candidates": []}
    # 查询 TI 公开 CAD 页面，从页面当前公开的 STEP 链接中选择匹配封装，
    # 避免将某个封装代码或文件命名规则固定在下载逻辑里。
    if (
        "Texas Instruments" in manufacturers
        and package_code
        and _package_pin_count(package_type)
        and identifiers
    ):
        ti_package_candidate = await _download_ti_qfn_package_reference(
            product_identifiers=identifiers,
            package_code=package_code,
            package_type=package_type,
            output_dir=output_dir,
        )
        if ti_package_candidate:
            return {
                "status": "candidates_found",
                "query": queries[-1],
                "queries": queries,
                "search_strategy": "official_package_catalog_fast_path",
                "agentic_search": {
                    "status": "skipped_official_package_reference_found",
                    "trace": [],
                },
                "identifiers": identifiers,
                "manufacturers": manufacturers,
                "package_type": package_type,
                "catalog_sources": ([{
                    "provider": "Ultra Librarian for TI",
                    "url": ti_catalog_url,
                    "status": "manual_product_export_available",
                }] if ti_catalog_url else []),
                "candidates": [ti_package_candidate],
            }
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
                f"封装：{package_type or '未知'}；"
                f"厂商封装代码：{package_code or '未知'}"
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
        ti_package_candidate = await _download_ti_qfn_package_reference(
            product_identifiers=identifiers,
            package_code=package_code,
            package_type=package_type,
            output_dir=output_dir,
        )
        if ti_package_candidate:
            return {
                "status": "candidates_found",
                "query": query,
                "queries": queries,
                "agentic_search": {"status": "unavailable", "trace": []},
                "identifiers": identifiers,
                "manufacturers": manufacturers,
                "package_type": package_type,
                "catalog_sources": ([{
                    "provider": "Ultra Librarian for TI",
                    "url": ti_catalog_url,
                    "status": "manual_export_required",
                }] if ti_catalog_url else []),
                "candidates": [ti_package_candidate],
            }
        return {
            "status": "search_unavailable",
            "query": query,
            "queries": queries,
            "candidates": [],
            "catalog_sources": ([{
                "provider": "Ultra Librarian for TI",
                "url": ti_catalog_url,
                "status": "manual_export_required",
            }] if ti_catalog_url else []),
            "error": "; ".join(search_errors),
        }
    unique_results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url", "")).strip()
        parsed_url = urlparse(url)
        normalized_url = parsed_url._replace(
            scheme=parsed_url.scheme.casefold(),
            netloc=parsed_url.netloc.casefold(),
            fragment="",
        ).geturl().rstrip("/")
        if normalized_url and normalized_url not in seen_urls:
            seen_urls.add(normalized_url)
            unique_results.append(result)

    # 精确料号结果优先处理，避免较早出现的封装相似结果挤掉精确命中。
    # 同时限制并发数，缩短网页读取/下载等待时间而不对外站造成突发请求。
    ranked_results = [
        (classify_result_match(result, identifiers, package_type), index, result)
        for index, result in enumerate(unique_results)
    ]
    ranked_results = [item for item in ranked_results if item[0] != "none"]
    ranked_results.sort(key=lambda item: (item[0] != "exact", item[1]))
    ranked_results = ranked_results[: max(6, min(max_results * 2, 12))]
    exact_results = [item for item in ranked_results if item[0] == "exact"]
    similar_results = [item for item in ranked_results if item[0] == "similar"]

    cache_dir = output_dir / "web_reference"
    semaphore = asyncio.Semaphore(3)

    async def resolve_result(
        match_type: str,
        result_index: int,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with semaphore:
            page_url = str(result.get("url", "")).strip()
            direct_urls = (
                [page_url]
                if re.search(r"\.(?:step|stp)(?:\?|$)", page_url, re.I)
                else await _discover_step_urls(page_url)
            )
            for link_index, step_url in enumerate(dict.fromkeys(direct_urls[:3])):
                local_path = cache_dir / f"candidate_{result_index:02d}_{link_index:02d}.step"
                if await _download_public_step(step_url, local_path):
                    return {
                        "match_type": match_type,
                        "title": str(result.get("title", "")),
                        "source_page": page_url,
                        "download_url": step_url,
                        "local_path": str(local_path),
                    }
        return None

    resolved_exact = await asyncio.gather(*(
        resolve_result(match_type, index, result)
        for match_type, index, result in exact_results
    ))
    candidates = [candidate for candidate in resolved_exact if candidate is not None]
    if not candidates:
        ti_package_candidate = await _download_ti_qfn_package_reference(
            product_identifiers=identifiers,
            package_code=package_code,
            package_type=package_type,
            output_dir=output_dir,
        )
        if ti_package_candidate:
            candidates = [ti_package_candidate]
        elif similar_results:
            resolved_similar = await asyncio.gather(*(
                resolve_result(match_type, index, result)
                for match_type, index, result in similar_results
            ))
            candidates = [candidate for candidate in resolved_similar if candidate is not None]
    candidates.sort(key=lambda candidate: candidate["match_type"] != "exact")
    candidates = candidates[:3]
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
        "catalog_sources": ([{
            "provider": "Ultra Librarian for TI",
            "url": ti_catalog_url,
            "status": "manual_export_required",
        }] if ti_catalog_url else []),
        "candidates": candidates,
    }
