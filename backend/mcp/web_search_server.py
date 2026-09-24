"""面向 Agent 的 MCP Web Search Server。

保留单次 ``web_search`` 兼容接口，并提供完整 Agentic Search：LLM 规划查询、
搜索工具调用、公开网页读取、文件线索发现、确定性代码评分、LLM 证据评估与
多轮停止判断。模块不开放任意代码执行，也不下载或执行二进制文件。
"""

from __future__ import annotations

import asyncio
import html
from html.parser import HTMLParser
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field

from backend.core.logger import get_logger


logger = get_logger(__name__)
mcp = MCPServer(name="Agent-WebSearch")

_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_PAGE_TEXT = 12_000
_DEFAULT_FILE_EXTENSIONS = (
    "step", "stp", "iges", "igs", "pdf", "zip", "dxf", "dwg",
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{1,}")


class SearchPlan(BaseModel):
    """LLM 输出的第一轮搜索计划。"""

    model_config = ConfigDict(extra="forbid")
    queries: list[str] = Field(min_length=1, max_length=6)
    must_include_terms: list[str] = Field(default_factory=list, max_length=12)
    target_domains: list[str] = Field(default_factory=list, max_length=6)
    file_extensions: list[str] = Field(default_factory=list, max_length=12)
    reasoning_summary: str = ""


class SearchAssessment(BaseModel):
    """LLM 对当前证据的阶段性判断。"""

    model_config = ConfigDict(extra="forbid")
    answer_summary: str = ""
    useful_urls: list[str] = Field(default_factory=list, max_length=12)
    next_queries: list[str] = Field(default_factory=list, max_length=6)
    missing_information: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    done: bool = False


class _HTMLEvidenceParser(HTMLParser):
    """提取页面标题、正文和链接的轻量 HTML 解析器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._inside_title = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.casefold()
        if lower in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if lower == "title":
            self._inside_title = True
        if lower == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(html.unescape(href))

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if lower == "title":
            self._inside_title = False
        if lower in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        self.text_parts.append(normalized)
        if self._inside_title:
            self.title_parts.append(normalized)


async def _search_tavily(
    query: str,
    max_results: int,
    api_key: str,
) -> list[dict[str, Any]]:
    """使用 Tavily 返回结构化搜索结果。"""
    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "include_answer": False,
                "include_raw_content": False,
            },
        )
        response.raise_for_status()
    return [
        {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "snippet": item.get("content", "")[:500],
            "content": item.get("content", ""),
        }
        for item in response.json().get("results", [])
    ]


async def _search_duckduckgo(query: str, max_results: int) -> list[dict[str, Any]]:
    """使用 DuckDuckGo 搜索，并在线程中包装同步客户端。"""
    from duckduckgo_search import DDGS

    def _sync_search() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        with DDGS() as client:
            for item in client.text(query, max_results=max_results):
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("href", ""),
                    "snippet": item.get("body", "")[:500],
                    "content": item.get("body", ""),
                })
        return results

    return await asyncio.to_thread(_sync_search)


async def _is_public_http_url(url: str) -> bool:
    """仅允许解析到公网地址的 HTTP(S) URL，拒绝本机和内网。"""
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
    return all(ipaddress.ip_address(item[4][0]).is_global for item in addresses)


def _normalize_extensions(extensions: list[str] | None) -> tuple[str, ...]:
    """规范化允许搜索的公开文件扩展名。"""
    values = extensions or list(_DEFAULT_FILE_EXTENSIONS)
    cleaned = [re.sub(r"[^a-z0-9]", "", value.casefold()) for value in values]
    return tuple(dict.fromkeys(value for value in cleaned if value))[:16]


def _is_file_url(url: str, extensions: tuple[str, ...]) -> bool:
    """根据 URL 路径判断是否为目标文件。"""
    path = urlparse(url).path.casefold()
    return any(path.endswith(f".{extension}") for extension in extensions)


async def _fetch_public_page(
    url: str,
    *,
    file_extensions: tuple[str, ...],
) -> dict[str, Any]:
    """读取公开 HTML 页面并提取正文和目标文件链接。"""
    if not await _is_public_http_url(url):
        return {"url": url, "status": "rejected_non_public_url", "file_urls": []}
    if _is_file_url(url, file_extensions):
        return {"url": url, "status": "direct_file", "file_urls": [url]}
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            trust_env=False,
            headers={"User-Agent": "AgenticWebSearch/1.0"},
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").casefold()
                if "html" not in content_type:
                    return {
                        "url": str(response.url),
                        "status": "unsupported_content_type",
                        "content_type": content_type,
                        "file_urls": [],
                    }
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_PAGE_BYTES:
                        raise ValueError("网页超过 2 MiB 读取上限")
                final_url = str(response.url)
        parser = _HTMLEvidenceParser()
        parser.feed(bytes(body).decode("utf-8", errors="replace"))
        links = list(dict.fromkeys(urljoin(final_url, link) for link in parser.links))
        files = [link for link in links if _is_file_url(link, file_extensions)]
        return {
            "url": final_url,
            "status": "fetched",
            "title": " ".join(parser.title_parts)[:300],
            "text": " ".join(parser.text_parts)[:_MAX_PAGE_TEXT],
            "file_urls": files[:24],
        }
    except Exception as exc:
        return {"url": url, "status": "fetch_failed", "error": str(exc), "file_urls": []}


def _deduplicate_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按规范化 URL 去重搜索结果，并合并来源查询。"""
    unique: list[dict[str, Any]] = []
    by_url: dict[str, dict[str, Any]] = {}
    for result in results:
        url = str(result.get("url", "")).strip()
        normalized = url.rstrip("/").casefold()
        if not url:
            continue
        if normalized in by_url:
            existing = by_url[normalized]
            existing["source_queries"] = list(dict.fromkeys([
                *existing.get("source_queries", []),
                *result.get("source_queries", []),
            ]))
            continue
        item = dict(result)
        by_url[normalized] = item
        unique.append(item)
    return unique


def _task_terms(task: str) -> set[str]:
    """从任务中提取供确定性相关度评分使用的词项。"""
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(task)
        if len(token) >= 3
    }


