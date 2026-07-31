from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import re
import shutil
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .chunking import StructureFirstSemanticChunker, content_hash, tokenize
from .config import SemanticChunkingConfig, load_rag_config
from .models import DocumentManifest, KnowledgeChunk

_SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".pdf"}
_PARSER_VERSION = "local-parser-v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _doc_id(collection: str, source_uri: str) -> str:
    digest = hashlib.sha256(f"{collection}:{source_uri}".encode("utf-8")).hexdigest()[:16]
    return f"doc-{digest}"


def _safe_component(value: str, default: str = "default") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    return cleaned or default


def _safe_filename(value: str, default: str = "document") -> str:
    name = Path(value or default).name
    suffix = Path(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    safe_stem = _safe_component(stem, default)
    safe_suffix = suffix.lower() if re.fullmatch(r"\.[A-Za-z0-9]+", suffix or "") else ""
    return f"{safe_stem}{safe_suffix}"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _query_terms(query: str) -> list[str]:
    terms = {token for token in tokenize(query) if len(token) >= 2}
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", query or ""):
        value = match.group(0)
        max_size = min(12, len(value))
        for size in range(max_size, 1, -1):
            for start in range(0, len(value) - size + 1):
                terms.add(value[start:start + size])
    return sorted(terms, key=lambda value: (-len(value), value))


def _find_term_index(text: str, term: str) -> int:
    direct = text.lower().find(term.lower())
    if direct >= 0:
        return direct
    if not re.search(r"[\u4e00-\u9fff]", term):
        return -1
    compact_chars: list[str] = []
    original_indexes: list[int] = []
    for index, char in enumerate(text):
        if char.isspace():
            continue
        compact_chars.append(char)
        original_indexes.append(index)
    compact_index = "".join(compact_chars).lower().find(re.sub(r"\s+", "", term).lower())
    if compact_index < 0 or compact_index >= len(original_indexes):
        return -1
    return original_indexes[compact_index]


def _safe_snippet(text: str, query: str = "", limit: int = 360) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if len(normalized) <= limit:
        return normalized
    match_index = -1
    for term in _query_terms(query):
        match_index = _find_term_index(normalized, term)
        if match_index >= 0:
            break
    if match_index < 0:
        return normalized[: max(0, limit - 3)].rstrip() + "..."
    start = max(0, match_index - limit // 2)
    end = min(len(normalized), start + max(0, limit - 3))
    start = max(0, end - max(0, limit - 3))
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(normalized) else ""
    return f"{prefix}{normalized[start:end].strip()}{suffix}"


def _safe_chunk_excerpt(text: str, query: str = "", limit: int = 1200) -> str:
    return _safe_snippet(text, query=query, limit=limit)


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _vectorize(text: str) -> dict[str, float]:
    vector: dict[str, float] = {}
    for token in tokenize(text):
        vector[token] = vector.get(token, 0.0) + 1.0
    return vector


class PersonalKnowledgeBase:
    def __init__(
        self,
        *,
        workspace_root: str | Path,
        store_path: str | Path,
        chunker_config: SemanticChunkingConfig | None = None,
        reranker_enabled: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.store_path = Path(store_path).resolve()
        self.documents_path = self.store_path / "documents"
        self.index_path = self.store_path / "index"
        self.manifests_path = self.store_path / "manifests"
        self.sync_reports_path = self.store_path / "reports"
        for path in (self.store_path, self.documents_path, self.index_path, self.manifests_path, self.sync_reports_path):
            path.mkdir(parents=True, exist_ok=True)
        self.chunker_config = chunker_config or SemanticChunkingConfig()
        self.chunker = StructureFirstSemanticChunker(self.chunker_config)
        self.reranker_enabled = bool(reranker_enabled)
        self.manifests: dict[str, DocumentManifest] = {}
        self.chunks_by_doc: dict[str, list[KnowledgeChunk]] = {}
        self._load()

    @classmethod
    def from_config(cls, *, workspace_root: str | Path, config_overrides: dict[str, Any] | None = None) -> "PersonalKnowledgeBase":
        config = load_rag_config(config_overrides)
        return cls(
            workspace_root=workspace_root,
            store_path=config.knowledge_store_path,
            chunker_config=config.chunking,
            reranker_enabled=config.reranker.enabled,
        )

    def _manifest_path(self) -> Path:
        return self.manifests_path / "manifests.json"

    def _chunks_path(self) -> Path:
        return self.index_path / "chunks.json"

    def _load(self) -> None:
        manifest_path = self._manifest_path()
        legacy_manifest_path = self.store_path / "manifests.json"
        if not manifest_path.exists() and legacy_manifest_path.exists():
            manifest_path = legacy_manifest_path
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.manifests = {
                doc_id: DocumentManifest(**item)
                for doc_id, item in data.items()
                if isinstance(item, dict)
            }
        chunks_path = self._chunks_path()
        legacy_chunks_path = self.store_path / "chunks.json"
        if not chunks_path.exists() and legacy_chunks_path.exists():
            chunks_path = legacy_chunks_path
        if chunks_path.exists():
            data = json.loads(chunks_path.read_text(encoding="utf-8"))
            self.chunks_by_doc = {
                doc_id: [KnowledgeChunk(**chunk) for chunk in chunks if isinstance(chunk, dict)]
                for doc_id, chunks in data.items()
                if isinstance(chunks, list)
            }

    def _save(self) -> None:
        for path in (self.store_path, self.documents_path, self.index_path, self.manifests_path, self.sync_reports_path):
            path.mkdir(parents=True, exist_ok=True)
        self._manifest_path().write_text(
            json.dumps({key: value.to_dict() for key, value in self.manifests.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._chunks_path().write_text(
            json.dumps(
                {key: [chunk.to_dict() for chunk in chunks] for key, chunks in self.chunks_by_doc.items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _validate_source(self, path: str | Path) -> tuple[Path | None, str]:
        candidate = Path(path).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (self.workspace_root / candidate).resolve()
        try:
            resolved.relative_to(self.workspace_root)
        except ValueError:
            return resolved, "outside allowed workspace roots"
        if not resolved.exists() or not resolved.is_file():
            return resolved, "file not found"
        if resolved.suffix.lower() not in _SUPPORTED_SUFFIXES:
            return resolved, "unsupported file type"
        return resolved, ""

    def _collection_from_document_path(self, path: Path) -> str:
        try:
            relative = path.resolve().relative_to(self.documents_path.resolve())
        except ValueError:
            return "default"
        if len(relative.parts) > 1:
            return _safe_component(relative.parts[0])
        return "default"

    def _target_import_path(self, collection: str, filename: str) -> Path:
        collection_dir = self.documents_path / _safe_component(collection)
        collection_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_filename(filename, "document")
        return collection_dir / safe_name

    def _read_supported_file(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except Exception as exc:
                raise RuntimeError("PDF text extraction requires pypdf") from exc
            with path.open("rb") as handle:
                reader = PdfReader(handle)
                pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(page.strip() for page in pages if page.strip()).strip()
            if not text:
                raise RuntimeError("PDF text extraction produced no text")
            return text
        if suffix == ".csv":
            with path.open("r", encoding="utf-8", newline="", errors="ignore") as handle:
                rows = list(csv.reader(handle))
            return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        raw = path.read_text(encoding="utf-8", errors="ignore")
        if suffix in {".html", ".htm"}:
            return BeautifulSoup(raw, "html.parser").get_text("\n")
        return html.unescape(raw)

    def index_files(
        self,
        collection: str,
        paths: list[str | Path],
        *,
        refresh: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        indexed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        normalized_collection = _safe_component(collection)
        for path in paths:
            resolved, reason = self._validate_source(path)
            if reason:
                skipped.append({"path": str(resolved or path), "reason": reason})
                continue
            assert resolved is not None
            source_uri = str(resolved)
            text = self._read_supported_file(resolved)
            digest = content_hash(text)
            doc_id = _doc_id(normalized_collection, source_uri)
            existing = self.manifests.get(doc_id)
            chunk_signature = self.chunker_config.signature()
            needs_refresh = (
                refresh
                or existing is None
                or existing.content_hash != digest
                or existing.chunking_signature != chunk_signature
            )
            if not needs_refresh:
                indexed.append({"doc_id": doc_id, "source_uri": source_uri, "refreshed": False, "chunk_count": existing.chunk_count})
                continue
            chunks = self.chunker.chunk_text(
                text,
                source_ref=resolved.name,
                doc_id=doc_id,
                collection=normalized_collection,
            )
            manifest = DocumentManifest(
                doc_id=doc_id,
                collection=normalized_collection,
                source_uri=source_uri,
                source_type=resolved.suffix.lower().lstrip("."),
                content_hash=digest,
                parser_version=_PARSER_VERSION,
                chunker_version=self.chunker_config.chunker_version,
                chunking_signature=chunk_signature,
                title=resolved.name,
                status="indexed",
                chunk_count=len(chunks),
                metadata=dict(metadata or {}),
            )
            self.manifests[doc_id] = manifest
            self.chunks_by_doc[doc_id] = chunks
            indexed.append({"doc_id": doc_id, "source_uri": source_uri, "refreshed": existing is not None, "chunk_count": len(chunks)})
        self._save()
        return {"collection": normalized_collection, "indexed": indexed, "skipped": skipped}

    def import_files(
        self,
        collection: str,
        paths: list[str | Path],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del metadata
        normalized_collection = _safe_component(collection)
        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for path in paths:
            resolved, reason = self._validate_source(path)
            if reason:
                skipped.append({"path": str(resolved or path), "reason": reason})
                continue
            assert resolved is not None
            if _is_relative_to(resolved, self.documents_path):
                target = resolved
            else:
                target = self._target_import_path(normalized_collection, resolved.name)
                if resolved.resolve() != target.resolve():
                    shutil.copy2(resolved, target)
            imported.append(
                {
                    "collection": normalized_collection,
                    "source_uri": str(resolved),
                    "knowledge_path": str(target),
                    "title": target.name,
                    "status": "pending_sync",
                }
            )
        return {
            "collection": normalized_collection,
            "imported": imported,
            "skipped": skipped,
            "counts": {
                "imported": len(imported),
                "skipped": len(skipped),
            },
        }

    def import_file_bytes(
        self,
        collection: str,
        filename: str,
        content: bytes,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del metadata
        normalized_collection = _safe_component(collection)
        target = self._target_import_path(normalized_collection, filename)
        if target.suffix.lower() not in _SUPPORTED_SUFFIXES:
            return {
                "collection": normalized_collection,
                "imported": [],
                "skipped": [{"path": str(target), "reason": "unsupported file type"}],
                "counts": {"imported": 0, "skipped": 1},
            }
        target.write_bytes(content)
        return {
            "collection": normalized_collection,
            "imported": [
                {
                    "collection": normalized_collection,
                    "source_uri": str(target),
                    "knowledge_path": str(target),
                    "title": target.name,
                    "status": "pending_sync",
                }
            ],
            "skipped": [],
            "counts": {"imported": 1, "skipped": 0},
        }

    def sync(self, collection: str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
        normalized_collection = _safe_component(collection) if collection else None
        counts = {
            "indexed": 0,
            "refreshed": 0,
            "unchanged": 0,
            "skipped": 0,
            "failed": 0,
            "deleted": 0,
        }
        files: list[dict[str, Any]] = []
        supported_paths: set[str] = set()
        scan_root = self.documents_path / normalized_collection if normalized_collection else self.documents_path
        candidates = sorted(scan_root.rglob("*")) if scan_root.exists() else []
        chunk_signature = self.chunker_config.signature()

        for path in candidates:
            if not path.is_file():
                continue
            inferred_collection = self._collection_from_document_path(path)
            if normalized_collection and inferred_collection != normalized_collection:
                continue
            if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
                counts["skipped"] += 1
                files.append({"path": str(path), "collection": inferred_collection, "status": "skipped", "reason": "unsupported file type"})
                continue
            source_uri = str(path.resolve())
            supported_paths.add(source_uri)
            doc_id = _doc_id(inferred_collection, source_uri)
            existing = self.manifests.get(doc_id)
            try:
                text = self._read_supported_file(path)
                digest = content_hash(text)
                needs_index = (
                    existing is None
                    or existing.content_hash != digest
                    or existing.chunking_signature != chunk_signature
                )
                if not needs_index:
                    counts["unchanged"] += 1
                    files.append({"path": source_uri, "collection": inferred_collection, "doc_id": doc_id, "status": "unchanged"})
                    continue
                if dry_run:
                    status = "would_index" if existing is None else "would_refresh"
                    files.append({"path": source_uri, "collection": inferred_collection, "doc_id": doc_id, "status": status})
                    continue
                result = self.index_files(inferred_collection, [path], refresh=True)
                if result["indexed"]:
                    item = result["indexed"][0]
                    if item.get("refreshed"):
                        counts["refreshed"] += 1
                        status = "refreshed"
                    else:
                        counts["indexed"] += 1
                        status = "indexed"
                    files.append({"path": source_uri, "collection": inferred_collection, "doc_id": item["doc_id"], "status": status})
                for skipped in result.get("skipped", []):
                    counts["skipped"] += 1
                    files.append({**skipped, "collection": inferred_collection, "status": "skipped"})
            except Exception as exc:
                counts["failed"] += 1
                files.append({"path": source_uri, "collection": inferred_collection, "doc_id": doc_id, "status": "failed", "reason": str(exc)})

        deleted: list[str] = []
        for doc_id, manifest in list(self.manifests.items()):
            source_path = Path(manifest.source_uri)
            if normalized_collection and manifest.collection != normalized_collection:
                continue
            if not _is_relative_to(source_path, self.documents_path):
                continue
            if str(source_path.resolve()) in supported_paths:
                continue
            deleted.append(doc_id)

        if not dry_run:
            for doc_id in deleted:
                self.delete_document(doc_id=doc_id)
        counts["deleted"] = len(deleted)
        for doc_id in deleted:
            files.append({"doc_id": doc_id, "status": "deleted"})

        report = {
            "collection": normalized_collection,
            "dry_run": dry_run,
            "counts": counts,
            "files": files,
            "store_path": str(self.store_path),
        }
        if not dry_run:
            report_path = self.sync_reports_path / f"sync-{int(time.time() * 1000)}.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            report["report_path"] = str(report_path)
        return report

    def list_collections(self) -> list[dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for manifest in self.manifests.values():
            entry = totals.setdefault(manifest.collection, {"collection": manifest.collection, "document_count": 0, "chunk_count": 0})
            entry["document_count"] += 1
            entry["chunk_count"] += manifest.chunk_count
        return sorted(totals.values(), key=lambda item: item["collection"])

    def list_documents(self, collection: str | None = None) -> list[dict[str, Any]]:
        return [
            manifest.to_dict()
            for manifest in sorted(self.manifests.values(), key=lambda item: item.source_uri)
            if collection is None or manifest.collection == collection
        ]

    def list_source_files(self, collection: str | None = None) -> list[dict[str, Any]]:
        normalized_collection = _safe_component(collection) if collection else None
        scan_root = self.documents_path / normalized_collection if normalized_collection else self.documents_path
        if not scan_root.exists():
            return []
        by_source = {manifest.source_uri: manifest for manifest in self.manifests.values()}
        sources: list[dict[str, Any]] = []
        for path in sorted(scan_root.rglob("*")):
            if not path.is_file():
                continue
            inferred_collection = self._collection_from_document_path(path)
            if normalized_collection and inferred_collection != normalized_collection:
                continue
            source_uri = str(path.resolve())
            suffix = path.suffix.lower()
            manifest = by_source.get(source_uri)
            status = "pending_sync"
            reason = ""
            chunk_count = 0
            doc_id = ""
            source_type = suffix.lstrip(".")
            latest_error = ""
            if suffix not in _SUPPORTED_SUFFIXES:
                status = "unsupported"
                reason = "unsupported file type"
            elif manifest is not None:
                doc_id = manifest.doc_id
                chunk_count = manifest.chunk_count
                source_type = manifest.source_type
                latest_error = manifest.latest_error
                try:
                    status = "indexed" if content_hash(self._read_supported_file(path)) == manifest.content_hash else "pending_sync"
                except Exception as exc:
                    status = "failed"
                    reason = str(exc)
            sources.append(
                {
                    "collection": inferred_collection,
                    "doc_id": doc_id,
                    "source_uri": source_uri,
                    "title": path.name,
                    "source_type": source_type,
                    "status": status,
                    "chunk_count": chunk_count,
                    "reason": reason or latest_error,
                }
            )
        return sources

    def get_document_detail(self, doc_id: str) -> dict[str, Any] | None:
        manifest = self.manifests.get(doc_id)
        if manifest is None:
            return None
        return {
            "document": manifest.to_dict(),
            "chunks": [chunk.to_dict() for chunk in self.chunks_by_doc.get(doc_id, [])],
        }

    def list_chunks(self, doc_id: str) -> list[dict[str, Any]]:
        return [chunk.to_dict() for chunk in self.chunks_by_doc.get(doc_id, [])]

    def delete_source_file(self, source_uri: str) -> dict[str, Any]:
        source_path = Path(source_uri)
        if not source_path.is_absolute():
            source_path = self.workspace_root / source_path
        resolved = source_path.resolve()
        if not _is_relative_to(resolved, self.documents_path):
            return {"deleted": [], "removed_sources": [], "error": "source is outside knowledge documents"}
        if not resolved.exists() or not resolved.is_file():
            return {"deleted": [], "removed_sources": [], "error": "source file not found"}

        deleted: list[str] = []
        for manifest_id, manifest in list(self.manifests.items()):
            if Path(manifest.source_uri).resolve() != resolved:
                continue
            self.manifests.pop(manifest_id, None)
            self.chunks_by_doc.pop(manifest_id, None)
            deleted.append(manifest_id)

        resolved.unlink()
        self._save()
        return {"deleted": deleted, "removed_sources": [str(resolved)]}

    def delete_document(
        self,
        *,
        doc_id: str | None = None,
        collection: str | None = None,
        source_uri: str | None = None,
        remove_source: bool = False,
    ) -> dict[str, Any]:
        to_delete: list[str] = []
        for manifest_id, manifest in self.manifests.items():
            if doc_id and manifest_id != doc_id:
                continue
            if collection and manifest.collection != collection:
                continue
            if source_uri and manifest.source_uri != str(Path(source_uri).resolve()):
                continue
            to_delete.append(manifest_id)
        removed_sources: list[str] = []
        for manifest_id in to_delete:
            manifest = self.manifests.pop(manifest_id, None)
            self.chunks_by_doc.pop(manifest_id, None)
            if remove_source and manifest:
                source_path = Path(manifest.source_uri)
                if _is_relative_to(source_path, self.documents_path):
                    source_path.unlink(missing_ok=True)
                    removed_sources.append(str(source_path))
        self._save()
        return {"deleted": to_delete, "removed_sources": removed_sources}

    def _candidate_chunks(self, collection: str | None) -> list[KnowledgeChunk]:
        chunks: list[KnowledgeChunk] = []
        for doc_id, doc_chunks in self.chunks_by_doc.items():
            manifest = self.manifests.get(doc_id)
            if manifest is None:
                continue
            if collection and manifest.collection != collection:
                continue
            chunks.extend(doc_chunks)
        return chunks

    def _lexical_rank(self, query_tokens: list[str], chunks: list[KnowledgeChunk]) -> list[tuple[KnowledgeChunk, float]]:
        if not query_tokens:
            return []
        scored: list[tuple[KnowledgeChunk, float]] = []
        for chunk in chunks:
            tokens = tokenize(chunk.text)
            if not tokens:
                continue
            token_set = set(tokens)
            overlap = sum(1 for token in query_tokens if token in token_set)
            if overlap:
                scored.append((chunk, overlap / max(1, len(set(query_tokens)))))
        return sorted(scored, key=lambda item: (-item[1], item[0].chunk_id))

    def _dense_rank(self, query: str, chunks: list[KnowledgeChunk]) -> list[tuple[KnowledgeChunk, float]]:
        query_vector = _vectorize(query)
        scored = [(chunk, _cosine(query_vector, _vectorize(chunk.text))) for chunk in chunks]
        return [(chunk, score) for chunk, score in sorted(scored, key=lambda item: (-item[1], item[0].chunk_id)) if score > 0]

    def _rrf(self, ranked_lists: list[list[tuple[KnowledgeChunk, float]]], k: int = 60) -> dict[str, float]:
        scores: dict[str, float] = {}
        for ranked in ranked_lists:
            for rank, (chunk, _) in enumerate(ranked, 1):
                scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
        return scores

    def _expand_with_adjacent_chunks(
        self,
        ranked: list[tuple[KnowledgeChunk, float]],
        chunks: list[KnowledgeChunk],
    ) -> tuple[list[tuple[KnowledgeChunk, float]], dict[str, str]]:
        by_position = {(chunk.doc_id, chunk.ordinal): chunk for chunk in chunks}
        expanded: list[tuple[KnowledgeChunk, float]] = []
        included: set[str] = set()
        adjacent_to: dict[str, str] = {}

        def add(chunk: KnowledgeChunk, score: float, source_chunk_id: str = "") -> None:
            if chunk.chunk_id in included:
                return
            included.add(chunk.chunk_id)
            if source_chunk_id:
                adjacent_to[chunk.chunk_id] = source_chunk_id
            expanded.append((chunk, score))

        for chunk, score in ranked:
            add(chunk, score)
            for ordinal in (chunk.ordinal - 1, chunk.ordinal + 1):
                neighbor = by_position.get((chunk.doc_id, ordinal))
                if neighbor is not None:
                    add(neighbor, score * 0.999, chunk.chunk_id)
        return expanded, adjacent_to

    def search(
        self,
        query: str,
        *,
        collection: str | None = None,
        filters: dict[str, Any] | None = None,
        top_k: int = 8,
        include_scores: bool = True,
    ) -> dict[str, Any]:
        del filters
        chunks = self._candidate_chunks(collection)
        query_tokens = tokenize(query)
        lexical = self._lexical_rank(query_tokens, chunks)
        dense = self._dense_rank(query, chunks)
        rrf_scores = self._rrf([lexical, dense])
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        lexical_scores = {chunk.chunk_id: score for chunk, score in lexical}
        dense_scores = {chunk.chunk_id: score for chunk, score in dense}
        fused = [(by_id[chunk_id], score) for chunk_id, score in rrf_scores.items()]
        if self.reranker_enabled:
            fused = [
                (chunk, score + lexical_scores.get(chunk.chunk_id, 0.0) * 0.1)
                for chunk, score in fused
            ]
        fused.sort(key=lambda item: (-item[1], item[0].chunk_id))
        fused, adjacent_to = self._expand_with_adjacent_chunks(fused, chunks)
        results = []
        for chunk, fused_score in fused[: max(0, int(top_k))]:
            score_payload = {
                "fusion_score": round(fused_score, 6),
                "lexical_score": round(lexical_scores.get(chunk.chunk_id, 0.0), 6),
                "dense_score": round(dense_scores.get(chunk.chunk_id, 0.0), 6),
            }
            if self.reranker_enabled:
                score_payload["reranker_score"] = round(score_payload["lexical_score"], 6)
            item = {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "collection": chunk.collection,
                "citation_id": chunk.chunk_id,
                "source_ref": chunk.source_ref,
                "source_uri": self.manifests.get(chunk.doc_id).source_uri if self.manifests.get(chunk.doc_id) else "",
                "source_type": self.manifests.get(chunk.doc_id).source_type if self.manifests.get(chunk.doc_id) else "",
                "title": self.manifests.get(chunk.doc_id).title if self.manifests.get(chunk.doc_id) else chunk.source_ref,
                "heading_path": chunk.heading_path,
                "snippet": _safe_snippet(chunk.text, query=query),
                "excerpt": _safe_chunk_excerpt(chunk.text, query=query),
                "metadata": {
                    "chunking_strategy": chunk.chunking_strategy,
                    "boundary_method": chunk.boundary_method,
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "adjacent_context": chunk.chunk_id in adjacent_to,
                    "adjacent_to": adjacent_to.get(chunk.chunk_id, ""),
                },
            }
            if include_scores:
                item["scores"] = score_payload
            results.append(item)
        return {
            "query": query,
            "collection": collection,
            "results": results,
            "settings": {
                "fusion_strategy": "rrf",
                "reranker_enabled": self.reranker_enabled,
                "top_k": top_k,
                "adjacent_chunk_expansion": True,
            },
        }
