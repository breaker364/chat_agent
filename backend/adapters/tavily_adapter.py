"""
Tavily search and extract adapter.

Calls a Tavily-compatible API endpoint for higher-quality search results
and clean Markdown extraction. Falls back gracefully on any error.
"""

from __future__ import annotations

import time

import requests

from ..config import load_tavily_config
from .base import FetchResult, RedirectInfo, SearchOptions, SearchResult

_TAVILY_TIMEOUT_SECONDS = 20


def _get_tavily_config() -> tuple[str, str | None]:
    """Return (base_url, api_key) from environment or config.json."""
    config = load_tavily_config()
    return str(config["base_url"]), config["api_key"]


def _tavily_headers() -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    _, api_key = _get_tavily_config()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


class TavilySearchAdapter:
    """Search adapter backed by Tavily API (/search endpoint)."""

    @property
    def name(self) -> str:
        return "tavily"

    def _is_available(self) -> bool:
        base_url, _ = _get_tavily_config()
        try:
            resp = requests.get(f"{base_url}/health", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def search(self, query: str, options: SearchOptions | None = None) -> list[SearchResult]:
        opts = options or SearchOptions()
        base_url, _ = _get_tavily_config()

        body: dict[str, object] = {
            "query": query,
            "max_results": max(opts.num_results, 3),
        }
        if opts.allowed_domains:
            body["include_domains"] = opts.allowed_domains
        if opts.blocked_domains:
            body["exclude_domains"] = opts.blocked_domains

        response = requests.post(
            f"{base_url}/search",
            json=body,
            headers=_tavily_headers(),
            timeout=_TAVILY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()

        results: list[SearchResult] = []
        for item in data.get("results", []):
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", item.get("snippet", "")),
                )
            )
        return results[: opts.num_results]


def fetch_with_tavily(url: str) -> FetchResult | RedirectInfo:
    """Extract a web page as clean Markdown via Tavily /extract endpoint."""
    start = time.monotonic()
    base_url, _ = _get_tavily_config()

    body = {"urls": [url]}
    response = requests.post(
        f"{base_url}/extract",
        json=body,
        headers=_tavily_headers(),
        timeout=_TAVILY_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()

    raw_content = data.get("raw_content", "")
    if not raw_content:
        results = data.get("results", [])
        if results and results[0].get("raw_content"):
            raw_content = results[0]["raw_content"]

    if not raw_content.strip():
        raise RuntimeError(
            f"Tavily Extract returned empty content for {url}. "
            "The page may require authentication or JavaScript rendering."
        )

    content_bytes = len(raw_content.encode("utf-8"))
    return FetchResult(
        url=url,
        content=raw_content,
        content_type="text/markdown",
        bytes=content_bytes,
        duration_ms=(time.monotonic() - start) * 1000,
    )
