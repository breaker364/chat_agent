from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from langchain_core.tools import tool
from langchain_mcp_adapters.sessions import StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools

_ALLOWED_ROOT: str | None = None
_SEARCH_CACHE: dict[str, dict[str, Any]] = {}
_SEARCH_CACHE_TTL_SECONDS = 300
_SEARCH_TIMEOUT_SECONDS = 4
_FETCH_TIMEOUT_SECONDS = 15


def set_allowed_root(path: str | Path) -> None:
    global _ALLOWED_ROOT
    _ALLOWED_ROOT = str(Path(path).resolve())


def _ensure_allowed(path: str) -> Path:
    root_str = _ALLOWED_ROOT or os.getcwd()
    root = Path(root_str).resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)):
        raise PermissionError(f"Access denied: {target} is outside {root}")
    return target


@tool
def list_directory(path: str) -> str:
    """List files and folders in a directory. Provide a relative path from the workspace root."""
    if not path or path.strip() == "":
        path = "."
    target = _ensure_allowed(path)
    if not target.is_dir():
        return f"Not a directory: {target}"
    items: list[str] = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        suffix = "/" if entry.is_dir() else ""
        items.append(f"{entry.name}{suffix}")
    return "\n".join(items) if items else "(empty directory)"


@tool
def read_file(path: str) -> str:
    """Read the contents of a text file. Provide a relative path from the workspace root."""
    target = _ensure_allowed(path)
    if not target.is_file():
        return f"File not found: {target}"
    try:
        raw = target.read_bytes()
        decoded = raw.decode("utf-8-sig")
        return decoded
    except UnicodeDecodeError:
        return f"[Binary file: {target.name}, size={len(raw)} bytes]"


@tool
def get_file_info(path: str) -> str:
    """Get metadata about a file or directory (size, modified time, type)."""
    target = _ensure_allowed(path)
    if not target.exists():
        return f"Not found: {target}"
    stat = target.stat()
    kind = "directory" if target.is_dir() else "file"
    lines = [
        f"Name: {target.name}",
        f"Path: {target}",
        f"Type: {kind}",
        f"Size: {stat.st_size:,} bytes",
        f"Modified: {stat.st_mtime}",
    ]
    return "\n".join(lines)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _extract_terms(query: str) -> tuple[list[str], list[str]]:
    ascii_terms = re.findall(r"[a-z0-9]+", _normalize_text(query))
    chinese_terms = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    return list(dict.fromkeys(ascii_terms)), list(dict.fromkeys(chinese_terms))


def _looks_like_model_code(term: str) -> bool:
    return bool(re.search(r"[a-z]+\d+|\d+[a-z]+", term))


def _relevance_score(query: str, title: str, snippet: str, url: str) -> int:
    haystack = _normalize_text(" ".join([title, snippet, url]))
    ascii_terms, chinese_terms = _extract_terms(query)
    score = 0

    for term in ascii_terms:
        if term in haystack:
            score += 4 if _looks_like_model_code(term) else 1

    for term in chinese_terms:
        if term in haystack:
            score += 2

    return score


def _min_score(query: str) -> int:
    ascii_terms, chinese_terms = _extract_terms(query)
    if any(_looks_like_model_code(term) for term in ascii_terms) and chinese_terms:
        return 4
    if chinese_terms:
        return 2
    return 1


def _is_low_quality_result(title: str, snippet: str, url: str) -> bool:
    text = _normalize_text(" ".join([title, snippet, url]))
    host = requests.utils.urlparse(url).netloc.lower()
    path = requests.utils.urlparse(url).path.lower()

    blocked_hosts = {
        "yuanbao.tencent.com",
    }
    if host in blocked_hosts:
        return True

    blocked_fragments = (
        "related searches",
        "猜你想搜",
        "大家还在搜",
        "精选视频",
        "搜索资讯页",
        "captcha",
        "环境异常",
    )
    if any(fragment in text for fragment in blocked_fragments):
        return True

    if "video-recommend" in path:
        return True

    return not title.strip()


