from __future__ import annotations

import importlib
import html
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from .chunking import content_hash
from .retrieval import RagBackendError


def normalize_remote_content(content: Any, content_format: str = "markdown") -> str:
    """Normalize provider output without destroying Markdown structure."""
    value = str(content or "").replace("\x00", "")
    normalized_format = str(content_format or "markdown").strip().lower()
    if normalized_format in {"html", "htm"}:
        value = re.sub(r"<\s*(br|/p|/div|/li|/tr|/h[1-6])\s*[^>]*>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"<[^>]+>", "", value)
        value = html.unescape(value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    compact: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip():
            blank_count = 0
            compact.append(line)
        elif blank_count < 2:
            blank_count += 1
            compact.append("")
    return "\n".join(compact).strip()


class RemoteSourceError(RagBackendError):
    """A normalized error returned by a remote document provider."""

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_unavailable",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.category = str(category or "provider_unavailable")
        self.retryable = bool(retryable)
        merged_details = dict(details or {})
        merged_details.update({"category": self.category, "retryable": self.retryable})
        super().__init__(message, details=merged_details)
        self.code = self.category


@dataclass(frozen=True)
class RemoteReference:
    raw: str
    token: str
    resource_type: str


@dataclass(frozen=True)
class RemoteDocumentMetadata:
    provider: str
    object_token: str
    source_uri: str
    source_url: str = ""
    title: str = ""
    owner: str = ""
    remote_revision: str = ""
    remote_updated_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RemoteDocumentSnapshot:
    metadata: RemoteDocumentMetadata
    content: str
    content_format: str = "markdown"
    content_hash: str = ""

    def __post_init__(self) -> None:
        normalized = normalize_remote_content(self.content, self.content_format)
        if not normalized:
            raise RemoteSourceError(
                "Remote document contains no indexable text",
                category="content_invalid",
                retryable=False,
            )
        object.__setattr__(self, "content", normalized)
        object.__setattr__(self, "content_hash", content_hash(normalized))


class RemoteDocumentProvider(Protocol):
    provider_name: str

    def inspect(self, reference: str) -> RemoteDocumentMetadata:
        ...

    def fetch(self, metadata: RemoteDocumentMetadata) -> RemoteDocumentSnapshot:
        ...


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,}$")
_RESOURCE_PATTERN = re.compile(r"/(docx|doc|wiki)/([A-Za-z0-9][A-Za-z0-9_-]{2,})(?:/|$)", re.IGNORECASE)


def parse_remote_reference(reference: str) -> RemoteReference:
    raw = str(reference or "").strip()
    if not raw:
        raise RemoteSourceError("Document reference is required", category="invalid_reference")
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        match = _RESOURCE_PATTERN.search(parsed.path or "")
        if not match:
            raise RemoteSourceError(
                "Document URL does not contain a supported resource token",
                category="invalid_reference",
            )
        return RemoteReference(raw=raw, token=match.group(2), resource_type=match.group(1).lower())
    if not _TOKEN_PATTERN.fullmatch(raw):
        raise RemoteSourceError("Document token is invalid", category="invalid_reference")
    return RemoteReference(raw=raw, token=raw, resource_type="token")


def canonical_source_uri(provider: str, object_token: str) -> str:
    provider_name = str(provider or "remote").strip().lower()
    token = str(object_token or "").strip()
    if not provider_name or not token:
        raise RemoteSourceError("Remote source identity is incomplete", category="invalid_reference")
    return f"{provider_name}://document/{token}"


def _response_data(response: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(response, dict):
        return None, {}
    data = response.get("data")
    if not isinstance(data, dict):
        return None, {}
    nested = data.get("data")
    if isinstance(nested, dict):
        return data.get("code", response.get("code")), nested
    return response.get("code", data.get("code")), data


def _response_error(response: Any, *, operation: str) -> RemoteSourceError:
    code, data = _response_data(response)
    code_text = str(code or "").lower()
    if code_text in {"401", "unauthorized", "login_required"}:
        category, retryable = "auth_required", True
    elif code_text in {"403", "forbidden", "permission_denied"}:
        category, retryable = "permission_denied", False
    elif code_text in {"404", "not_found", "missing"}:
        category, retryable = "not_found", False
    elif code_text in {"429", "rate_limited", "too_many_requests"}:
        category, retryable = "rate_limited", True
    else:
        category, retryable = "provider_unavailable", True
    details = {"operation": operation}
    if data.get("title"):
        details["title"] = str(data["title"])
    return RemoteSourceError(
        f"Remote document provider failed during {operation}",
        category=category,
        retryable=retryable,
        details=details,
    )


def _default_session_loader(workspace_root: Path, session_file: str = "") -> list[dict[str, str]]:
    try:
        if session_file:
            path = Path(session_file)
            if not path.is_absolute():
                path = workspace_root / path
            payload = json.loads(path.resolve().read_text(encoding="utf-8")) or {}
        else:
            from backend.feishu_web_login import FeishuWebSessionStore

            payload = FeishuWebSessionStore(workspace_root).load() or {}
    except Exception as exc:
        raise RemoteSourceError(
            "Unable to load the configured Feishu session",
            category="auth_required",
            retryable=True,
        ) from exc
    session = str(payload.get("session") or "").strip()
    if not session:
        raise RemoteSourceError(
            "Feishu session is missing; complete the separate login flow first",
            category="auth_required",
            retryable=True,
        )
    return [{"name": "session", "value": session}]


def _default_reader() -> Any:
    project_root = Path(__file__).resolve().parents[2]
    skills_root = project_root / "skills"
    reader_roots = sorted(
        path
        for path in skills_root.iterdir()
        if path.is_dir() and (path / "lark_tools" / "commands" / "doc.py").is_file()
    ) if skills_root.is_dir() else []
    if not reader_roots:
        raise RemoteSourceError("Feishu document reader is not installed", category="provider_unavailable", retryable=False)
    skill_text = str(reader_roots[0])
    if skill_text not in sys.path:
        sys.path.insert(0, skill_text)
    try:
        return importlib.import_module("lark_tools.commands.doc")
    except Exception as exc:
        raise RemoteSourceError(
            "Unable to load the configured Feishu document reader",
            category="provider_unavailable",
            retryable=False,
        ) from exc


class FeishuDocumentProvider:
    provider_name = "feishu"

    def __init__(
        self,
        *,
        workspace_root: str | Path,
        reader: Any | None = None,
        session_loader: Callable[[], list[dict[str, str]]] | None = None,
        session_file: str = "",
        max_content_chars: int = 1_000_000,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.reader = reader or _default_reader()
        self.session_loader = session_loader or (
            lambda: _default_session_loader(self.workspace_root, session_file=session_file)
        )
        self.max_content_chars = max(1, int(max_content_chars))

    def _cookies(self) -> list[dict[str, str]]:
        try:
            cookies = self.session_loader()
        except RemoteSourceError:
            raise
        except Exception as exc:
            raise RemoteSourceError(
                "Unable to load the configured Feishu session",
                category="auth_required",
                retryable=True,
            ) from exc
        if not cookies:
            raise RemoteSourceError(
                "Feishu session is missing; complete the separate login flow first",
                category="auth_required",
                retryable=True,
            )
        return cookies

    def inspect(self, reference: str) -> RemoteDocumentMetadata:
        parsed = parse_remote_reference(reference)
        cookies = self._cookies()
        try:
            object_token = str(self.reader.resolve_doc_token(cookies, parsed.raw) or "").strip()
            if not object_token:
                raise RemoteSourceError("Document token could not be resolved", category="not_found", retryable=False)
            host = str(getattr(self.reader, "DOC_HOST", "") or "").strip()
            response = self.reader.http_get(
                cookies,
                host,
                f"/space/api/meta/?token={object_token}&type=22",
            )
            code, data = _response_data(response)
            if str(code) != "0":
                raise _response_error(response, operation="inspect")
            source_url = parsed.raw if urlparse(parsed.raw).scheme or urlparse(parsed.raw).netloc else ""
            if not source_url and host:
                source_url = f"https://{host}/docx/{object_token}"
            title = str(data.get("title") or object_token)
            owner = str(data.get("owner") or data.get("owner_id") or data.get("creator_id") or "")
            revision = str(data.get("revision") or data.get("revision_id") or data.get("version") or "")
            updated_at = str(data.get("updated_at") or data.get("update_time") or data.get("edit_time") or "")
            return RemoteDocumentMetadata(
                provider=self.provider_name,
                object_token=object_token,
                source_uri=canonical_source_uri(self.provider_name, object_token),
                source_url=source_url,
                title=title,
                owner=owner,
                remote_revision=revision,
                remote_updated_at=updated_at,
                extra={"resource_type": parsed.resource_type},
            )
        except RemoteSourceError:
            raise
        except Exception as exc:
            raise RemoteSourceError(
                "Unable to inspect the remote document",
                category="provider_unavailable",
                retryable=True,
            ) from exc

    def fetch(self, metadata: RemoteDocumentMetadata) -> RemoteDocumentSnapshot:
        cookies = self._cookies()
        try:
            result = self.reader.fetch_doc_blocks(cookies, metadata.object_token)
            if not isinstance(result, dict) or result.get("error"):
                raise _response_error(result, operation="fetch")
            content = normalize_remote_content(
                self.reader.doc_blocks_to_markdown(
                    result.get("sequence") or [],
                    result.get("blocks") or {},
                    cookies,
                ),
                "markdown",
            )
            if len(content) > self.max_content_chars or len(content.encode("utf-8")) > self.max_content_chars:
                raise RemoteSourceError(
                    "Remote document exceeds the configured content limit",
                    category="content_invalid",
                    retryable=False,
                    details={"max_content_chars": self.max_content_chars},
                )
            return RemoteDocumentSnapshot(
                metadata=metadata,
                content=content,
                content_format="markdown",
            )
        except RemoteSourceError:
            raise
        except Exception as exc:
            raise RemoteSourceError(
                "Unable to fetch the remote document",
                category="provider_unavailable",
                retryable=True,
            ) from exc
