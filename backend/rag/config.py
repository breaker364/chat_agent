from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import get_runtime_value, resolve_runtime_path

_HF_MODEL_ALIASES = {
    "bge-m3": "BAAI/bge-m3",
    "bge-reranker-v2-m3": "BAAI/bge-reranker-v2-m3",
}


def normalize_huggingface_model_id(value: Any, default: str = "") -> str:
    raw = str(value or default).strip()
    if not raw:
        return ""
    return _HF_MODEL_ALIASES.get(raw, raw)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class SemanticChunkingConfig:
    target_tokens: int = 500
    max_tokens: int = 800
    overlap_ratio: float = 0.12
    semantic_boundary_threshold: float = 0.72
    chunker_version: str = "structure-first-semantic-v1"

    def signature(self) -> str:
        return (
            f"{self.chunker_version}:target={self.target_tokens}:max={self.max_tokens}:"
            f"overlap={self.overlap_ratio:.3f}:semantic={self.semantic_boundary_threshold:.3f}"
        )


@dataclass
class HybridRetrievalConfig:
    fusion_strategy: str = "rrf"
    lexical_candidate_depth: int = 30
    dense_candidate_depth: int = 30
    rrf_k: int = 60
    top_k: int = 8


@dataclass
class RerankerConfig:
    enabled: bool = False
    provider: str = "local_overlap"
    model: str = ""
    candidate_top_k: int = 30


@dataclass
class Bm25Config:
    k1: float = 1.2
    b: float = 0.75


@dataclass
class RemoteSourceConfig:
    enabled: bool = True
    provider: str = "feishu"
    session_mode: str = "existing_session_store"
    session_file: str = ""
    max_content_chars: int = 1_000_000
    max_retries: int = 1
    retry_backoff_seconds: float = 0.2


@dataclass
class RagConfig:
    enabled: bool = False
    retrieval_profile: str = "deterministic"
    knowledge_store_path: Path = field(default_factory=lambda: resolve_runtime_path(None, "knowledge_base"))
    evaluation_cache_path: Path = field(default_factory=lambda: resolve_runtime_path(None, "tmp/rag_eval_cache"))
    report_dir: Path = field(default_factory=lambda: resolve_runtime_path(None, "tmp/rag_eval_reports"))
    embedding_provider: str = "local_hashing"
    vector_backend: str = "in_memory"
    sparse_backend: str = "in_memory_bm25"
    embedding_model: str = ""
    embedding_dimension: int = 0
    model_cache_path: Path = field(default_factory=lambda: resolve_runtime_path(None, "knowledge_base/models"))
    qdrant_collection: str = "knowledge_chunks"
    faiss_enabled: bool = False
    faiss_candidate_multiplier: int = 4
    embedding_batch_size: int = 8
    reranker_batch_size: int = 8
    max_sequence_length: int = 8192
    device: str = "auto"
    chunking: SemanticChunkingConfig = field(default_factory=SemanticChunkingConfig)
    hybrid: HybridRetrievalConfig = field(default_factory=HybridRetrievalConfig)
    bm25: Bm25Config = field(default_factory=Bm25Config)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    remote_source: RemoteSourceConfig = field(default_factory=RemoteSourceConfig)

    @property
    def documents_path(self) -> Path:
        return self.knowledge_store_path / "documents"

    @property
    def index_path(self) -> Path:
        return self.knowledge_store_path / "index"

    @property
    def manifests_path(self) -> Path:
        return self.knowledge_store_path / "manifests"

    @property
    def sync_reports_path(self) -> Path:
        return self.knowledge_store_path / "reports"

    @property
    def qdrant_path(self) -> Path:
        return self.knowledge_store_path / "qdrant"

    @property
    def faiss_path(self) -> Path:
        return self.knowledge_store_path / "faiss"

    def retrieval_signature(self) -> str:
        return (
            f"profile={self.retrieval_profile};"
            f"embedding={self.embedding_provider}:{self.embedding_model}:dim={self.embedding_dimension};"
            f"vector={self.vector_backend}:{self.qdrant_collection};"
            f"sparse={self.sparse_backend};fusion={self.hybrid.fusion_strategy}:"
            f"{self.hybrid.lexical_candidate_depth}:{self.hybrid.dense_candidate_depth}:{self.hybrid.rrf_k};"
            f"bm25={self.bm25.k1:.4f}:{self.bm25.b:.4f};"
            f"reranker={self.reranker.enabled}:{self.reranker.provider}:{self.reranker.model}:"
            f"{self.reranker.candidate_top_k}"
        )


