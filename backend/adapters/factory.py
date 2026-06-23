"""
Search adapter factory - selects the appropriate backend.

Priority (highest first):
  1. WEB_SEARCH_ADAPTER environment variable (explicit override)
  2. Default: 'tavily'
"""

from __future__ import annotations

import os

from .base import WebFetchAdapter, WebSearchAdapter
from .bing_adapter import BingSearchAdapter
from .tavily_adapter import TavilySearchAdapter, fetch_with_tavily

_SEARCH_CACHE: WebSearchAdapter | None = None
_SEARCH_CACHE_KEY: str | None = None


def _get_search_adapter_key() -> str:
    return os.environ.get("WEB_SEARCH_ADAPTER", "").strip().lower() or "tavily"


def create_search_adapter() -> WebSearchAdapter:
    """Return a WebSearchAdapter - Tavily (default) or Bing (env override)."""
    global _SEARCH_CACHE, _SEARCH_CACHE_KEY

    adapter_key = _get_search_adapter_key()

    if _SEARCH_CACHE is not None and _SEARCH_CACHE_KEY == adapter_key:
        return _SEARCH_CACHE

    adapter: WebSearchAdapter
    if adapter_key == "bing":
        adapter = BingSearchAdapter()
    else:
        adapter = TavilySearchAdapter()

    _SEARCH_CACHE = adapter
    _SEARCH_CACHE_KEY = adapter_key
    return adapter


def create_fetch_adapter() -> WebFetchAdapter | None:
    """Return a WebFetchAdapter - Tavily Extract (default) or None for direct HTTP."""
    adapter_key = _get_search_adapter_key()
    if adapter_key == "bing":
        return None

    class _TavilyFetchAdapter:
        @property
        def name(self) -> str:
            return "tavily"

        def fetch(self, url: str):
            return fetch_with_tavily(url)

    return _TavilyFetchAdapter()
