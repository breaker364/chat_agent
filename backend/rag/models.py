from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ParsedBlock:
    text: str
    block_type: str
    heading_path: list[str] = field(default_factory=list)
    start_offset: int = 0
    end_offset: int = 0


@dataclass
class KnowledgeChunk:
    chunk_id: str
    doc_id: str
    collection: str
    ordinal: int
    text: str
    content_hash: str
    heading_path: list[str]
    source_ref: str
    start_offset: int
    end_offset: int
    token_count: int
    chunking_strategy: str
    boundary_method: str
    overlap_from_previous: int = 0
    overlap_to_next: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DocumentManifest:
    doc_id: str
    collection: str
    source_uri: str
    source_type: str
    content_hash: str
    parser_version: str
    chunker_version: str
    chunking_signature: str
    title: str
    status: str
    chunk_count: int
    metadata: dict[str, Any] = field(default_factory=dict)
    latest_error: str = ""
    retrieval_signature: str = ""
    indexed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
