from __future__ import annotations

import hashlib
import math
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from .chunking import tokenize

try:
    import torch
except Exception:  # pragma: no cover - exercised only when optional runtime dependency is absent.
    torch = None  # type: ignore[assignment]

try:
    from transformers import AutoModel, AutoModelForSequenceClassification, AutoTokenizer
except Exception:  # pragma: no cover - exercised only when optional runtime dependency is absent.
    AutoModel = None  # type: ignore[assignment]
    AutoModelForSequenceClassification = None  # type: ignore[assignment]
    AutoTokenizer = None  # type: ignore[assignment]

try:
    from qdrant_client import QdrantClient, models as qdrant_models
except Exception:  # pragma: no cover - exercised only when optional runtime dependency is absent.
    QdrantClient = None  # type: ignore[assignment]
    qdrant_models = None  # type: ignore[assignment]


class RagBackendError(RuntimeError):
    """Base error for a configured RAG provider or index."""

    code = "rag_backend_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
        }
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class BackendUnavailableError(RagBackendError):
    """Raised when a configured local model or backend cannot be used."""

    code = "backend_unavailable"


class VectorDimensionMismatchError(RagBackendError):
    """Raised when stored and configured vector dimensions disagree."""

    code = "vector_dimension_mismatch"


class EmbeddingProvider(Protocol):
    model_id: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class RerankerProvider(Protocol):
    model_id: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        ...


@dataclass(frozen=True)
class VectorRecord:
    chunk_id: str
    doc_id: str
    collection: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorSearchHit:
    chunk_id: str
    doc_id: str
    collection: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


def deterministic_point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"chat-agent-rag:{chunk_id}"))


def _resolve_device(device: str) -> str:
    if device and device.lower() != "auto":
        return device
    if torch is not None and torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _model_cache_path(config: Any, role: str, model_id: str) -> Path:
    return Path(config.model_cache_path) / role / model_id.replace("/", "__")


_CACHE_LOCK = threading.RLock()
_EMBEDDING_PROVIDER_CACHE: dict[tuple[Any, ...], EmbeddingProvider] = {}
_RERANKER_PROVIDER_CACHE: dict[tuple[Any, ...], RerankerProvider] = {}
_VECTOR_STORE_CACHE: dict[tuple[Any, ...], "QdrantVectorStore"] = {}


