from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

try:
    # The Windows FAISS wheel and PyTorch can ship different OpenMP runtimes.
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    import faiss
    import numpy as np
except Exception:  # pragma: no cover - exercised only when the optional accelerator is absent.
    faiss = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]


class FaissIndexError(RuntimeError):
    """Raised when the local FAISS accelerator cannot be opened or updated."""


@dataclass(frozen=True)
class FaissRecord:
    chunk_id: str
    doc_id: str
    collection: str
    vector: Sequence[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FaissSearchHit:
    chunk_id: str
    doc_id: str
    collection: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


class FaissVectorIndex:
    """Persistent exact inner-product index for normalized dense embeddings.

    Qdrant remains the durable vector source of truth. This index only stores a
    query accelerator plus enough payload metadata to apply the same filters.
    """

    metadata_version = 1

    def __init__(
        self,
        *,
        path: str | Path,
        vector_size: int,
        candidate_multiplier: int = 4,
    ) -> None:
        if faiss is None or np is None:
            raise FaissIndexError("FAISS acceleration requires the faiss and numpy packages.")
        self.path = Path(path)
        self.index_path = self.path / "index.faiss"
        self.metadata_path = self.path / "metadata.json"
        self.vector_size = int(vector_size)
        if self.vector_size <= 0:
            raise FaissIndexError("FAISS vector size must be positive")
        self.candidate_multiplier = max(1, int(candidate_multiplier))
        self._index: Any = None
        self._entries: dict[int, FaissRecord] = {}
        self._ids_by_chunk: dict[str, int] = {}
        self._next_id = 1
        self._needs_rebuild = False
        self._load()

    @property
    def ntotal(self) -> int:
        return int(self._index.ntotal)

    @property
    def needs_rebuild(self) -> bool:
        return self._needs_rebuild

    def _new_index(self) -> Any:
        return faiss.IndexIDMap2(faiss.IndexFlatIP(self.vector_size))

    def _load(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._index = self._new_index()
        if not self.index_path.exists() or not self.metadata_path.exists():
            self._needs_rebuild = True
            return
        try:
            index = faiss.read_index(str(self.index_path))
            data = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if int(getattr(index, "d", 0)) != self.vector_size:
                raise FaissIndexError(
                    f"FAISS index dimension mismatch: configured={self.vector_size}, observed={getattr(index, 'd', 0)}"
                )
            if not isinstance(data, dict) or int(data.get("version", 0)) != self.metadata_version:
                raise FaissIndexError("Unsupported FAISS metadata version")
            raw_entries = data.get("entries")
            if not isinstance(raw_entries, list) or int(index.ntotal) != len(raw_entries):
                raise FaissIndexError("FAISS index and metadata entry counts differ")
            entries: dict[int, FaissRecord] = {}
            ids_by_chunk: dict[str, int] = {}
            for item in raw_entries:
                if not isinstance(item, dict):
                    raise FaissIndexError("Malformed FAISS metadata entry")
                entry_id = int(item["id"])
                record = FaissRecord(
                    chunk_id=str(item["chunk_id"]),
                    doc_id=str(item.get("doc_id", "")),
                    collection=str(item.get("collection", "")),
                    vector=[],
                    payload=dict(item.get("payload") or {}),
                )
                if record.chunk_id in ids_by_chunk or entry_id in entries:
                    raise FaissIndexError("Duplicate FAISS metadata identifier")
                entries[entry_id] = record
                ids_by_chunk[record.chunk_id] = entry_id
            self._index = index
            self._entries = entries
            self._ids_by_chunk = ids_by_chunk
            self._next_id = max(entries, default=0) + 1
            self._needs_rebuild = False
        except Exception:
            self._index = self._new_index()
            self._entries = {}
            self._ids_by_chunk = {}
            self._next_id = 1
            self._needs_rebuild = True

    def _save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        index_tmp = self.index_path.with_suffix(".faiss.tmp")
        metadata_tmp = self.metadata_path.with_suffix(".json.tmp")
        try:
            faiss.write_index(self._index, str(index_tmp))
            metadata_tmp.write_text(
                json.dumps(
                    {
                        "version": self.metadata_version,
                        "vector_size": self.vector_size,
                        "entries": [
                            {
                                "id": entry_id,
                                "chunk_id": record.chunk_id,
                                "doc_id": record.doc_id,
                                "collection": record.collection,
                                "payload": record.payload,
                            }
                            for entry_id, record in sorted(self._entries.items())
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            index_tmp.replace(self.index_path)
            metadata_tmp.replace(self.metadata_path)
        except Exception as exc:
            for temporary_path in (index_tmp, metadata_tmp):
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise FaissIndexError(f"Unable to persist FAISS index at {self.path}: {exc}") from exc

    def _remove_ids(self, entry_ids: Sequence[int]) -> None:
        ids = sorted({int(entry_id) for entry_id in entry_ids if int(entry_id) in self._entries})
        if not ids:
            return
        self._index.remove_ids(np.asarray(ids, dtype="int64"))
        for entry_id in ids:
            record = self._entries.pop(entry_id, None)
            if record is not None:
                self._ids_by_chunk.pop(record.chunk_id, None)

    def _normalized_matrix(self, records: Sequence[FaissRecord]) -> Any:
        matrix = np.asarray([list(record.vector) for record in records], dtype="float32")
        if matrix.ndim != 2 or matrix.shape[1] != self.vector_size:
            observed = matrix.shape[1] if matrix.ndim == 2 and matrix.shape[1:] else 0
            raise FaissIndexError(
                f"FAISS vector dimension mismatch: configured={self.vector_size}, observed={observed}"
            )
        norms = np.linalg.norm(matrix, axis=1)
        if np.any(norms <= 0):
            raise FaissIndexError("FAISS cannot index zero-length vectors")
        faiss.normalize_L2(matrix)
        return matrix

    def _upsert(self, records: Sequence[FaissRecord], *, persist: bool) -> None:
        unique: dict[str, FaissRecord] = {}
        for record in records:
            if not record.chunk_id:
                continue
            unique[record.chunk_id] = record
        normalized_records = list(unique.values())
        if not normalized_records:
            if persist:
                self._save()
            return
        matrix = self._normalized_matrix(normalized_records)
        existing_ids = {
            record.chunk_id: self._ids_by_chunk[record.chunk_id]
            for record in normalized_records
            if record.chunk_id in self._ids_by_chunk
        }
        self._remove_ids(existing_ids.values())
        ids: list[int] = []
        for record in normalized_records:
            entry_id = existing_ids.get(record.chunk_id)
            if entry_id is None:
                entry_id = self._next_id
                self._next_id += 1
            ids.append(entry_id)
            self._entries[entry_id] = FaissRecord(
                chunk_id=record.chunk_id,
                doc_id=record.doc_id,
                collection=record.collection,
                vector=[],
                payload=dict(record.payload),
            )
            self._ids_by_chunk[record.chunk_id] = entry_id
        self._index.add_with_ids(matrix, np.asarray(ids, dtype="int64"))
        self._needs_rebuild = False
        if persist:
            self._save()

    def upsert(self, records: Sequence[FaissRecord]) -> None:
        self._upsert(records, persist=True)

    def rebuild(self, records: Sequence[FaissRecord]) -> None:
        self._index = self._new_index()
        self._entries = {}
        self._ids_by_chunk = {}
        self._next_id = 1
        self._needs_rebuild = False
        self._upsert(records, persist=False)
        self._save()

    @staticmethod
    def _matches_value(actual: Any, expected: Any) -> bool:
        if isinstance(expected, (list, tuple, set)):
            expected_values = set(expected)
            if isinstance(actual, (list, tuple, set)):
                return bool(expected_values.intersection(actual))
            return actual in expected_values
        if isinstance(actual, (list, tuple, set)):
            return expected in actual
        return actual == expected

    @classmethod
    def _matches(
        cls,
        record: FaissRecord,
        *,
        collection: str | None,
        filters: dict[str, Any] | None,
    ) -> bool:
        if collection and record.collection != collection:
            return False
        payload = record.payload
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        direct_fields = {
            "chunk_id": record.chunk_id,
            "doc_id": record.doc_id,
            "collection": record.collection,
            "source_type": payload.get("source_type"),
            "title": payload.get("title"),
            "source_ref": payload.get("source_ref"),
        }
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            normalized_key = str(key)
            if normalized_key.startswith("metadata."):
                actual = metadata.get(normalized_key[9:])
            elif normalized_key in direct_fields:
                actual = direct_fields[normalized_key]
            else:
                actual = metadata.get(normalized_key)
            if not cls._matches_value(actual, expected):
                return False
        return True

    def _search_once(
        self,
        query: Any,
        *,
        limit: int,
        collection: str | None,
        filters: dict[str, Any] | None,
    ) -> list[FaissSearchHit]:
        distances, labels = self._index.search(query, limit)
        hits: list[FaissSearchHit] = []
        for distance, label in zip(distances[0], labels[0]):
            entry_id = int(label)
            record = self._entries.get(entry_id)
            if record is None or not self._matches(record, collection=collection, filters=filters):
                continue
            hits.append(
                FaissSearchHit(
                    chunk_id=record.chunk_id,
                    doc_id=record.doc_id,
                    collection=record.collection,
                    score=float(distance),
                    payload=dict(record.payload),
                )
            )
            if len(hits) >= limit:
                break
        return hits

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        collection: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[FaissSearchHit]:
        requested = max(0, int(limit))
        if requested == 0 or self.ntotal == 0:
            return []
        query = self._normalized_matrix([FaissRecord("query", "", "", query_vector)])
        constrained = bool(collection or filters)
        search_limit = min(
            self.ntotal,
            max(requested, requested * self.candidate_multiplier if constrained else requested),
        )
        hits = self._search_once(
            query,
            limit=search_limit,
            collection=collection,
            filters=filters,
        )
        if len(hits) < requested and search_limit < self.ntotal:
            hits = self._search_once(
                query,
                limit=self.ntotal,
                collection=collection,
                filters=filters,
            )
        return hits[:requested]

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        self._remove_ids(
            self._ids_by_chunk[chunk_id]
            for chunk_id in chunk_ids
            if chunk_id in self._ids_by_chunk
        )
        self._save()

    def delete_document(self, doc_id: str) -> None:
        self._remove_ids(
            entry_id
            for entry_id, record in self._entries.items()
            if record.doc_id == doc_id
        )
        self._save()
