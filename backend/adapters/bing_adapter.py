"""
Bing HTML-scraping search adapter.

Fetches Bing search result pages and extracts organic results
using regex matching on raw HTML — no API key required.
"""

from __future__ import annotations

import base64
import re
from html import unescape as _html_unescape

import requests

from .base import SearchOptions, SearchResult, WebSearchAdapter

_SEARCH_TIMEOUT_SECONDS = 12
_MAX_BING_RESULTS = 12

_BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

_BING_HEADERS_EN = {
    **_BING_HEADERS,
    "Accept-Language": "en-US,en;q=0.9",
}


def _contains_chinese(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


def _decode_html_entities(value: str) -> str:
    return _html_unescape(value or "")


def _resolve_bing_url(raw_url: str) -> str | None:
    """Resolve a Bing redirect URL (base64-encoded `u` parameter) to the real URL."""
    if not raw_url or raw_url.startswith("/") or raw_url.startswith("#"):
        return None
    match = re.search(r"[?&]u=([a-zA-Z0-9+/_=-]+)", raw_url)
    if match:
        encoded = match.group(1)
        if len(encoded) >= 3:
            payload = encoded[2:]
            padded = payload.replace("-", "+").replace("_", "/")
            while len(padded) % 4:
                padded += "="
            try:
                decoded = base64.b64decode(padded).decode("utf-8")
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
    if "bing.com" not in raw_url:
        return raw_url
    return None


def _extract_snippet(block: str) -> str:
    """Extract the snippet text from a Bing search result block."""
    patterns = [
        r'<p[^>]*class="b_lineclamp[^"]*"[^>]*>([\s\S]*?)</p>',
        r'<div[^>]*class="b_caption[^"]*"[^>]*>[\s\S]*?<p[^>]*>([\s\S]*?)</p>',
        r'<div[^>]*class="b_caption[^"]*"[^>]*>([\s\S]*?)</div>',
    ]
    for pattern in patterns:
        match = re.search(pattern, block, re.IGNORECASE)
        if match:
            text = re.sub(r"<[^>]+>", "", match.group(1)).strip()
            if text:
                return _decode_html_entities(text)
    return ""


def _extract_domain(url: str) -> str:
    """Extract hostname from URL, stripping 'www.' prefix."""
    from urllib.parse import urlparse

    hostname = (urlparse(url).hostname or "").lower()
    return re.sub(r"^www\.", "", hostname)


def _matches_domain(url: str, domains: list[str]) -> bool:
    """Check if the URL hostname matches any domain in the list."""
    try:
        hostname = _extract_domain(url)
    except Exception:
        return False
    return any(hostname == d or hostname.endswith("." + d) for d in domains)


class BingSearchAdapter:
    """Scrapes Bing.com HTML search results as structured data."""

    @property
    def name(self) -> str:
        return "bing"

    def search(self, query: str, options: SearchOptions | None = None) -> list[SearchResult]:
        opts = options or SearchOptions()
        count = max(opts.num_results, 3)

        raw_results = self._scrape(query, count)

        # Apply domain filters
        results = raw_results
        if opts.allowed_domains:
            results = [r for r in results if _matches_domain(r.url, opts.allowed_domains)]
        if opts.blocked_domains:
            results = [r for r in results if not _matches_domain(r.url, opts.blocked_domains)]

        return results[:count]

    def _scrape(self, query: str, count: int) -> list[SearchResult]:
        """Fetch Bing and extract organic search results."""
        use_en = not _contains_chinese(query)
        headers = _BING_HEADERS_EN if use_en else _BING_HEADERS
        params: dict[str, str] = {"q": query}
        if use_en:
            params.update({"setmkt": "en-US", "setlang": "en-US"})
        else:
            params.update({"setmkt": "zh-CN", "setlang": "zh-Hans"})

        response = requests.get(
            "https://www.bing.com/search",
            params=params,
            headers=headers,
            timeout=_SEARCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        html = response.text

        results: list[SearchResult] = []
        for match in re.finditer(
            r'<li\s+class="b_algo"[^>]*>([\s\S]*?)</li>', html, re.IGNORECASE
        ):
            block = match.group(1)
            link_match = re.search(
                r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>',
                block,
                re.IGNORECASE,
            )
            if not link_match:
                continue
            url = _resolve_bing_url(_decode_html_entities(link_match.group(1)))
            if not url:
                continue
            title = _decode_html_entities(
                re.sub(r"<[^>]+>", "", link_match.group(2)).strip()
            )
            snippet = _extract_snippet(block)
            results.append(SearchResult(title=title, url=url, snippet=snippet[:400]))
            if len(results) >= max(count * 2, _MAX_BING_RESULTS):
                break

        return results
