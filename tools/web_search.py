"""
Web Search Tool
---------------
Uses DuckDuckGo via duckduckgo-search (no API key).
Falls back to a simple requests scrape if unavailable.
"""

import asyncio
import json
from typing import Optional


async def web_search(query: str, max_results: int = 5) -> dict:
    """
    Async web search. Returns dict with results list and status.
    """
    try:
        results = await asyncio.get_event_loop().run_in_executor(
            None, _ddg_search, query, max_results
        )
        return {"ok": True, "query": query, "results": results}
    except Exception as e:
        return {"ok": False, "query": query, "error": str(e), "results": []}


def _ddg_search(query: str, max_results: int) -> list[dict]:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            return [
                {
                    "title": r.get("title", ""),
                    "url":   r.get("href", ""),
                    "body":  r.get("body", "")[:400],
                }
                for r in results
            ]
    except ImportError:
        return _fallback_search(query, max_results)


def _fallback_search(query: str, max_results: int) -> list[dict]:
    """Minimal fallback using requests to DuckDuckGo lite."""
    try:
        import requests
        from urllib.parse import quote
        url = f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AgentCollective/1.0)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        # Very basic text extraction
        text = resp.text
        lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 40]
        return [{"title": "Result", "url": "", "body": l} for l in lines[:max_results]]
    except Exception as e:
        return [{"title": "Search unavailable", "url": "", "body": str(e)}]


def format_results(result: dict) -> str:
    """Format search results as readable text for agent consumption."""
    if not result["ok"]:
        return f"Search failed: {result.get('error', 'unknown error')}"
    if not result["results"]:
        return "No results found."
    lines = [f"Search results for: {result['query']}\n"]
    for i, r in enumerate(result["results"], 1):
        lines.append(f"{i}. {r['title']}")
        if r["url"]:
            lines.append(f"   {r['url']}")
        lines.append(f"   {r['body']}")
        lines.append("")
    return "\n".join(lines)
