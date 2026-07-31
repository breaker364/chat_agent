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
class RagConfig:
    enabled: bool = False
    knowledge_store_path: Path = field(default_factory=lambda: resolve_runtime_path(None, "knowledge_base"))
    evaluation_cache_path: Path = field(default_factory=lambda: resolve_runtime_path(None, "tmp/rag_eval_cache"))
    report_dir: Path = field(default_factory=lambda: resolve_runtime_path(None, "tmp/rag_eval_reports"))
    embedding_provider: str = "local_hashing"
    vector_backend: str = "in_memory"
    sparse_backend: str = "in_memory_bm25"
    embedding_model: str = ""
    embedding_dimension: int = 0
    model_cache_path: Path = field(default_factory=lambda: resolve_runtime_path(None, "knowledge_base/models"))
    chunking: SemanticChunkingConfig = field(default_factory=SemanticChunkingConfig)
    hybrid: HybridRetrievalConfig = field(default_factory=HybridRetrievalConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)

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


def load_rag_config(overrides: dict[str, Any] | None = None) -> RagConfig:
    data = dict(overrides or {})
    chunking_data = data.get("chunking") if isinstance(data.get("chunking"), dict) else {}
    hybrid_data = data.get("hybrid") if isinstance(data.get("hybrid"), dict) else {}
    reranker_data = data.get("reranker") if isinstance(data.get("reranker"), dict) else {}

    enabled = _as_bool(data.get("enabled", get_runtime_value("rag", "enabled", False)), False)
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
            hybrid_data.get("fusion_strategy", data.get("hybrid_fusion", data.get("fusion_strategy", "rrf")))
            or "rrf"
        ),
        lexical_candidate_depth=_as_int(hybrid_data.get("lexical_candidate_depth"), 30),
        dense_candidate_depth=_as_int(hybrid_data.get("dense_candidate_depth"), 30),
        rrf_k=_as_int(hybrid_data.get("rrf_k"), 60),
        top_k=_as_int(hybrid_data.get("top_k", data.get("final_top_k", data.get("top_k"))), 8),
    )
    reranker = RerankerConfig(
        enabled=_as_bool(
            reranker_data.get("enabled", data.get("reranker_enabled_by_default", data.get("reranker_enabled"))),
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
            reranker_data.get("candidate_top_k", data.get("reranker_candidate_top_k")),
            30,
        ),
    )
    knowledge_store_path = resolve_runtime_path(store_raw, "knowledge_base")
    return RagConfig(
        enabled=enabled,
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
        chunking=chunking,
        hybrid=hybrid,
        reranker=reranker,
    )