class LocalBgeEmbeddingProvider:
    provider_id = "huggingface_local"

    def __init__(
        self,
        *,
        model_path: str | Path,
        model_id: str,
        dimension: int,
        batch_size: int = 8,
        max_length: int = 8192,
        device: str = "auto",
    ) -> None:
        self.model_path = Path(model_path)
        self.model_id = model_id
        self.dimension = int(dimension)
        if self.dimension <= 0:
            raise VectorDimensionMismatchError("embedding dimension must be positive")
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(1, int(max_length))
        self.device = _resolve_device(device)
        self._tokenizer: Any | None = None
        self._model: Any | None = None

    def _ensure_loaded(self) -> tuple[Any, Any]:
        if self._tokenizer is not None and self._model is not None:
            return self._tokenizer, self._model
        if torch is None or AutoTokenizer is None or AutoModel is None:
            raise BackendUnavailableError(
                "BGE-M3 requires torch and transformers; install the configured local model runtime."
            )
        if not self.model_path.exists():
            raise BackendUnavailableError(f"BGE-M3 model cache does not exist: {self.model_path}")
        try:
            tokenizer = AutoTokenizer.from_pretrained(str(self.model_path), local_files_only=True)
            model = AutoModel.from_pretrained(str(self.model_path), local_files_only=True)
            model.to(self.device)
            model.eval()
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to load BGE-M3 model {self.model_id}: {exc}") from exc
        self._tokenizer = tokenizer
        self._model = model
        return tokenizer, model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        values = [str(text or "") for text in texts]
        if not values:
            return []
        tokenizer, model = self._ensure_loaded()
        vectors: list[list[float]] = []
        for start in range(0, len(values), self.batch_size):
            batch = values[start : start + self.batch_size]
            try:
                encoded = tokenizer(
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                encoded = {
                    key: value.to(self.device) if hasattr(value, "to") else value
                    for key, value in encoded.items()
                }
                with torch.no_grad():
                    output = model(**encoded)
                    hidden = output.last_hidden_state
                    mask = encoded["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
                    pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1e-9)
                    normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
                vectors.extend(normalized.detach().cpu().tolist())
            except Exception as exc:
                raise BackendUnavailableError(f"BGE-M3 embedding failed: {exc}") from exc
        if any(len(vector) != self.dimension for vector in vectors):
            observed = len(vectors[0]) if vectors else 0
            raise VectorDimensionMismatchError(
                f"BGE-M3 dimension mismatch: configured={self.dimension}, observed={observed}"
            )
        return vectors


class LocalBgeRerankerProvider:
    provider_id = "huggingface_local"

    def __init__(
        self,
        *,
        model_path: str | Path,
        model_id: str,
        batch_size: int = 8,
        max_length: int = 8192,
        device: str = "auto",
    ) -> None:
        self.model_path = Path(model_path)
        self.model_id = model_id
        self.batch_size = max(1, int(batch_size))
        self.max_length = max(1, int(max_length))
        self.device = _resolve_device(device)
        self._tokenizer: Any | None = None
        self._model: Any | None = None

    def _ensure_loaded(self) -> tuple[Any, Any]:
        if self._tokenizer is not None and self._model is not None:
            return self._tokenizer, self._model
        if torch is None or AutoTokenizer is None or AutoModelForSequenceClassification is None:
            raise BackendUnavailableError(
                "BGE reranking requires torch and transformers; install the configured local model runtime."
            )
        if not self.model_path.exists():
            raise BackendUnavailableError(f"BGE reranker model cache does not exist: {self.model_path}")
        try:
            tokenizer = AutoTokenizer.from_pretrained(str(self.model_path), local_files_only=True)
            model = AutoModelForSequenceClassification.from_pretrained(str(self.model_path), local_files_only=True)
            model.to(self.device)
            model.eval()
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to load BGE reranker {self.model_id}: {exc}") from exc
        self._tokenizer = tokenizer
        self._model = model
        return tokenizer, model

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        values = [str(text or "") for text in texts]
        if not values:
            return []
        tokenizer, model = self._ensure_loaded()
        scores: list[float] = []
        for start in range(0, len(values), self.batch_size):
            batch = values[start : start + self.batch_size]
            try:
                encoded = tokenizer(
                    [query] * len(batch),
                    batch,
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                    return_tensors="pt",
                )
                encoded = {
                    key: value.to(self.device) if hasattr(value, "to") else value
                    for key, value in encoded.items()
                }
                with torch.no_grad():
                    logits = model(**encoded).logits
                    if logits.ndim > 1 and logits.shape[-1] > 1:
                        logits = logits[:, -1]
                    scores.extend(torch.sigmoid(logits.reshape(-1)).detach().cpu().tolist())
            except Exception as exc:
                raise BackendUnavailableError(f"BGE reranker scoring failed: {exc}") from exc
        return [float(score) for score in scores]


class LexicalRerankerProvider:
    provider_id = "deterministic"
    model_id = "local_overlap"

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        query_terms = set(tokenize(query))
        if not query_terms:
            return [0.0 for _ in texts]
        return [
            len(query_terms.intersection(tokenize(text))) / len(query_terms)
            for text in texts
        ]


class QdrantVectorStore:
    backend_id = "qdrant"

    def __init__(
        self,
        *,
        path: str | Path,
        collection_name: str,
        vector_size: int,
        client: Any | None = None,
    ) -> None:
        if QdrantClient is None or qdrant_models is None:
            raise BackendUnavailableError("qdrant-client is required for the configured Qdrant vector backend.")
        self.path = Path(path)
        try:
            self.path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackendUnavailableError(f"Unable to create Qdrant storage path {self.path}: {exc}") from exc
        self.collection_name = collection_name
        self.vector_size = int(vector_size)
        if self.vector_size <= 0:
            raise VectorDimensionMismatchError("Qdrant vector size must be positive")
        try:
            self.client = client or QdrantClient(path=str(self.path))
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to open Qdrant storage at {self.path}: {exc}") from exc
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        try:
            exists = self.client.collection_exists(self.collection_name)
        except AttributeError:
            try:
                names = {item.name for item in self.client.get_collections().collections}
                exists = self.collection_name in names
            except Exception as exc:
                raise BackendUnavailableError(f"Unable to list Qdrant collections: {exc}") from exc
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to inspect Qdrant collection {self.collection_name}: {exc}") from exc
        if not exists:
            try:
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=qdrant_models.VectorParams(
                        size=self.vector_size,
                        distance=qdrant_models.Distance.COSINE,
                    ),
                )
            except Exception as exc:
                raise BackendUnavailableError(f"Unable to create Qdrant collection {self.collection_name}: {exc}") from exc
            return
        try:
            info = self.client.get_collection(self.collection_name)
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to inspect Qdrant collection {self.collection_name}: {exc}") from exc
        configured = getattr(getattr(info, "config", None), "params", None)
        vectors = getattr(configured, "vectors", None)
        observed = getattr(vectors, "size", None)
        if observed is None and isinstance(vectors, dict) and vectors:
            observed = getattr(next(iter(vectors.values())), "size", None)
        if observed is not None and int(observed) != self.vector_size:
            raise VectorDimensionMismatchError(
                f"Qdrant collection dimension mismatch: configured={self.vector_size}, observed={observed}"
            )

    @staticmethod
    def _payload(record: VectorRecord) -> dict[str, Any]:
        payload = dict(record.payload)
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            payload["metadata"] = {}
        payload.update(
            {
                "chunk_id": record.chunk_id,
                "doc_id": record.doc_id,
                "collection": record.collection,
            }
        )
        return payload

    @staticmethod
    def _filter(collection: str | None, filters: dict[str, Any] | None = None) -> Any | None:
        conditions = []
        if collection:
            conditions.append(
                qdrant_models.FieldCondition(
                    key="collection",
                    match=qdrant_models.MatchValue(value=collection),
                )
            )
        for key, value in (filters or {}).items():
            if value is None:
                continue
            normalized_key = str(key)
            if normalized_key.startswith("metadata."):
                payload_key = normalized_key
            else:
                payload_key = (
                    normalized_key
                    if normalized_key in {"chunk_id", "doc_id", "collection", "source_type", "title", "source_ref"}
                    else f"metadata.{normalized_key}"
                )
            matcher = (
                qdrant_models.MatchAny(any=list(value))
                if isinstance(value, (list, tuple, set))
                else qdrant_models.MatchValue(value=value)
            )
            conditions.append(qdrant_models.FieldCondition(key=payload_key, match=matcher))
        return qdrant_models.Filter(must=conditions) if conditions else None

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        points = []
        for record in records:
            if len(record.vector) != self.vector_size:
                raise VectorDimensionMismatchError(
                    f"Vector dimension mismatch: configured={self.vector_size}, observed={len(record.vector)}"
                )
            points.append(
                qdrant_models.PointStruct(
                    id=deterministic_point_id(record.chunk_id),
                    vector=record.vector,
                    payload=self._payload(record),
                )
            )
        try:
            self.client.upsert(collection_name=self.collection_name, points=points, wait=True)
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to upsert Qdrant vectors: {exc}") from exc

    def refresh(self, doc_id: str, records: Sequence[VectorRecord]) -> None:
        """Replace one document's points after all replacement vectors are ready."""
        existing_ids: set[str] = set()
        if doc_id:
            try:
                points, _ = self.client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=self._filter(None, {"doc_id": doc_id}),
                    limit=10000,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                raise BackendUnavailableError(f"Unable to inspect existing Qdrant vectors: {exc}") from exc
            for point in points:
                payload = getattr(point, "payload", None) or {}
                chunk_id = str(payload.get("chunk_id") or "")
                if chunk_id:
                    existing_ids.add(chunk_id)

        self.upsert(records)
        replacement_ids = {record.chunk_id for record in records}
        stale_ids = existing_ids - replacement_ids
        if stale_ids:
            self.delete_chunks(sorted(stale_ids))

    def document_point_ids(self, doc_id: str) -> list[str]:
        if not doc_id:
            return []
        try:
            points, _ = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._filter(None, {"doc_id": doc_id}),
                limit=10000,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to inspect Qdrant document vectors: {exc}") from exc
        return [
            str((getattr(point, "payload", None) or {}).get("chunk_id") or "")
            for point in points
            if (getattr(point, "payload", None) or {}).get("chunk_id")
        ]

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        collection: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        if len(query_vector) != self.vector_size:
            raise VectorDimensionMismatchError(
                f"Query vector dimension mismatch: configured={self.vector_size}, observed={len(query_vector)}"
            )
        try:
            response = self.client.query_points(
                collection_name=self.collection_name,
                query=list(query_vector),
                query_filter=self._filter(collection, filters),
                limit=max(0, int(limit)),
                with_payload=True,
            )
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to query Qdrant vectors: {exc}") from exc
        points = getattr(response, "points", response)
        results: list[VectorSearchHit] = []
        for point in points:
            payload = dict(getattr(point, "payload", None) or {})
            chunk_id = str(payload.get("chunk_id") or "")
            if not chunk_id:
                continue
            results.append(
                VectorSearchHit(
                    chunk_id=chunk_id,
                    doc_id=str(payload.get("doc_id") or ""),
                    collection=str(payload.get("collection") or ""),
                    score=float(getattr(point, "score", 0.0) or 0.0),
                    payload=payload,
                )
            )
        return results

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        ids = [deterministic_point_id(chunk_id) for chunk_id in chunk_ids if chunk_id]
        if ids:
            try:
                self.client.delete(
                    collection_name=self.collection_name,
                    points_selector=qdrant_models.PointIdsList(points=ids),
                    wait=True,
                )
            except Exception as exc:
                raise BackendUnavailableError(f"Unable to delete Qdrant chunk vectors: {exc}") from exc

    def delete_document(self, doc_id: str) -> None:
        if not doc_id:
            return
        query_filter = self._filter(None, {"doc_id": doc_id})
        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=qdrant_models.FilterSelector(filter=query_filter),
                wait=True,
            )
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to delete Qdrant document vectors: {exc}") from exc

    def document_exists(self, doc_id: str) -> bool:
        query_filter = self._filter(None, {"doc_id": doc_id})
        try:
            count = self.client.count(
                collection_name=self.collection_name,
                count_filter=query_filter,
                exact=True,
            )
        except Exception as exc:
            raise BackendUnavailableError(f"Unable to count Qdrant document vectors: {exc}") from exc
        return int(getattr(count, "count", 0)) > 0


