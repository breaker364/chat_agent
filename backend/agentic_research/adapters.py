from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .models import EvidenceObservation


class EvidenceRegistryError(ValueError):
    """Raised when a non-research or non-read-only adapter is registered."""


class EvidenceAdapter(Protocol):
    source_kind: str

    def collect(self, query_or_scope: str) -> EvidenceObservation:
        ...


def _invoke(tool: Any, payload: dict[str, Any], positional: str) -> Any:
    invoke = getattr(tool, "invoke", None)
    if callable(invoke):
        return invoke(payload)
    if callable(tool):
        return tool(positional)
    raise TypeError("evidence adapter callback is not callable")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except Exception:
            return {"text": value}
        return dict(decoded) if isinstance(decoded, Mapping) else {"text": str(decoded)}
    return {"text": str(value or "")}


def _error_observation(source_kind: str, exc: BaseException) -> EvidenceObservation:
    category = "provider_unavailable"
    if isinstance(exc, PermissionError):
        category = "access_denied"
    elif isinstance(exc, (FileNotFoundError,)):
        category = "not_found"
    return EvidenceObservation(
        source_kind=source_kind,
        status=category,
        error_category=category,
        metadata={"error_type": type(exc).__name__},
    )


def _result_items(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = data.get("results")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)][:32]


class KnowledgeEvidenceAdapter:
    source_kind = "personal_knowledge"

    def __init__(self, search: Callable[..., Any], *, result_limit: int = 8, excerpt_char_limit: int = 1200) -> None:
        self.search = search
        self.result_limit = max(1, min(int(result_limit), 20))
        self.excerpt_char_limit = max(128, min(int(excerpt_char_limit), 4000))

    def collect(self, query_or_scope: str) -> EvidenceObservation:
        try:
            data = _payload(_invoke(self.search, {"query": query_or_scope, "top_k": self.result_limit}, query_or_scope))
            items = _result_items(data)[: self.result_limit]
            citations = [
                {
                    "id": item.get("citation_id") or item.get("chunk_id") or item.get("doc_id"),
                    "title": item.get("title") or item.get("source_ref"),
                    "uri": item.get("source_url") or item.get("source_uri"),
                }
                for item in items
            ]
            excerpts = [item.get("excerpt") or item.get("snippet") or "" for item in items]
            error = data.get("error") if isinstance(data.get("error"), Mapping) else {}
            status = "error" if error else ("ok" if items else "no_results")
            return EvidenceObservation(
                source_kind=self.source_kind,
                status=status,
                citations=citations,
                excerpts=excerpts,
                query=query_or_scope,
                metadata={"settings": data.get("settings", {}) if isinstance(data.get("settings"), Mapping) else {}},
                citation_limit=self.result_limit,
                excerpt_limit=self.excerpt_char_limit,
                error_category=str(error.get("code") or "")[:80],
                raw_payload=data,
            )
        except Exception as exc:
            return _error_observation(self.source_kind, exc)