def _search_with_ddgs(query: str, count: int) -> list[dict[str, str]]:
    with DDGS(timeout=_SEARCH_TIMEOUT_SECONDS) as ddgs:
        raw_results = list(
            ddgs.text(
                query,
                backend="bing",
                region="cn-zh",
                safesearch="off",
                max_results=max(count * 2, 10),
            )
        )
    results: list[dict[str, str]] = []
    for item in raw_results:
        title = str(item.get("title") or "").strip()
        url = str(item.get("href") or "").strip()
        snippet = str(item.get("body") or "").strip()
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet[:400]})
    return results


def _search_once(query: str, count: int) -> tuple[list[dict[str, str]], str, str | None]:
    try:
        return (
            _search_with_ddgs(query, count),
            "ddgs",
            None,
        )
    except Exception as exc:
        return (
            [],
            "ddgs",
            f"Search backend failed: {exc}",
        )


def _filter_results(query: str, results: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    threshold = _min_score(query)
    ranked: list[tuple[int, dict[str, str]]] = []
    seen_urls: set[str] = set()

    for item in results:
        if _is_low_quality_result(item["title"], item["snippet"], item["url"]):
            continue
        score = _relevance_score(query, item["title"], item["snippet"], item["url"])
        if score < threshold:
            continue
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        ranked.append((score, item))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in ranked[:count]]


@tool
def web_search(query: str, count: int = 3) -> str:
    """Search the web for the user's exact topic, filter noisy results, and return only relevant pages."""
    normalized_query = _normalize_text(query)
    now = time.time()
    cached = _SEARCH_CACHE.get(normalized_query)
    if cached and now - cached["ts"] < _SEARCH_CACHE_TTL_SECONDS:
        return json.dumps(cached["payload"], ensure_ascii=False, indent=2)

    raw_results, engine, backend_note = _search_once(query, count)
    filtered_results = _filter_results(query, raw_results, count)

    payload = {
        "query": query,
        "engine": engine,
        "result_count": len(filtered_results),
        "results": filtered_results,
    }
    if backend_note:
        payload["backend_note"] = backend_note
    if not filtered_results:
        payload["note"] = "No sufficiently relevant search results were found for the exact topic."

    _SEARCH_CACHE[normalized_query] = {"ts": now, "payload": payload}
    return json.dumps(payload, ensure_ascii=False, indent=2)


@tool
def fetch_webpage(url: str) -> str:
    """Fetch a webpage and return cleaned title and content excerpt."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=_FETCH_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.Timeout:
        return json.dumps(
            {
                "url": url,
                "error": f"Timed out after {_FETCH_TIMEOUT_SECONDS}s while fetching the page.",
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps(
            {
                "url": url,
                "error": str(exc),
            },
            ensure_ascii=False,
            indent=2,
        )

    response.encoding = response.apparent_encoding or response.encoding
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else response.url
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    payload = {
        "url": response.url,
        "title": title,
        "content_preview": text[:4000],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def load_12306_tools(
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> list[Any]:
    """Start the 12306-mcp server and return the LangChain tools."""
    connection = StdioConnection(
        transport="stdio",
        command="npx",
        args=["-y", "12306-mcp"],
        cwd=cwd or os.getcwd(),
        encoding="utf-8",
        encoding_error_handler="replace",
        env=env or dict(os.environ),
    )
    return await load_mcp_tools(session=None, connection=connection)


_FILE_TOOLS: list[Any] = [list_directory, read_file, get_file_info]
_SEARCH_TOOLS: list[Any] = [web_search, fetch_webpage]


async def get_all_tools(
    workspace_dir: str | Path | None = None,
) -> list[Any]:
    """Return the complete tool list: local search, file ops, and 12306 tools."""
    if workspace_dir is not None:
        set_allowed_root(workspace_dir)

    tools = list(_FILE_TOOLS) + list(_SEARCH_TOOLS)

    try:
        ticket_tools = await load_12306_tools(cwd=str(workspace_dir or os.getcwd()))
        tools.extend(ticket_tools)
    except Exception as exc:
        print(f"[WARN] 12306 MCP tools unavailable: {exc}")
    return tools
