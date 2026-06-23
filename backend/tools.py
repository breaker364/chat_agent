from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_mcp_adapters.sessions import StdioConnection
from langchain_mcp_adapters.tools import load_mcp_tools

from langchain_core.messages import HumanMessage, SystemMessage

from .adapters import (
    BingSearchAdapter,
    FetchResult,
    RedirectInfo,
    SearchOptions,
    SearchResult,
    create_fetch_adapter,
    create_search_adapter,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (tunable parameters only — no entity-specific values)
# ---------------------------------------------------------------------------
_ALLOWED_ROOT: str | None = None
_SEARCH_CACHE: dict[str, dict[str, Any]] = {}
_FETCH_CACHE: dict[str, dict[str, Any]] = {}
_SEARCH_CACHE_TTL_SECONDS = 900
_FETCH_CACHE_TTL_SECONDS = 900
_FETCH_TIMEOUT_SECONDS = 15
_MAX_FETCH_REDIRECTS = 10
_PYTHON_RUN_TIMEOUT_SECONDS = 60
_PYTHON_OUTPUT_MAX_CHARS = 12_000
_MAX_PARALLEL_SEARCH_ROUTES = 2

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/markdown, text/html, */*;q=0.8",
}

# ---------------------------------------------------------------------------
# File-system helpers (workspace sandboxing)
# ---------------------------------------------------------------------------


def set_allowed_root(path: str | Path) -> None:
    global _ALLOWED_ROOT
    _ALLOWED_ROOT = str(Path(path).resolve())


def _ensure_allowed(path: str) -> Path:
    raw = (path or "").strip() or "."
    candidate = Path(raw)
    root_str = _ALLOWED_ROOT or os.getcwd()
    root = Path(root_str).resolve()
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Access denied: {target} is outside {root}") from exc
    return target


def _workspace_root() -> Path:
    return Path(_ALLOWED_ROOT or os.getcwd()).resolve()


def _resolve_python_script_path(path: str) -> Path:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("Python file path is required.")
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (Path(_workspace_root()) / candidate).resolve()


def _resolve_python_executable(preferred: str = "") -> str:
    configured = preferred.strip()
    candidates = [
        configured,
        (os.environ.get("PYTHON_EXECUTABLE") or "").strip(),
        sys.executable or "",
        shutil.which("python") or "",
        shutil.which("py") or "",
        shutil.which("python3") or "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            resolved = Path(candidate).resolve(strict=False)
        except OSError:
            continue
        if resolved.exists():
            return str(resolved)
    raise FileNotFoundError(
        "Python interpreter not found. Set PYTHON_EXECUTABLE or run the agent from a Python environment."
    )


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate the process and any child processes it may have started."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        else:
            os.killpg(process.pid, 9)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


class RunPythonFileInput(BaseModel):
    """Arguments for running a Python script via the agent tool."""

    path: str = Field(..., description="Path to the Python script. Relative paths use the workspace root.")
    cli_args: str = Field("", description="Command-line arguments passed to the script.")
    timeout_seconds: int = Field(_PYTHON_RUN_TIMEOUT_SECONDS, description="Execution timeout in seconds.")
    working_directory: str = Field("", description="Optional working directory for the script.")
    python_executable: str = Field("", description="Optional Python executable path.")


# ---------------------------------------------------------------------------
# URL / network helpers
# ---------------------------------------------------------------------------


def _validate_public_url(url: str) -> bool:
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return False
    if hostname.endswith(".local"):
        return False
    # Reject URLs with username/password
    if parsed.username or parsed.password:
        return False
    # Must have at least one dot in hostname
    if "." not in hostname:
        return False
    return True


def _upgrade_url_if_needed(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "http":
        return parsed._replace(scheme="https").geturl()
    return url


def _is_permitted_redirect(source_url: str, redirect_url: str) -> bool:
    """Allow same-domain redirects (including www ↔ non-www changes)."""
    try:
        source = urlparse(source_url)
        target = urlparse(redirect_url)
    except Exception:
        return False
    if target.scheme != source.scheme:
        return False
    if target.port != source.port:
        return False
    if target.username or target.password:
        return False
    source_host = re.sub(r"^www\.", "", (source.hostname or "").lower())
    target_host = re.sub(r"^www\.", "", (target.hostname or "").lower())
    return source_host == target_host


def _fetch_url_with_permitted_redirects(
    url: str,
    timeout_seconds: int = _FETCH_TIMEOUT_SECONDS,
) -> requests.Response | dict[str, Any]:
    """Fetch a URL, following same-host redirects, stopping at cross-host ones."""
    current_url = _upgrade_url_if_needed(url)
    for _ in range(_MAX_FETCH_REDIRECTS + 1):
        response = requests.get(
            current_url,
            headers=_FETCH_HEADERS,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        if response.status_code not in {301, 302, 307, 308}:
            return response
        redirect_location = response.headers.get("Location")
        if not redirect_location:
            break
        redirect_url = urljoin(current_url, redirect_location)
        if _is_permitted_redirect(current_url, redirect_url):
            current_url = redirect_url
            continue
        return {
            "type": "redirect",
            "original_url": current_url,
            "redirect_url": redirect_url,
            "status_code": response.status_code,
        }
    raise requests.TooManyRedirects(f"Too many redirects while fetching {url}")


def _html_to_text(html: str) -> tuple[str, str]:
    """Return (title, body_text) from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return title, text


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _contains_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _tokenize_query(query: str) -> list[str]:
    return [token for token in re.split(r"\s+", (query or "").strip()) if token]


def _keyword_fallback_query(query: str) -> str:
    tokens = _tokenize_query(query)
    if not tokens:
        return ""
    return " ".join(tokens[:12])


def _build_cross_language_query(query: str) -> str:
    """Build a keyword-style query in the other language for bilingual search."""
    normalized = (query or "").strip()
    if not normalized:
        return ""

    target_language = "English" if _contains_cjk(normalized) else "Simplified Chinese"
    system_text = (
        "You rewrite web search queries for bilingual retrieval. "
        "Return only a compact search-engine query in the requested language. "
        "Keep product names, model names, years, numbers, and acronyms exact. "
        "Do not add explanations, quotes, or markdown."
    )
    user_text = (
        f"Original query: {normalized}\n"
        f"Target language: {target_language}\n"
        "Rewrite it as a concise search query optimized for web search."
    )
    llm = _get_secondary_llm()
    response = llm.invoke(
        [SystemMessage(content=system_text), HumanMessage(content=user_text)]
    )
    rewritten = response.content if hasattr(response, "content") else str(response)
    candidate = (rewritten or "").strip().strip('"').strip("'")
    if not candidate or candidate.casefold() == normalized.casefold():
        candidate = _keyword_fallback_query(normalized)
        if not candidate or candidate.casefold() == normalized.casefold():
            return ""
    return candidate


def _build_bilingual_queries(query: str) -> list[tuple[str, str]]:
    normalized = (query or "").strip()
    if not normalized:
        return []

    routes: list[tuple[str, str]] = [("primary", normalized)]
    try:
        alternate = _build_cross_language_query(normalized)
    except Exception as exc:
        logger.warning("Cross-language query generation failed: %s", exc)
        alternate = ""
    if alternate and alternate.casefold() != normalized.casefold():
        routes.append(("alternate", alternate))
    return routes[:_MAX_PARALLEL_SEARCH_ROUTES]


def _merge_search_results(
    route_results: list[tuple[str, str, list[SearchResult]]],
    count: int,
) -> tuple[list[SearchResult], list[dict[str, Any]]]:
    """Merge results from multiple search routes, deduplicating by URL.

    Returns at most `count` results, interleaving from different routes
    for diversity (primary first, then alternate, etc.).
    """
    # First pass: collect all unique results per route
    per_route: list[tuple[str, str, list[SearchResult]]] = []
    all_seen: set[str] = set()
    for route_name, route_query, results in route_results:
        unique: list[SearchResult] = []
        for r in results:
            key = (r.url or "").strip().lower()
            if not key or key in all_seen:
                continue
            all_seen.add(key)
            unique.append(r)
        per_route.append((route_name, route_query, unique))

    # Build route_info from all routes
    route_info: list[dict[str, Any]] = [
        {"route": name, "query": q, "result_count": len(items)}
        for name, q, items in per_route
    ]

    # Interleave: round-robin from each route for diversity
    merged: list[SearchResult] = []
    indices = [0] * len(per_route)
    while len(merged) < count:
        added = False
        for i, (_, _, items) in enumerate(per_route):
            if indices[i] < len(items):
                merged.append(items[indices[i]])
                indices[i] += 1
                added = True
                if len(merged) >= count:
                    break
        if not added:
            break

    return merged, route_info


# ---------------------------------------------------------------------------
# File-operation tools
# ---------------------------------------------------------------------------


@tool
def list_directory(path: str) -> str:
    """List files and folders in a directory. Provide a relative path from the workspace root."""
    if not path or path.strip() == "":
        path = "."
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
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
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if not target.is_file():
        return f"File not found: {target}"
    try:
        raw = target.read_bytes()
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return f"[Binary file: {target.name}, size={len(raw)} bytes]"


@tool
def get_file_info(path: str) -> str:
    """Get metadata about a file or directory (size, modified time, type)."""
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if not target.exists():
        return f"Not found: {target}"
    stat = target.stat()
    kind = "directory" if target.is_dir() else "file"
    return "\n".join(
        [
            f"Name: {target.name}",
            f"Path: {target}",
            f"Type: {kind}",
            f"Size: {stat.st_size:,} bytes",
            f"Modified: {stat.st_mtime}",
        ]
    )


@tool
def write_file(path: str, content: str, overwrite: bool = True) -> str:
    """Create or replace a text file in the workspace. Use overwrite=false to avoid replacing an existing file."""
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if target.exists() and target.is_dir():
        return f"Cannot write file because target is a directory: {target}"
    if target.exists() and not overwrite:
        return f"File already exists and overwrite is false: {target}"
    existed_before = target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or "", encoding="utf-8", newline="\n")
    action = "Updated" if existed_before else "Created"
    return f"{action} file: {target}"


@tool
def append_file(path: str, content: str) -> str:
    """Append text to a file in the workspace. Creates the file if it does not exist."""
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if target.exists() and target.is_dir():
        return f"Cannot append because target is a directory: {target}"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(content or "")
    return f"Appended to file: {target}"


@tool
def delete_file(path: str) -> str:
    """Delete a file in the workspace. Does not delete directories."""
    try:
        target = _ensure_allowed(path)
    except PermissionError as exc:
        return str(exc)
    if not target.exists():
        return f"File not found: {target}"
    if target.is_dir():
        return f"Refusing to delete directory with delete_file: {target}"
    target.unlink()
    return f"Deleted file: {target}"


@tool(args_schema=RunPythonFileInput)
def run_python_file(
    path: str,
    cli_args: str = "",
    timeout_seconds: int = _PYTHON_RUN_TIMEOUT_SECONDS,
    working_directory: str = "",
    python_executable: str = "",
) -> str:
    """Run a Python file for local verification. Relative paths use the workspace root; absolute paths may be outside it."""
    try:
        target = _resolve_python_script_path(path)
    except Exception as exc:
        return json.dumps({"path": path, "error": str(exc)}, ensure_ascii=False, indent=2)

    if not target.is_file():
        return json.dumps({"path": str(target), "error": "File not found."}, ensure_ascii=False, indent=2)
    if target.suffix.lower() != ".py":
        return json.dumps({"path": str(target), "error": "Only .py files can be executed."}, ensure_ascii=False, indent=2)

    try:
        timeout_value = max(1, int(timeout_seconds))
    except (TypeError, ValueError):
        timeout_value = _PYTHON_RUN_TIMEOUT_SECONDS

    try:
        resolved_python_executable = _resolve_python_executable(python_executable)
    except Exception as exc:
        return json.dumps(
            {"path": str(target), "error": str(exc)},
            ensure_ascii=False,
            indent=2,
        )

    if working_directory.strip():
        try:
            cwd_path = Path(working_directory).expanduser()
            if not cwd_path.is_absolute():
                cwd_path = (_workspace_root() / cwd_path).resolve()
            else:
                cwd_path = cwd_path.resolve()
        except Exception as exc:
            return json.dumps(
                {"path": str(target), "error": f"Invalid working_directory: {exc}"},
                ensure_ascii=False,
                indent=2,
            )
    else:
        cwd_path = target.parent if target.parent.exists() else _workspace_root()

    if not cwd_path.exists() or not cwd_path.is_dir():
        return json.dumps(
            {
                "path": str(target),
                "error": f"Working directory not found or not a directory: {cwd_path}",
            },
            ensure_ascii=False,
            indent=2,
        )

    command = [resolved_python_executable, str(target), *shlex.split(cli_args or "", posix=False)]
    started_at = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd_path),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = creationflags
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except Exception as exc:
        return json.dumps(
            {"path": str(target), "command": command, "error": str(exc)},
            ensure_ascii=False,
            indent=2,
        )

    try:
        stdout_text, stderr_text = process.communicate(timeout=timeout_value)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            process.wait(timeout=2)
        except Exception:
            pass
        return json.dumps(
            {
                "path": str(target),
                "command": command,
                "cwd": str(cwd_path),
                "timed_out": True,
                "timeout_seconds": timeout_value,
                "duration_seconds": round(time.monotonic() - started_at, 3),
                "stdout": "",
                "stderr": "",
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps(
            {"path": str(target), "command": command, "error": str(exc)},
            ensure_ascii=False,
            indent=2,
        )

    return json.dumps(
        {
            "path": str(target),
            "command": command,
            "cwd": str(cwd_path),
            "exit_code": process.returncode,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "stdout": (stdout_text or "")[:_PYTHON_OUTPUT_MAX_CHARS],
            "stderr": (stderr_text or "")[:_PYTHON_OUTPUT_MAX_CHARS],
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Web search tool (adapter-based, like Claude Code)
# ---------------------------------------------------------------------------


def _format_search_results(query: str, results: list[SearchResult], adapter_name: str, duration_ms: float) -> str:
    """Format search results as structured JSON — compatible with Claude Code output style."""
    output: dict[str, Any] = {
        "query": query,
        "engine": adapter_name,
        "duration_seconds": round(duration_ms / 1000, 3),
        "result_count": len(results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
            }
            for r in results
        ],
    }
    if not results:
        output["note"] = "No search results found. Try a different query or narrower terms."
    return json.dumps(output, ensure_ascii=False, indent=2)


@tool
def web_search(
    query: str,
    count: int = 8,
    allowed_domains: list[str] | None = None,
    blocked_domains: list[str] | None = None,
) -> str:
    """Search the web and return structured results (titles, URLs, snippets).

    The search adapter is selected automatically — Tavily if configured via
    TAVILY_BASE_URL, otherwise Bing HTML scraping (no API key required).

    Use `allowed_domains` to restrict results to specific domains.
    Use `blocked_domains` to exclude specific domains.
    """
    # Cache check
    cache_key = json.dumps(
        {
            "query": query.strip(),
            "count": count,
            "allowed_domains": allowed_domains or [],
            "blocked_domains": blocked_domains or [],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    now = time.time()
    cached = _SEARCH_CACHE.get(cache_key)
    if cached and now - cached["ts"] < _SEARCH_CACHE_TTL_SECONDS:
        return json.dumps(cached["payload"], ensure_ascii=False, indent=2)

    start = time.monotonic()
    routes = _build_bilingual_queries(query.strip())
    options = SearchOptions(
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        num_results=count,
    )

    try:
        adapter = create_search_adapter()
        route_results: list[tuple[str, str, list[SearchResult]]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(routes))) as executor:
            future_map = {
                executor.submit(adapter.search, route_query, options): (route_name, route_query)
                for route_name, route_query in routes
            }
            for future in as_completed(future_map):
                route_name, route_query = future_map[future]
                route_results.append((route_name, route_query, future.result()))
        route_results.sort(key=lambda item: 0 if item[0] == "primary" else 1)
        results, route_info = _merge_search_results(route_results, count)
        duration_ms = (time.monotonic() - start) * 1000
        payload_str = _format_search_results(query, results, adapter.name, duration_ms)
        payload = json.loads(payload_str)
        payload["search_routes"] = route_info
        payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
    except Exception as exc:
        requested_engine = getattr(adapter, "name", "?")
        logger.warning("Search adapter '%s' failed: %s", requested_engine, exc)
        # Fallback to Bing if Tavily fails
        adapter = BingSearchAdapter()
        try:
            route_results = []
            with ThreadPoolExecutor(max_workers=max(1, len(routes))) as executor:
                future_map = {
                    executor.submit(adapter.search, route_query, options): (route_name, route_query)
                    for route_name, route_query in routes
                }
                for future in as_completed(future_map):
                    route_name, route_query = future_map[future]
                    route_results.append((route_name, route_query, future.result()))
            route_results.sort(key=lambda item: 0 if item[0] == "primary" else 1)
            results, route_info = _merge_search_results(route_results, count)
            duration_ms = (time.monotonic() - start) * 1000
            payload_str = _format_search_results(query, results, adapter.name, duration_ms)
            payload = json.loads(payload_str)
            payload["requested_engine"] = requested_engine
            payload["fallback_reason"] = str(exc)
            payload["search_routes"] = route_info
            payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
        except Exception as fallback_exc:
            duration_ms = (time.monotonic() - start) * 1000
            payload_str = json.dumps(
                {
                    "query": query,
                    "engine": "none",
                    "duration_seconds": round(duration_ms / 1000, 3),
                    "result_count": 0,
                    "results": [],
                    "error": f"Search failed: {fallback_exc}",
                },
                ensure_ascii=False,
                indent=2,
            )

    payload = json.loads(payload_str)
    _SEARCH_CACHE[cache_key] = {"ts": now, "payload": payload}
    return payload_str


# ---------------------------------------------------------------------------
# Web fetch tool (adapter-based, like Claude Code)
# ---------------------------------------------------------------------------

# Cached secondary LLM for applying prompt to fetched content
_SECONDARY_LLM: Any = None
_FETCH_CONTENT_MAX_CHARS = 10_000  # max chars fed to secondary model


def _get_secondary_llm():
    """Return a cached ChatDeepSeek instance for content processing."""
    global _SECONDARY_LLM
    if _SECONDARY_LLM is None:
        from .config import create_chat_deepseek, load_llm_config

        cfg = load_llm_config()
        _SECONDARY_LLM = create_chat_deepseek(cfg, temperature=0.0, streaming=False)
    return _SECONDARY_LLM


def _apply_prompt_to_content(prompt: str, content: str) -> str:
    """Process fetched content with the user's prompt via a secondary LLM call.

    Mirrors Claude Code's applyPromptToMarkdown / makeSecondaryModelPrompt pattern.
    """
    truncated = content[:_FETCH_CONTENT_MAX_CHARS]
    if len(content) > _FETCH_CONTENT_MAX_CHARS:
        truncated += "\n\n[Content truncated due to length...]"

    system_text = (
        "You are a precise content extraction assistant. "
        "Answer ONLY based on the provided web page content. "
        "If the content does not contain the requested information, say so clearly. "
        "Keep quotes under 125 characters. Do not reproduce song lyrics."
    )
    user_text = (
        f"Web page content:\n---\n{truncated}\n---\n\n"
        f"{prompt}\n\n"
        f"Provide a concise response based only on the content above. "
        f"Include specific details, numbers, and names where available."
    )

    llm = _get_secondary_llm()
    response = llm.invoke(
        [SystemMessage(content=system_text), HumanMessage(content=user_text)]
    )
    return response.content if hasattr(response, "content") else str(response)


def _format_fetch_success(fetch_result: FetchResult, prompt: str) -> str:
    """Format a successful fetch as JSON — matches Claude Code output schema."""
    content = fetch_result.content
    if prompt and prompt.strip():
        try:
            result = _apply_prompt_to_content(prompt, content)
        except Exception as exc:
            logger.warning("Secondary model call failed: %s — returning raw content", exc)
            result = content[:_FETCH_CONTENT_MAX_CHARS]
    else:
        result = content[:_FETCH_CONTENT_MAX_CHARS]

    payload: dict[str, Any] = {
        "url": fetch_result.url,
        "code": fetch_result.code,
        "code_text": fetch_result.code_text,
        "bytes": fetch_result.bytes,
        "result": result,
        "content_type": fetch_result.content_type,
        "duration_seconds": round(fetch_result.duration_ms / 1000, 3),
        "prompt": prompt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _format_fetch_redirect(redirect: RedirectInfo, prompt: str) -> str:
    """Format a cross-host redirect notice as JSON."""
    payload = {
        "url": redirect.original_url,
        "code": redirect.status_code,
        "code_text": HTTPStatus(redirect.status_code).phrase,
        "result": (
            "REDIRECT DETECTED: The URL redirects to a different host.\n"
            f"Original URL: {redirect.original_url}\n"
            f"Redirect URL: {redirect.redirect_url}\n"
            "Please call web_fetch again with the redirect URL if you want that page."
        ),
        "duration_seconds": 0,
        "prompt": prompt,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


@tool
def web_fetch(url: str, prompt: str = "") -> str:
    """Fetch a webpage, process with the prompt, and return a result.

    - Fetches the URL, converts HTML to text/Markdown.
    - If Tavily Extract is configured (TAVILY_BASE_URL), returns clean Markdown directly.
    - When `prompt` is provided, a secondary model processes the content
      to answer your prompt, and the answer is returned in the `result` field.
    - When `prompt` is empty, `result` contains the raw content (truncated).
    - HTTP URLs are automatically upgraded to HTTPS.
    - Cross-host redirects are reported (not followed).
    - Results are cached for 15 minutes.
    """
    if not _validate_public_url(url):
        return json.dumps({"url": url, "error": "Invalid URL."}, ensure_ascii=False, indent=2)

    # Cache check (keyed by url only — prompt variations share the same fetched content)
    cache_key = json.dumps({"url": url}, ensure_ascii=False, sort_keys=True)
    now = time.time()
    cached = _FETCH_CACHE.get(cache_key)
    cached_content: str | None = None
    if cached and now - cached["ts"] < _FETCH_CACHE_TTL_SECONDS:
        cached_content = cached.get("content")

    started_at = time.monotonic()

    if cached_content is not None:
        # Re-apply prompt to cached content
        fetch_result = FetchResult(
            url=url,
            content=cached_content,
            content_type=cached.get("content_type", "text/plain"),
            bytes=cached.get("bytes", len(cached_content.encode("utf-8"))),
            code=cached.get("code", 200),
            code_text=cached.get("code_text", "OK"),
            duration_ms=0,
        )
        return _format_fetch_success(fetch_result, prompt)

    # Try Tavily Extract first if available
    fetch_adapter = create_fetch_adapter()
    if fetch_adapter is not None:
        try:
            result = fetch_adapter.fetch(url)
            if isinstance(result, RedirectInfo):
                return _format_fetch_redirect(result, prompt)
            # Cache the raw content
            _FETCH_CACHE[cache_key] = {
                "ts": now,
                "content": result.content,
                "content_type": result.content_type,
                "bytes": result.bytes,
                "code": result.code,
                "code_text": result.code_text,
            }
            return _format_fetch_success(result, prompt)
        except Exception as exc:
            logger.warning("Tavily fetch failed for %s: %s — falling back to direct HTTP", url, exc)

    # Direct HTTP fetch
    try:
        response = _fetch_url_with_permitted_redirects(url)
    except requests.Timeout:
        return json.dumps(
            {"url": url, "error": f"Timed out after {_FETCH_TIMEOUT_SECONDS}s while fetching the page."},
            ensure_ascii=False,
            indent=2,
        )
    except Exception as exc:
        return json.dumps({"url": url, "error": str(exc)}, ensure_ascii=False, indent=2)

    if isinstance(response, dict):
        return _format_fetch_redirect(
            RedirectInfo(
                original_url=response["original_url"],
                redirect_url=response["redirect_url"],
                status_code=response["status_code"],
            ),
            prompt,
        )

    try:
        response.raise_for_status()
        response.encoding = response.apparent_encoding or response.encoding
        _, text = _html_to_text(response.text)
        duration_ms = (time.monotonic() - started_at) * 1000

        fetch_result = FetchResult(
            url=str(response.url),
            content=text,
            content_type="text/plain",
            bytes=len(response.content),
            code=response.status_code,
            code_text=response.reason,
            duration_ms=duration_ms,
        )
        # Cache the raw content (without prompt-specific result)
        _FETCH_CACHE[cache_key] = {
            "ts": now,
            "content": text,
            "content_type": "text/plain",
            "bytes": len(response.content),
            "code": response.status_code,
            "code_text": response.reason,
        }
        return _format_fetch_success(fetch_result, prompt)
    except Exception as exc:
        return json.dumps(
            {"url": url, "error": f"HTTP {response.status_code}: {exc}"},
            ensure_ascii=False,
            indent=2,
        )


fetch_webpage = web_fetch


# ---------------------------------------------------------------------------
# 12306 MCP tools (unchanged)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Tool collections
# ---------------------------------------------------------------------------

_FILE_TOOLS: list[Any] = [
    list_directory,
    read_file,
    get_file_info,
    write_file,
    append_file,
    delete_file,
    run_python_file,
]
_SEARCH_TOOLS: list[Any] = [web_search, web_fetch]


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
        logger.warning("12306 MCP tools unavailable: %s", exc)
    return tools
