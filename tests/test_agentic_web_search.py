"""Agentic Web Search MCP 的确定性单元测试。"""

from __future__ import annotations

import asyncio

from backend.mcp import web_search_server as search_server
from backend.agents.step import reference_search


def test_html_parser_extracts_engineering_file_links():
    """页面解析器应保留正文，并识别相对工程文件链接。"""
    parser = search_server._HTMLEvidenceParser()
    parser.feed(
        "<html><head><title>Part A</title></head><body>"
        "<p>Official model</p><a href='/cad/part-a.step'>STEP</a>"
        "<script>ignored text</script></body></html>"
    )
    links = [
        search_server.urljoin("https://vendor.example/product/a", link)
        for link in parser.links
    ]

    assert "Official model" in parser.text_parts
    assert "ignored text" not in parser.text_parts
    assert links == ["https://vendor.example/cad/part-a.step"]
    assert search_server._is_file_url(
        links[0], search_server._normalize_extensions(["STEP"])
    )


def test_result_deduplication_merges_query_audit_trail():
    """相同 URL 只能保留一份，但必须合并来源查询。"""
    results = search_server._deduplicate_results([
        {"url": "https://example.com/a", "source_queries": ["query-a"]},
        {"url": "https://example.com/a/", "source_queries": ["query-b"]},
    ])

    assert len(results) == 1
    assert results[0]["source_queries"] == ["query-a", "query-b"]


def test_code_ranking_prefers_official_page_with_file():
    """受控代码评分应优先厂商官网且包含目标文件的页面。"""
    results = [
        {
            "title": "Part A official model",
            "url": "https://vendor.example/products/a",
            "snippet": "Part A STEP",
            "source_queries": ["Part A STEP"],
        },
        {
            "title": "Unrelated page",
            "url": "https://other.example/post",
            "snippet": "Part A discussion",
            "source_queries": ["Part A STEP"],
        },
    ]
    pages = {
        "https://vendor.example/products/a": {
            "status": "fetched",
            "text": "Part A official CAD",
            "file_urls": ["https://vendor.example/a.step"],
        }
    }

    ranked = search_server._rank_evidence(
        "find Part A STEP",
        results,
        pages,
        ["vendor.example"],
    )

    assert ranked[0]["url"] == "https://vendor.example/products/a"
    assert ranked[0]["file_urls"] == ["https://vendor.example/a.step"]


def test_agentic_search_orchestrates_llm_tools_files_and_code(monkeypatch):
    """Agentic 工具应串联规划、搜索、页面、文件、评分和证据评估。"""
    async def fake_plan(_task: str, _context: str, _limit: int):
        return search_server.SearchPlan(
            queries=["site:vendor.example Part A STEP"],
            target_domains=["vendor.example"],
            file_extensions=["step"],
            reasoning_summary="优先检索厂商官网和 STEP。",
        )

    async def fake_search(query: str, max_results: int = 5):
        assert query == "site:vendor.example Part A STEP"
        assert max_results == 3
        return [{
            "title": "Part A official",
            "url": "https://vendor.example/products/a",
            "snippet": "Part A 3D CAD",
            "content": "",
        }]

    async def fake_fetch(url: str, *, file_extensions: tuple[str, ...]):
        assert file_extensions == ("step",)
        return {
            "url": url,
            "status": "fetched",
            "title": "Part A official",
            "text": "Part A official 3D CAD model",
            "file_urls": ["https://vendor.example/files/part-a.step"],
        }

    async def fake_assess(_task: str, evidence: list[dict], _round: int):
        return search_server.SearchAssessment(
            answer_summary="已找到厂商 STEP。",
            useful_urls=[evidence[0]["url"], "https://fabricated.example"],
            confidence=0.98,
            done=True,
        )

    monkeypatch.setattr(search_server, "_plan_search", fake_plan)
    monkeypatch.setattr(search_server, "web_search", fake_search)
    monkeypatch.setattr(search_server, "_fetch_public_page", fake_fetch)
    monkeypatch.setattr(search_server, "_assess_search", fake_assess)

    result = asyncio.run(search_server.agentic_web_search(
        task="find Part A STEP",
        max_rounds=2,
        max_queries_per_round=2,
        max_results_per_query=3,
        max_pages=2,
    ))

    assert result["status"] == "completed"
    assert result["file_candidates"] == [
        "https://vendor.example/files/part-a.step"
    ]
    assert result["citations"] == ["https://vendor.example/products/a"]
    assert result["trace"][0]["stage"] == "plan"
    assert result["trace"][1]["stage"] == "round"
    assert result["safety"]["arbitrary_code_execution"] is False
    assert result["code_execution"]["mode"] == "deterministic_sandboxed_helpers"


def test_public_url_gate_rejects_non_http_scheme():
    """页面工具不得读取 file、data 等非 HTTP URL。"""
    assert asyncio.run(search_server._is_public_http_url("file:///etc/passwd")) is False


def test_step_reference_resolver_uses_agentic_search_before_domain_validation(
    monkeypatch,
    tmp_path,
):
    """STEP 领域解析器应消费 Agentic 结果，再执行既有下载验证。"""
    calls: list[str] = []

    async def fake_agentic_search(**_kwargs):
        calls.append("agentic")
        return {
            "status": "completed",
            "answer_summary": "已找到官方 STEP。",
            "confidence": 0.99,
            "citations": ["https://vendor.example/products/part-a"],
            "file_candidates": ["https://vendor.example/files/part-a.step"],
            "evidence": [],
            "trace": [{"stage": "plan"}],
        }

    async def fake_web_search(*, query: str, max_results: int):
        calls.append("domain_search")
        assert query
        assert max_results == 3
        return []

    async def fake_download(url, output_path):
        calls.append("download_validate")
        assert url == "https://vendor.example/files/part-a.step"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("ISO-10303-21;", encoding="ascii")
        return True

    monkeypatch.setattr(reference_search, "agentic_web_search", fake_agentic_search)
    monkeypatch.setattr(reference_search, "web_search", fake_web_search)
    monkeypatch.setattr(reference_search, "_download_public_step", fake_download)

    report = asyncio.run(reference_search.search_public_step_candidates(
        identifiers=["PART-A"],
        package_type="PACKAGE-A",
        manufacturers=[],
        output_dir=tmp_path,
        max_results=3,
    ))

    assert calls == ["agentic", "domain_search", "download_validate"]
    assert report["status"] == "candidates_found"
    assert report["agentic_search"]["status"] == "completed"
    assert report["candidates"][0]["match_type"] == "exact"