class WorkspaceEvidenceAdapter:
    source_kind = "workspace"

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        read_file: Callable[..., Any],
        list_directory: Callable[..., Any] | None = None,
        get_file_info: Callable[..., Any] | None = None,
        excerpt_char_limit: int = 1200,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.read_file = read_file
        self.list_directory = list_directory
        self.get_file_info = get_file_info
        self.excerpt_char_limit = max(128, min(int(excerpt_char_limit), 4000))

    def collect(self, query_or_scope: str) -> EvidenceObservation:
        requested = Path(str(query_or_scope or "").strip()).expanduser()
        candidate = requested.resolve() if requested.is_absolute() else (self.workspace_root / requested).resolve()
        try:
            candidate.relative_to(self.workspace_root)
        except ValueError:
            return EvidenceObservation(
                source_kind=self.source_kind,
                status="access_denied",
                error_category="access_denied",
                query=query_or_scope,
            )
        relative = candidate.relative_to(self.workspace_root).as_posix()
        if candidate.is_dir():
            if self.list_directory is None:
                return EvidenceObservation(
                    source_kind=self.source_kind,
                    status="not_found",
                    error_category="not_found",
                    query=query_or_scope,
                )
            try:
                listing = str(_invoke(self.list_directory, {"path": relative}, relative) or "")
                return EvidenceObservation(
                    source_kind=self.source_kind,
                    status="ok" if listing.strip() else "no_results",
                    citations=[{"id": relative, "title": candidate.name or relative, "uri": f"workspace://{relative}"}],
                    excerpts=[listing],
                    query=relative,
                    metadata={"path": relative, "kind": "directory"},
                    citation_limit=1,
                    excerpt_limit=self.excerpt_char_limit,
                    raw_payload=listing,
                )
            except Exception as exc:
                return _error_observation(self.source_kind, exc)
        if not candidate.is_file():
            return EvidenceObservation(
                source_kind=self.source_kind,
                status="not_found",
                error_category="not_found",
                query=query_or_scope,
            )
        try:
            text = _invoke(self.read_file, {"path": relative}, relative)
            text = str(text or "")
            return EvidenceObservation(
                source_kind=self.source_kind,
                status="ok" if text.strip() else "no_results",
                citations=[{"id": relative, "title": candidate.name, "uri": f"workspace://{relative}"}],
                excerpts=[text],
                query=relative,
                metadata={"path": relative, "suffix": candidate.suffix.lower()},
                citation_limit=1,
                excerpt_limit=self.excerpt_char_limit,
                raw_payload=text,
            )
        except Exception as exc:
            return _error_observation(self.source_kind, exc)


class WebEvidenceAdapter:
    source_kind = "web"

    def __init__(
        self,
        search: Callable[..., Any],
        fetch: Callable[..., Any] | None = None,
        *,
        result_limit: int = 8,
        excerpt_char_limit: int = 1200,
    ) -> None:
        self.search = search
        self.fetch = fetch
        self.result_limit = max(1, min(int(result_limit), 20))
        self.excerpt_char_limit = max(128, min(int(excerpt_char_limit), 4000))

    def collect(self, query_or_scope: str) -> EvidenceObservation:
        try:
            data = _payload(_invoke(self.search, {"query": query_or_scope, "count": self.result_limit}, query_or_scope))
            items = _result_items(data)[: self.result_limit]
            citations = [
                {
                    "id": item.get("url") or item.get("title"),
                    "title": item.get("title"),
                    "uri": item.get("url"),
                }
                for item in items
            ]
            excerpts = [item.get("snippet") or item.get("excerpt") or "" for item in items]
            if self.fetch and items and str(items[0].get("url") or "").strip():
                try:
                    fetched = _payload(
                        _invoke(
                            self.fetch,
                            {"url": items[0]["url"], "prompt": query_or_scope, "download": False},
                            str(items[0]["url"]),
                        )
                    )
                    excerpt = fetched.get("result") or fetched.get("content") or fetched.get("text")
                    if isinstance(excerpt, str) and excerpt.strip():
                        excerpts[0] = excerpt
                except Exception:
                    pass
            return EvidenceObservation(
                source_kind=self.source_kind,
                status="ok" if items else "no_results",
                citations=citations,
                excerpts=excerpts,
                query=query_or_scope,
                metadata={"engine": str(data.get("engine") or "")[:80]},
                citation_limit=self.result_limit,
                excerpt_limit=self.excerpt_char_limit,
                raw_payload=data,
            )
        except Exception as exc:
            return _error_observation(self.source_kind, exc)


class EvidenceRegistry:
    allowed_source_kinds = frozenset({"personal_knowledge", "workspace", "web"})

    def __init__(self, adapters: Mapping[str, EvidenceAdapter]) -> None:
        invalid = set(adapters) - self.allowed_source_kinds
        if invalid:
            raise EvidenceRegistryError(
                f"only read-only evidence source kinds may be registered: {sorted(invalid)}"
            )
        for source_kind, adapter in adapters.items():
            if not callable(getattr(adapter, "collect", None)):
                raise EvidenceRegistryError(f"adapter {source_kind} does not implement collect")
        self._adapters = dict(adapters)

    @property
    def source_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def get(self, source_kind: str) -> EvidenceAdapter:
        try:
            return self._adapters[source_kind]
        except KeyError as exc:
            raise EvidenceRegistryError(f"source kind is not registered: {source_kind}") from exc
