"""Generic structural import model for document replacement planning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .document_replacement import canonical_block_summary, canonical_content_hash
from .docx_ot import parse_markdown


_NODE_KINDS = frozenset({
    "title",
    "heading",
    "paragraph",
    "ordered_list",
    "unordered_list",
    "quote",
    "code",
    "table",
    "metadata",
})


@dataclass(frozen=True)
class StructuralNode:
    """One content or metadata unit, independent of any source terminology."""

    kind: str
    text: str = ""
    level: int = 1
    language: str = ""
    rows: tuple[tuple[str, ...], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructuredDocument:
    nodes: tuple[StructuralNode, ...]
    metadata: Mapping[str, Any]
    confidence: float
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class ReplacementPlan:
    markdown: str
    block_summary: dict[str, int]
    source_hash: str
    confidence: float
    diagnostics: list[str]
    requires_preview: bool
    mutation_allowed: bool


def canonical_serialize(document: StructuredDocument) -> str:
    """Return a stable, complete serialization suitable for audits and tests."""
    payload = {
        "nodes": [
            {
                "kind": node.kind,
                "text": node.text,
                "level": node.level,
                "language": node.language,
                "rows": [list(row) for row in node.rows],
                "metadata": dict(node.metadata),
            }
            for node in document.nodes
        ],
        "metadata": dict(document.metadata),
        "confidence": document.confidence,
        "diagnostics": list(document.diagnostics),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _coerce_node(record: Mapping[str, Any], diagnostics: list[str]) -> StructuralNode | None:
    kind = str(record.get("type") or "paragraph").strip().lower()
    if kind not in _NODE_KINDS:
        diagnostics.append(f"unsupported_structure:{kind}")
        return StructuralNode(kind="paragraph", text=str(record.get("text") or ""))
    if kind == "metadata":
        return None
    rows = tuple(tuple(str(cell) for cell in row) for row in record.get("rows") or ())
    return StructuralNode(
        kind=kind,
        text=str(record.get("text") or ""),
        level=max(1, min(6, int(record.get("level") or 1))),
        language=str(record.get("language") or ""),
        rows=rows,
        metadata=dict(record.get("metadata") or {}),
    )


def extract_structured_document(
    *,
    source_path: str = "",
    pages: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None = None,
) -> StructuredDocument:
    """Convert typed page/block records into a source-neutral structural document."""
    document_metadata: dict[str, Any] = dict(metadata or {})
    if source_path:
        document_metadata["source_path"] = source_path
    page_numbers: list[int] = []
    diagnostics: list[str] = []
    confidences: list[float] = []
    nodes: list[StructuralNode] = []
    for index, page in enumerate(pages, start=1):
        page_number = int(page.get("number") or index)
        page_numbers.append(page_number)
        confidences.append(float(page.get("reading_order_confidence", 1.0)))
        diagnostics.extend(str(item) for item in page.get("diagnostics") or [])
        for record in page.get("blocks") or []:
            if not isinstance(record, Mapping):
                diagnostics.append("invalid_structure_record")
                continue
            if str(record.get("type") or "").lower() == "metadata":
                document_metadata.update(dict(record.get("metadata") or {}))
                continue
            node = _coerce_node(record, diagnostics)
            if node is not None:
                nodes.append(node)
    document_metadata["page_numbers"] = page_numbers
    confidence = min(confidences) if confidences else 0.0
    return StructuredDocument(
        nodes=tuple(nodes),
        metadata=document_metadata,
        confidence=confidence,
        diagnostics=tuple(diagnostics),
    )


def _render_table(rows: tuple[tuple[str, ...], ...]) -> str:
    if not rows:
        return ""
    width = len(rows[0])
    normalized = [list(row[:width]) + [""] * max(0, width - len(row)) for row in rows]

    def row_line(row: list[str]) -> str:
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"

    return "\n".join(
        [row_line(normalized[0]), row_line(["---"] * width), *[row_line(row) for row in normalized[1:]]]
    )


def render_replacement_plan(
    document: StructuredDocument,
    *,
    confidence_threshold: float = 0.8,
    confirm: bool = False,
) -> ReplacementPlan:
    """Render only body structures; metadata stays available for audit and preview."""
    lines: list[str] = []
    for node in document.nodes:
        if node.kind == "title":
            lines.append(f"# {node.text}")
        elif node.kind == "heading":
            lines.append(f"{'#' * node.level} {node.text}")
        elif node.kind == "paragraph":
            lines.append(node.text)
        elif node.kind == "ordered_list":
            lines.append(f"1. {node.text}")
        elif node.kind == "unordered_list":
            lines.append(f"- {node.text}")
        elif node.kind == "quote":
            lines.append(f"> {node.text}")
        elif node.kind == "code":
            lines.append(f"```{node.language}\n{node.text}\n```")
        elif node.kind == "table":
            rendered = _render_table(node.rows)
            if rendered:
                lines.append(rendered)
    markdown = "\n\n".join(line for line in lines if line)
    blocks = parse_markdown(markdown)
    low_confidence = document.confidence < confidence_threshold
    requires_preview = low_confidence and not confirm
    return ReplacementPlan(
        markdown=markdown,
        block_summary=canonical_block_summary(blocks),
        source_hash=canonical_content_hash(blocks),
        confidence=document.confidence,
        diagnostics=list(document.diagnostics),
        requires_preview=requires_preview,
        mutation_allowed=not requires_preview,
    )
