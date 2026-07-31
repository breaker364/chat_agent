from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from .config import SemanticChunkingConfig
from .models import KnowledgeChunk, ParsedBlock

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in _TOKEN_RE.findall(text or "")]


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    return max(1, len(tokenize(text)))


def _heading_text(line: str) -> str | None:
    match = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
    if not match:
        return None
    return match.group(2).strip().strip("#").strip()


def _line_offsets(lines: list[str]) -> list[int]:
    offsets: list[int] = []
    current = 0
    for line in lines:
        offsets.append(current)
        current += len(line) + 1
    return offsets


class StructureFirstSemanticChunker:
    def __init__(self, config: SemanticChunkingConfig | None = None) -> None:
        self.config = config or SemanticChunkingConfig()

    def parse_blocks(self, text: str) -> list[ParsedBlock]:
        lines = (text or "").splitlines()
        offsets = _line_offsets(lines)
        blocks: list[ParsedBlock] = []
        heading_path: list[str] = []
        paragraph: list[str] = []
        paragraph_start = 0
        index = 0

        def flush_paragraph(end_index: int) -> None:
            nonlocal paragraph, paragraph_start
            if not paragraph:
                return
            block_text = "\n".join(paragraph).strip()
            if block_text:
                end_offset = offsets[end_index - 1] + len(lines[end_index - 1]) if end_index > 0 else 0
                blocks.append(
                    ParsedBlock(
                        text=block_text,
                        block_type="paragraph",
                        heading_path=list(heading_path),
                        start_offset=paragraph_start,
                        end_offset=end_offset,
                    )
                )
            paragraph = []

        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            heading = _heading_text(line)
            if heading is not None:
                flush_paragraph(index)
                heading_path = [heading]
                index += 1
                continue
            if not stripped:
                flush_paragraph(index)
                index += 1
                continue
            if stripped.startswith("```"):
                flush_paragraph(index)
                start = index
                code_lines = [line]
                index += 1
                while index < len(lines):
                    code_lines.append(lines[index])
                    if lines[index].strip().startswith("```"):
                        index += 1
                        break
                    index += 1
                blocks.append(
                    ParsedBlock(
                        text="\n".join(code_lines).strip(),
                        block_type="code",
                        heading_path=list(heading_path),
                        start_offset=offsets[start],
                        end_offset=offsets[index - 1] + len(lines[index - 1]),
                    )
                )
                continue
            if stripped.startswith("|"):
                flush_paragraph(index)
                start = index
                table_lines: list[str] = []
                while index < len(lines) and lines[index].strip().startswith("|"):
                    table_lines.append(lines[index])
                    index += 1
                blocks.append(
                    ParsedBlock(
                        text="\n".join(table_lines).strip(),
                        block_type="table",
                        heading_path=list(heading_path),
                        start_offset=offsets[start],
                        end_offset=offsets[index - 1] + len(lines[index - 1]),
                    )
                )
                continue
            if not paragraph:
                paragraph_start = offsets[index]
            paragraph.append(line)
            index += 1
        flush_paragraph(len(lines))
        return blocks

    def _split_oversized_block(self, block: ParsedBlock) -> list[ParsedBlock]:
        if _estimate_tokens(block.text) <= self.config.max_tokens:
            return [block]
        sentences = [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+", block.text) if part.strip()]
        if len(sentences) <= 1:
            words = block.text.split()
            if not words:
                return [block]
            window = max(1, self.config.max_tokens)
            overlap = max(0, int(window * self.config.overlap_ratio))
            step = max(1, window - overlap)
            chunks = []
            for start in range(0, len(words), step):
                chunk_words = words[start:start + window]
                if not chunk_words:
                    continue
                chunks.append(
                    replace(
                        block,
                        text=" ".join(chunk_words),
                        block_type="token-window",
                    )
                )
            return chunks
        split_blocks: list[ParsedBlock] = []
        current: list[str] = []
        for sentence in sentences:
            candidate = " ".join(current + [sentence])
            if current and _estimate_tokens(candidate) > self.config.max_tokens:
                split_blocks.append(replace(block, text=" ".join(current), block_type="semantic"))
                current = [sentence]
            else:
                current.append(sentence)
        if current:
            split_blocks.append(replace(block, text=" ".join(current), block_type="semantic"))
        return split_blocks

    def chunk_text(
        self,
        text: str,
        *,
        source_ref: str = "",
        doc_id: str = "",
        collection: str = "",
    ) -> list[KnowledgeChunk]:
        atomic_blocks: list[ParsedBlock] = []
        for block in self.parse_blocks(text):
            atomic_blocks.extend(self._split_oversized_block(block))

        chunk_groups: list[list[ParsedBlock]] = []
        current: list[ParsedBlock] = []
        current_tokens = 0
        current_heading: tuple[str, ...] | None = None
        for block in atomic_blocks:
            tokens = _estimate_tokens(block.text)
            block_heading = tuple(block.heading_path)
            can_merge = (
                current
                and current_heading == block_heading
                and current_tokens + tokens <= self.config.target_tokens
                and block.block_type not in {"table", "code"}
            )
            if current and not can_merge:
                chunk_groups.append(current)
                current = []
                current_tokens = 0
            current.append(block)
            current_tokens += tokens
            current_heading = block_heading
        if current:
            chunk_groups.append(current)

        chunks: list[KnowledgeChunk] = []
        resolved_doc_id = doc_id or f"doc-{content_hash(source_ref + text)[:16]}"
        for ordinal, group in enumerate(chunk_groups):
            chunk_text = "\n\n".join(block.text for block in group).strip()
            boundary_types = sorted({block.block_type for block in group})
            method = "mixed" if len(boundary_types) > 1 else boundary_types[0]
            chunk_hash = content_hash(
                f"{resolved_doc_id}:{ordinal}:{self.config.signature()}:{chunk_text}"
            )
            chunks.append(
                KnowledgeChunk(
                    chunk_id=f"{resolved_doc_id}:chunk:{ordinal}:{chunk_hash[:12]}",
                    doc_id=resolved_doc_id,
                    collection=collection,
                    ordinal=ordinal,
                    text=chunk_text,
                    content_hash=content_hash(chunk_text),
                    heading_path=list(group[0].heading_path),
                    source_ref=source_ref,
                    start_offset=min(block.start_offset for block in group),
                    end_offset=max(block.end_offset for block in group),
                    token_count=_estimate_tokens(chunk_text),
                    chunking_strategy="structure-first-semantic",
                    boundary_method=method,
                )
            )
        return chunks