def _rank_evidence(
    task: str,
    results: list[dict[str, Any]],
    pages: dict[str, dict[str, Any]],
    target_domains: list[str],
) -> list[dict[str, Any]]:
    """通过可复现代码对搜索结果、页面正文和文件证据进行评分。"""
    terms = _task_terms(task)
    ranked: list[dict[str, Any]] = []
    for result in results:
        url = str(result.get("url", ""))
        page = pages.get(url, {})
        text = " ".join((
            str(result.get("title", "")),
            str(result.get("snippet", "")),
            str(page.get("text", ""))[:4000],
        )).casefold()
        matched = sorted(term for term in terms if term in text)
        hostname = (urlparse(url).hostname or "").casefold()
        domain_match = any(
            hostname == domain.casefold() or hostname.endswith(f".{domain.casefold()}")
            for domain in target_domains
        )
        file_urls = list(page.get("file_urls", []))
        score = min(len(matched), 8) * 1.5
        score += 4.0 if domain_match else 0.0
        score += 5.0 if file_urls else 0.0
        score += 2.0 if str(page.get("status")) == "fetched" else 0.0
        ranked.append({
            "title": str(result.get("title", "")),
            "url": url,
            "snippet": str(result.get("snippet", ""))[:500],
            "source_queries": list(result.get("source_queries", [])),
            "matched_terms": matched,
            "domain_match": domain_match,
            "file_urls": file_urls,
            "page_status": page.get("status", "not_fetched"),
            "page_excerpt": str(page.get("text", ""))[:1200],
            "score": round(score, 3),
        })
    return sorted(ranked, key=lambda item: (-item["score"], item["url"]))


async def _plan_search(task: str, context: str, max_queries: int) -> SearchPlan:
    """调用项目统一 LLM Factory 生成首轮搜索计划。"""
    from backend.core.llm_factory import get_structured_llm

    model = get_structured_llm("qa", SearchPlan)
    result = await model.ainvoke([
        SystemMessage(content=(
            "你是检索规划器。根据任务生成少量互补的 Web 查询。优先使用精确料号、"
            "厂商官网 site: 域名、文件扩展名和同义词。不要回答任务，不要编造 URL。"
            "只输出符合 schema 的 JSON；reasoning_summary 只写简短策略摘要。"
        )),
        HumanMessage(content=(
            f"任务：{task}\n已知上下文：{context or '无'}\n"
            f"最多生成 {max_queries} 条查询。"
        )),
    ])
    plan = result if isinstance(result, SearchPlan) else SearchPlan.model_validate(result)
    plan.queries = plan.queries[:max_queries]
    return plan


