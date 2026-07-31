from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .evaluation import BenchmarkDataset, EvaluationConfig, EvaluationHarness
from .config import load_rag_config
from .model_assets import download_configured_huggingface_models
from .service import PersonalKnowledgeBase


class KnowledgeIndexFilesInput(BaseModel):
    collection: str = Field(..., description="Knowledge collection name.")
    paths: list[str] = Field(..., description="Workspace file paths to index.")
    refresh: bool = Field(False, description="Refresh existing indexed documents.")
    metadata: dict[str, Any] | None = Field(None, description="Optional generic metadata.")


class KnowledgeImportFilesInput(BaseModel):
    collection: str = Field("default", description="Knowledge collection name.")
    paths: list[str] = Field(..., description="Workspace file paths to import into the project knowledge base.")
    metadata: dict[str, Any] | None = Field(None, description="Optional generic metadata.")


class KnowledgeSyncInput(BaseModel):
    collection: str | None = Field(None, description="Optional collection to sync.")
    dry_run: bool = Field(False, description="Preview sync without changing indexes.")


class KnowledgeSearchInput(BaseModel):
    query: str = Field(..., description="Search query.")
    collection: str | None = Field(None, description="Optional collection filter.")
    filters: dict[str, Any] | None = Field(None, description="Optional metadata filters.")
    top_k: int = Field(8, description="Maximum number of chunks to return.")
    include_scores: bool = Field(True, description="Include retrieval score components.")


class KnowledgeDeleteDocumentInput(BaseModel):
    doc_id: str | None = Field(None, description="Document id to delete.")
    collection: str | None = Field(None, description="Optional collection filter.")
    source_uri: str | None = Field(None, description="Optional source URI to delete.")


class KnowledgeEvaluateInput(BaseModel):
    run_id: str = Field("rag-smoke", description="Evaluation run id.")
    sample_limit: int | None = Field(None, description="Optional sample limit for smoke runs.")


class KnowledgeDownloadModelsInput(BaseModel):
    local_files_only: bool = Field(False, description="Use only existing local Hugging Face cache files.")


def build_knowledge_tools(
    *,
    workspace_root: str | Path,
    config_overrides: dict[str, Any] | None = None,
) -> list[Any]:
    config = load_rag_config(config_overrides)
    if not config.enabled:
        return []
    def knowledge_base() -> PersonalKnowledgeBase:
        return PersonalKnowledgeBase(
            workspace_root=workspace_root,
            store_path=config.knowledge_store_path,
            chunker_config=config.chunking,
            reranker_enabled=config.reranker.enabled,
        )

    def knowledge_import_files(collection: str, paths: list[str], metadata: dict[str, Any] | None = None) -> str:
        return json.dumps(knowledge_base().import_files(collection, paths, metadata=metadata), ensure_ascii=False, indent=2)

    def knowledge_sync(collection: str | None = None, dry_run: bool = False) -> str:
        return json.dumps(knowledge_base().sync(collection=collection, dry_run=dry_run), ensure_ascii=False, indent=2)

    def knowledge_index_files(collection: str, paths: list[str], refresh: bool = False, metadata: dict[str, Any] | None = None) -> str:
        return json.dumps(knowledge_base().index_files(collection, paths, refresh=refresh, metadata=metadata), ensure_ascii=False, indent=2)

    def knowledge_search(
        query: str,
        collection: str | None = None,
        filters: dict[str, Any] | None = None,
        top_k: int = 8,
        include_scores: bool = True,
    ) -> str:
        return json.dumps(
            knowledge_base().search(query, collection=collection, filters=filters, top_k=top_k, include_scores=include_scores),
            ensure_ascii=False,
            indent=2,
        )

    def knowledge_list_collections() -> str:
        return json.dumps(knowledge_base().list_collections(), ensure_ascii=False, indent=2)

    def knowledge_list_documents(collection: str | None = None) -> str:
        return json.dumps(knowledge_base().list_documents(collection), ensure_ascii=False, indent=2)

    def knowledge_delete_document(doc_id: str | None = None, collection: str | None = None, source_uri: str | None = None) -> str:
        return json.dumps(
            knowledge_base().delete_document(doc_id=doc_id, collection=collection, source_uri=source_uri),
            ensure_ascii=False,
            indent=2,
        )

    def knowledge_evaluate(run_id: str = "rag-smoke", sample_limit: int | None = None) -> str:
        dataset = BenchmarkDataset(
            dataset_id="public-smoke-fixture",
            kind="retrieval",
            corpus={"d1": "alpha beta", "d2": "gamma delta"},
            queries={"q1": "alpha"},
            qrels={"q1": {"d1": 1}},
            public_source="fixture public dataset",
        )
        harness = EvaluationHarness(
            EvaluationConfig(
                report_dir=config.report_dir,
                quality_gates={"average_ndcg@10": 0.1},
                sample_limit=sample_limit,
            )
        )
        return json.dumps(harness.run([dataset], run_id=run_id), ensure_ascii=False, indent=2)

    def knowledge_download_models(local_files_only: bool = False) -> str:
        return json.dumps(
            download_configured_huggingface_models(config, local_files_only=local_files_only),
            ensure_ascii=False,
            indent=2,
        )

    return [
        StructuredTool.from_function(
            knowledge_import_files,
            name="knowledge_import_files",
            description="Import selected workspace files into the project knowledge base document folder.",
            args_schema=KnowledgeImportFilesInput,
        ),
        StructuredTool.from_function(
            knowledge_sync,
            name="knowledge_sync",
            description="Synchronize the project knowledge base folder with manifests and retrieval indexes.",
            args_schema=KnowledgeSyncInput,
        ),
        StructuredTool.from_function(
            knowledge_index_files,
            name="knowledge_index_files",
            description="Index selected workspace files into a personal knowledge collection.",
            args_schema=KnowledgeIndexFilesInput,
        ),
        StructuredTool.from_function(
            knowledge_search,
            name="knowledge_search",
            description="Search indexed personal knowledge chunks with hybrid retrieval.",
            args_schema=KnowledgeSearchInput,
        ),
        StructuredTool.from_function(
            knowledge_list_collections,
            name="knowledge_list_collections",
            description="List personal knowledge collections and counts.",
        ),
        StructuredTool.from_function(
            knowledge_list_documents,
            name="knowledge_list_documents",
            description="List indexed personal knowledge documents.",
        ),
        StructuredTool.from_function(
            knowledge_delete_document,
            name="knowledge_delete_document",
            description="Delete an indexed personal knowledge document and its chunks.",
            args_schema=KnowledgeDeleteDocumentInput,
        ),
        StructuredTool.from_function(
            knowledge_evaluate,
            name="knowledge_evaluate",
            description="Run the configured RAG benchmark smoke evaluator.",
            args_schema=KnowledgeEvaluateInput,
        ),
        StructuredTool.from_function(
            knowledge_download_models,
            name="knowledge_download_models",
            description="Download configured Hugging Face embedding and reranker models into the local knowledge base model cache.",
            args_schema=KnowledgeDownloadModelsInput,
        ),
    ]