def load_rag_config(overrides: dict[str, Any] | None = None) -> RagConfig:
    data = dict(overrides or {})
    chunking_data = data.get("chunking") if isinstance(data.get("chunking"), dict) else {}
    hybrid_data = data.get("hybrid") if isinstance(data.get("hybrid"), dict) else {}
    runtime_bm25_data = get_runtime_value("rag", "bm25", {})
    if not isinstance(runtime_bm25_data, dict):
        runtime_bm25_data = {}
    bm25_data = data.get("bm25") if isinstance(data.get("bm25"), dict) else runtime_bm25_data
    reranker_data = data.get("reranker") if isinstance(data.get("reranker"), dict) else {}
    runtime_remote_data = get_runtime_value("rag", "remote_source", {})
    if not isinstance(runtime_remote_data, dict):
        runtime_remote_data = {}
    remote_data = data.get("remote_source") if isinstance(data.get("remote_source"), dict) else runtime_remote_data

    enabled = _as_bool(data.get("enabled", get_runtime_value("rag", "enabled", False)), False)
    retrieval_profile = str(
        data.get("retrieval_profile", get_runtime_value("rag", "retrieval_profile", "production"))
        or "production"
    ).strip().lower()
    store_raw = data.get("knowledge_store_path", get_runtime_value("rag", "knowledge_store_path", "knowledge_base"))
    eval_raw = data.get("evaluation_cache_path", get_runtime_value("rag", "evaluation_cache_path", "tmp/rag_eval_cache"))
    report_raw = data.get("report_dir", get_runtime_value("rag", "report_dir", "tmp/rag_eval_reports"))
    model_cache_raw = data.get("model_cache_path")
    if model_cache_raw is None and "knowledge_store_path" not in data:
        model_cache_raw = get_runtime_value("rag", "model_cache_path", None)
    embedding_model_raw = data.get("embedding_model", get_runtime_value("rag", "embedding_model", ""))
    reranker_model_raw = data.get("reranker_model", get_runtime_value("rag", "reranker_model", ""))
    embedding_provider = str(
        data.get("embedding_provider", get_runtime_value("rag", "embedding_provider", "local_hashing"))
        or "local_hashing"
    )
    vector_backend = str(data.get("vector_backend", get_runtime_value("rag", "vector_backend", "in_memory")) or "in_memory")
    sparse_backend = str(
        data.get("sparse_backend", get_runtime_value("rag", "sparse_backend", "in_memory_bm25"))
        or "in_memory_bm25"
    )
    embedding_dimension = _as_int(
        data.get("embedding_dimension", get_runtime_value("rag", "embedding_dimension", 0)),
        0,
    )
    qdrant_collection = str(
        data.get("qdrant_collection", get_runtime_value("rag", "qdrant_collection", "knowledge_chunks"))
        or "knowledge_chunks"
    )
    faiss_enabled = _as_bool(
        data.get("faiss_enabled", get_runtime_value("rag", "faiss_enabled", False)),
        False,
    )
    faiss_candidate_multiplier = max(
        1,
        _as_int(
            data.get(
                "faiss_candidate_multiplier",
                get_runtime_value("rag", "faiss_candidate_multiplier", 4),
            ),
            4,
        ),
    )
    embedding_batch_size = _as_int(
        data.get("embedding_batch_size", get_runtime_value("rag", "embedding_batch_size", 8)),
        8,
    )
    reranker_batch_size = _as_int(
        data.get("reranker_batch_size", get_runtime_value("rag", "reranker_batch_size", 8)),
        8,
    )
    max_sequence_length = _as_int(
        data.get("max_sequence_length", get_runtime_value("rag", "max_sequence_length", 8192)),
        8192,
    )
    device = str(data.get("device", get_runtime_value("rag", "device", "auto")) or "auto")

    chunking = SemanticChunkingConfig(
        target_tokens=_as_int(chunking_data.get("target_tokens", data.get("chunk_target_tokens")), 500),
        max_tokens=_as_int(chunking_data.get("max_tokens", data.get("chunk_max_tokens")), 800),
        overlap_ratio=_as_float(chunking_data.get("overlap_ratio", data.get("chunk_overlap_ratio")), 0.12),
        semantic_boundary_threshold=_as_float(
            chunking_data.get("semantic_boundary_threshold", data.get("semantic_boundary_threshold")),
            0.72,
        ),
    )
    hybrid = HybridRetrievalConfig(
        fusion_strategy=str(
            hybrid_data.get(
                "fusion_strategy",
                data.get(
                    "hybrid_fusion",
                    data.get("fusion_strategy", get_runtime_value("rag", "hybrid_fusion", "rrf")),
                ),
            )
            or "rrf"
        ),
        lexical_candidate_depth=_as_int(
            hybrid_data.get(
                "lexical_candidate_depth",
                data.get("lexical_candidate_depth", get_runtime_value("rag", "lexical_candidate_depth", 30)),
            ),
            30,
        ),
        dense_candidate_depth=_as_int(
            hybrid_data.get(
                "dense_candidate_depth",
                data.get("dense_candidate_depth", get_runtime_value("rag", "dense_candidate_depth", 30)),
            ),
            30,
        ),
        rrf_k=_as_int(
            hybrid_data.get("rrf_k", data.get("rrf_k", get_runtime_value("rag", "rrf_k", 60))),
            60,
        ),
        top_k=_as_int(
            hybrid_data.get(
                "top_k",
                data.get(
                    "final_top_k",
                    data.get("top_k", get_runtime_value("rag", "final_top_k", 8)),
                ),
            ),
            8,
        ),
    )
    reranker = RerankerConfig(
        enabled=_as_bool(
            reranker_data.get(
                "enabled",
                data.get(
                    "reranker_enabled_by_default",
                    data.get(
                        "reranker_enabled",
                        get_runtime_value("rag", "reranker_enabled_by_default", False),
                    ),
                ),
            ),
            False,
        ),
        provider=str(
            reranker_data.get(
                "provider",
                data.get("reranker_provider", get_runtime_value("rag", "reranker_provider", "local_overlap")),
            )
            or "local_overlap"
        ),
        model=normalize_huggingface_model_id(
            reranker_data.get("model", reranker_model_raw),
            "",
        ),
        candidate_top_k=_as_int(
            reranker_data.get(
                "candidate_top_k",
                data.get(
                    "reranker_candidate_top_k",
                    get_runtime_value("rag", "reranker_candidate_top_k", 30),
                ),
            ),
            30,
        ),
    )
    bm25 = Bm25Config(
        k1=max(
            0.01,
            _as_float(
                bm25_data.get("k1", data.get("bm25_k1", get_runtime_value("rag", "bm25_k1", 1.2))),
                1.2,
            ),
        ),
        b=min(
            1.0,
            max(
                0.0,
                _as_float(
                    bm25_data.get("b", data.get("bm25_b", get_runtime_value("rag", "bm25_b", 0.75))),
                    0.75,
                ),
            ),
        ),
    )
    remote_source = RemoteSourceConfig(
        enabled=_as_bool(remote_data.get("enabled", True), True),
        provider=str(remote_data.get("provider", "feishu") or "feishu").strip().lower(),
        session_mode=str(remote_data.get("session_mode", "existing_session_store") or "existing_session_store").strip(),
        session_file=str(remote_data.get("session_file", "") or "").strip(),
        max_content_chars=max(
            1,
            _as_int(remote_data.get("max_content_chars", 1_000_000), 1_000_000),
        ),
        max_retries=max(0, _as_int(remote_data.get("max_retries", 1), 1)),
        retry_backoff_seconds=max(
            0.0,
            _as_float(remote_data.get("retry_backoff_seconds", 0.2), 0.2),
        ),
    )
    if retrieval_profile == "deterministic":
        deterministic_reranker_enabled = (
            reranker.enabled
            if "reranker" in data
            or "reranker_enabled_by_default" in data
            or "reranker_enabled" in data
            else False
        )
        embedding_provider = "deterministic"
        embedding_model = ""
        embedding_dimension = 0
        vector_backend = "in_memory"
        faiss_enabled = False
        sparse_backend = "in_memory_bm25"
        reranker = RerankerConfig(
            enabled=deterministic_reranker_enabled,
            provider="local_overlap",
            model="",
            candidate_top_k=reranker.candidate_top_k,
        )
    knowledge_store_path = resolve_runtime_path(store_raw, "knowledge_base")
    return RagConfig(
        enabled=enabled,
        retrieval_profile=retrieval_profile,
        knowledge_store_path=knowledge_store_path,
        evaluation_cache_path=resolve_runtime_path(eval_raw, "tmp/rag_eval_cache"),
        report_dir=resolve_runtime_path(report_raw, "tmp/rag_eval_reports"),
        embedding_provider=embedding_provider,
        vector_backend=vector_backend,
        sparse_backend=sparse_backend,
        embedding_model=normalize_huggingface_model_id(embedding_model_raw, ""),
        embedding_dimension=embedding_dimension,
        model_cache_path=(
            resolve_runtime_path(model_cache_raw, "knowledge_base/models")
            if model_cache_raw
            else knowledge_store_path / "models"
        ),
        qdrant_collection=qdrant_collection,
        faiss_enabled=faiss_enabled,
        faiss_candidate_multiplier=faiss_candidate_multiplier,
        embedding_batch_size=max(1, embedding_batch_size),
        reranker_batch_size=max(1, reranker_batch_size),
        max_sequence_length=max(1, max_sequence_length),
        device=device,
        chunking=chunking,
        hybrid=hybrid,
        bm25=bm25,
        reranker=reranker,
        remote_source=remote_source,
    )