async def _assess_search(
    task: str,
    evidence: list[dict[str, Any]],
    round_index: int,
) -> SearchAssessment:
    """让 LLM 基于受限证据决定是否结束或生成下一轮查询。"""
    from backend.core.llm_factory import get_structured_llm

    compact = [{
        "title": item["title"],
        "url": item["url"],
        "snippet": item["snippet"],
        "file_urls": item["file_urls"],
        "matched_terms": item["matched_terms"],
        "score": item["score"],
    } for item in evidence[:12]]
    model = get_structured_llm("qa", SearchAssessment)
    result = await model.ainvoke([
        SystemMessage(content=(
            "你是搜索证据评估器。只能依据提供的 evidence 判断，不得新增 URL 或事实。"
            "证据不足时给出少量下一轮查询；useful_urls 必须逐字来自 evidence。"
            "只输出符合 schema 的 JSON，不输出思维过程。"
        )),
        HumanMessage(content=(
            f"任务：{task}\n轮次：{round_index}\n"
            f"evidence JSON：{compact}"
        )),
    ])
    assessment = (
        result
        if isinstance(result, SearchAssessment)
        else SearchAssessment.model_validate(result)
    )
    allowed_urls = {item["url"] for item in evidence}
    assessment.useful_urls = [url for url in assessment.useful_urls if url in allowed_urls]
    return assessment


@mcp.tool()
async def web_search(query: str, max_results: int = 5) -> list[dict[str, Any]]:
    """执行一次 Web 搜索；Tavily 失败时自动降级 DuckDuckGo。

    Args:
        query: 单条搜索语句。
        max_results: 返回结果上限，范围 1～10。

    Returns:
        标题、URL、摘要和搜索内容组成的结果列表。
    """
    from backend.config import get_settings

    settings = get_settings()
    limit = max(1, min(int(max_results), 10))
    if settings.tavily_api_key:
        try:
            results = await _search_tavily(query, limit, settings.tavily_api_key)
            logger.info("web_search_mcp.tavily_done", query=query, hits=len(results))
            return results
        except Exception as exc:
            logger.warning("web_search_mcp.tavily_failed", query=query, error=str(exc))
    try:
        results = await _search_duckduckgo(query, limit)
        logger.info("web_search_mcp.ddgs_done", query=query, hits=len(results))
        return results
    except Exception as exc:
        logger.error("web_search_mcp.ddgs_failed", query=query, error=str(exc))
        return []


@mcp.tool()
async def fetch_public_page(
    url: str,
    file_extensions: list[str] | None = None,
) -> dict[str, Any]:
    """读取一个公开网页，返回正文摘录及 STEP/PDF 等文件链接。

    Args:
        url: 待读取的公网 HTTP(S) 页面。
        file_extensions: 需要提取的文件扩展名；留空使用工程文件默认集合。

    Returns:
        页面状态、标题、正文摘录和文件 URL。内网地址会被拒绝。
    """
    return await _fetch_public_page(
        url,
        file_extensions=_normalize_extensions(file_extensions),
    )