def build_embedding_provider(config: Any) -> EmbeddingProvider | None:
    if config.vector_backend != "qdrant":
        return None
    if config.embedding_provider != "huggingface_local":
        raise BackendUnavailableError(
            f"Qdrant production retrieval requires embedding_provider=huggingface_local, got {config.embedding_provider}"
        )
    model_id = str(config.embedding_model or "").strip()
    if not model_id:
        raise BackendUnavailableError("embedding_model is required for the configured BGE-M3 stack")
    key = (
        str(_model_cache_path(config, "embedding", model_id).resolve()),
        model_id,
        int(config.embedding_dimension),
        int(config.embedding_batch_size),
        int(config.max_sequence_length),
        str(config.device),
    )
    with _CACHE_LOCK:
        provider = _EMBEDDING_PROVIDER_CACHE.get(key)
        if provider is None:
            provider = LocalBgeEmbeddingProvider(
                model_path=_model_cache_path(config, "embedding", model_id),
                model_id=model_id,
                dimension=int(config.embedding_dimension),
                batch_size=config.embedding_batch_size,
                max_length=config.max_sequence_length,
                device=config.device,
            )
            _EMBEDDING_PROVIDER_CACHE[key] = provider
        return provider


def build_vector_store(config: Any) -> QdrantVectorStore | None:
    if config.vector_backend != "qdrant":
        return None
    if int(config.embedding_dimension) <= 0:
        raise VectorDimensionMismatchError("embedding_dimension must be positive for Qdrant")
    key = (str(Path(config.qdrant_path).resolve()), str(config.qdrant_collection))
    with _CACHE_LOCK:
        store = _VECTOR_STORE_CACHE.get(key)
        if store is not None and store.vector_size != int(config.embedding_dimension):
            raise VectorDimensionMismatchError(
                "Qdrant vector dimension mismatch: "
                f"configured={int(config.embedding_dimension)}, observed={store.vector_size}"
            )
        if store is None:
            store = QdrantVectorStore(
                path=config.qdrant_path,
                collection_name=config.qdrant_collection,
                vector_size=int(config.embedding_dimension),
            )
            _VECTOR_STORE_CACHE[key] = store
        return store


def build_reranker_provider(config: Any) -> RerankerProvider | None:
    if not config.reranker.enabled:
        return None
    if config.reranker.provider == "local_overlap":
        return LexicalRerankerProvider()
    if config.reranker.provider != "huggingface_local":
        raise BackendUnavailableError(
            f"Unsupported reranker provider: {config.reranker.provider}"
        )
    model_id = str(config.reranker.model or "").strip()
    if not model_id:
        raise BackendUnavailableError("reranker_model is required when reranking is enabled")
    key = (
        str(_model_cache_path(config, "reranker", model_id).resolve()),
        model_id,
        int(config.reranker_batch_size),
        int(config.max_sequence_length),
        str(config.device),
    )
    with _CACHE_LOCK:
        provider = _RERANKER_PROVIDER_CACHE.get(key)
        if provider is None:
            provider = LocalBgeRerankerProvider(
                model_path=_model_cache_path(config, "reranker", model_id),
                model_id=model_id,
                batch_size=config.reranker_batch_size,
                max_length=config.max_sequence_length,
                device=config.device,
            )
            _RERANKER_PROVIDER_CACHE[key] = provider
        return provider
