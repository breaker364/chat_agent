from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class SearchResult:
    """A single search result from any search backend."""

    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchOptions:
    """Options passed to a search adapter."""

    allowed_domains: list[str] | None = None
    blocked_domains: list[str] | None = None
    num_results: int = 8


@dataclass
class FetchResult:
    """Result from fetching/extracting a web page."""

    url: str
    content: str
    content_type: str = "text/plain"
    bytes: int = 0
    code: int = 200
    code_text: str = "OK"
    duration_ms: float = 0


@dataclass
class RedirectInfo:
    """Returned when a URL redirects to a different host."""

    type: str = "redirect"
    original_url: str = ""
    redirect_url: str = ""
    status_code: int = 302


@runtime_checkable
class WebSearchAdapter(Protocol):
    """Protocol for web search backends (Tavily, Bing, etc.)."""

    def search(self, query: str, options: SearchOptions | None = None) -> list[SearchResult]:
        ...

    @property
    def name(self) -> str:
        ...


@runtime_checkable
class WebFetchAdapter(Protocol):
    """Protocol for web page fetching/extraction backends."""

    def fetch(self, url: str) -> FetchResult | RedirectInfo:
        ...

    @property
    def name(self) -> str:
        ...