@mcp.tool()
async def agentic_web_search(
    task: str,
    context: str = "",
    max_rounds: int = 2,
    max_queries_per_round: int = 3,
    max_results_per_query: int = 5,
    max_pages: int = 4,
    file_extensions: list[str] | None = None,
) -> dict[str, Any]:
    """执行 LLM 驱动、可审计、有边界的多轮 Agentic Web Search。

    Args:
        task: 需要通过互联网解决的明确检索任务。
        context: 已知厂商、料号、封装或其他检索上下文。
        max_rounds: 最大搜索轮数，范围 1～3。
        max_queries_per_round: 每轮查询上限，范围 1～4。
        max_results_per_query: 每条查询结果上限，范围 1～8。
        max_pages: 全流程最多读取的网页数，范围 0～8。
        file_extensions: 需要发现的文件扩展名。

    Returns:
        搜索摘要、引用、文件候选、排序证据和每轮审计轨迹。

    失败状态:
        LLM 不可用时降级为以 ``task`` 为查询的单轮搜索；搜索后端不可用时
        返回 ``status=insufficient_evidence``，不会伪造答案或文件链接。
    """
    rounds = max(1, min(int(max_rounds), 3))
    query_limit = max(1, min(int(max_queries_per_round), 4))
    result_limit = max(1, min(int(max_results_per_query), 8))
    page_limit = max(0, min(int(max_pages), 8))
    extensions = _normalize_extensions(file_extensions)
    trace: list[dict[str, Any]] = []
    all_results: list[dict[str, Any]] = []
    fetched_pages: dict[str, dict[str, Any]] = {}
    target_domains: list[str] = []
    scoring_task = task
    assessment = SearchAssessment()
    per_round_page_budget = (
        max(1, (page_limit + rounds - 1) // rounds) if page_limit else 0
    )

    try:
        plan = await _plan_search(task, context, query_limit)
        queries = plan.queries
        target_domains = plan.target_domains
        scoring_task = " ".join([task, *plan.must_include_terms])
        if plan.file_extensions:
            extensions = _normalize_extensions(plan.file_extensions)
        trace.append({
            "stage": "plan",
            "queries": queries,
            "target_domains": target_domains,
            "file_extensions": list(extensions),
            "reasoning_summary": plan.reasoning_summary,
            "fallback": False,
        })
    except Exception as exc:
        queries = [" ".join(item for item in (task, context) if item).strip()]
        trace.append({
            "stage": "plan",
            "queries": queries,
            "fallback": True,
            "error": str(exc),
        })

    for round_index in range(1, rounds + 1):
        queries = list(dict.fromkeys(query for query in queries if query.strip()))[:query_limit]
        batches = await asyncio.gather(*(
            web_search(query=query, max_results=result_limit) for query in queries
        ))
        for query, batch in zip(queries, batches):
            for result in batch:
                enriched = dict(result)
                enriched["source_queries"] = [
                    *enriched.get("source_queries", []), query
                ]
                all_results.append(enriched)
        all_results = _deduplicate_results(all_results)

        remaining_pages = min(
            max(0, page_limit - len(fetched_pages)),
            per_round_page_budget,
        )
        preliminary_evidence = _rank_evidence(
            scoring_task,
            all_results,
            fetched_pages,
            target_domains,
        )
        page_urls = [
            item["url"]
            for item in preliminary_evidence
            if item["url"] not in fetched_pages
        ][:remaining_pages]
        page_results = await asyncio.gather(*(
            _fetch_public_page(url, file_extensions=extensions) for url in page_urls
        ))
        fetched_pages.update(zip(page_urls, page_results))
        evidence = _rank_evidence(
            scoring_task,
            all_results,
            fetched_pages,
            target_domains,
        )

        try:
            assessment = await _assess_search(task, evidence, round_index)
            assessment_fallback = False
        except Exception as exc:
            file_found = any(item["file_urls"] for item in evidence)
            assessment = SearchAssessment(
                answer_summary="搜索完成，需由调用方依据结构化证据继续处理。",
                useful_urls=[item["url"] for item in evidence[:5]],
                missing_information=[] if evidence else ["未检索到可用公开证据"],
                confidence=0.7 if file_found else (0.45 if evidence else 0.0),
                done=bool(evidence),
            )
            assessment_fallback = True
            trace.append({
                "stage": "assessment_error",
                "round": round_index,
                "error": str(exc),
            })
        trace.append({
            "stage": "round",
            "round": round_index,
            "queries": queries,
            "result_count": len(all_results),
            "page_count": len(fetched_pages),
            "file_count": sum(len(item["file_urls"]) for item in evidence),
            "confidence": assessment.confidence,
            "done": assessment.done,
            "missing_information": assessment.missing_information,
            "assessment_fallback": assessment_fallback,
        })
        if assessment.done or round_index >= rounds:
            break
        queries = assessment.next_queries or [f"{task} official source"]

    evidence = _rank_evidence(
        scoring_task,
        all_results,
        fetched_pages,
        target_domains,
    )
    file_candidates = list(dict.fromkeys(
        file_url
        for item in evidence
        for file_url in item["file_urls"]
    ))
    allowed_urls = {item["url"] for item in evidence}
    citations = [url for url in assessment.useful_urls if url in allowed_urls]
    if not citations:
        citations = [item["url"] for item in evidence[:5]]
    status = (
        "completed"
        if evidence and assessment.done
        else ("partial" if evidence else "insufficient_evidence")
    )
    logger.info(
        "web_search_mcp.agentic_done",
        status=status,
        rounds=len([item for item in trace if item.get("stage") == "round"]),
        evidence_count=len(evidence),
        file_count=len(file_candidates),
    )
    return {
        "status": status,
        "task": task,
        "answer_summary": assessment.answer_summary,
        "confidence": assessment.confidence,
        "citations": citations,
        "file_candidates": file_candidates,
        "evidence": evidence,
        "missing_information": assessment.missing_information,
        "trace": trace,
        "safety": {
            "public_http_only": True,
            "arbitrary_code_execution": False,
            "binary_execution": False,
            "max_page_bytes": _MAX_PAGE_BYTES,
        },
        "code_execution": {
            "mode": "deterministic_sandboxed_helpers",
            "operations": [
                "url_deduplication",
                "term_and_domain_scoring",
                "html_link_extraction",
                "citation_allowlist_filtering",
            ],
        },
    }


if __name__ == "__main__":
    import uvicorn

    port = 8002
    print(f"Agentic Web Search MCP Server → http://localhost:{port}/mcp")
    uvicorn.run(
        mcp.streamable_http_app(stateless_http=True, json_response=True),
        host="0.0.0.0",
        port=port,
    )
