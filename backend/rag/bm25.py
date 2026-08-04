from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Sequence

from .chunking import tokenize


class BM25Index:
    """Small deterministic BM25 index for the currently visible chunks."""

    def __init__(self, chunks: Sequence[Any], *, k1: float = 1.2, b: float = 0.75) -> None:
        if k1 <= 0:
            raise ValueError("BM25 k1 must be positive")
        if not 0 <= b <= 1:
            raise ValueError("BM25 b must be between 0 and 1")
        self.k1 = float(k1)
        self.b = float(b)
        self.chunks = list(chunks)
        self._term_counts: dict[str, Counter[str]] = {}
        self._document_frequency: Counter[str] = Counter()
        self._document_lengths: dict[str, int] = {}
        for chunk in self.chunks:
            chunk_id = str(chunk.chunk_id)
            counts = Counter(tokenize(str(chunk.text or "")))
            self._term_counts[chunk_id] = counts
            self._document_lengths[chunk_id] = sum(counts.values())
            for term in counts:
                self._document_frequency[term] += 1
        total_length = sum(self._document_lengths.values())
        self.average_document_length = total_length / len(self.chunks) if self.chunks else 0.0

    @property
    def document_count(self) -> int:
        return len(self.chunks)

    def _idf(self, term: str) -> float:
        document_count = self.document_count
        document_frequency = self._document_frequency.get(term, 0)
        if not document_count or not document_frequency:
            return 0.0
        return math.log(
            1.0
            + (document_count - document_frequency + 0.5)
            / (document_frequency + 0.5)
        )

    def score(self, query: str | Sequence[str], chunk: Any) -> float:
        query_terms = tokenize(query) if isinstance(query, str) else [str(term).lower() for term in query]
        term_counts = self._term_counts.get(str(chunk.chunk_id), Counter())
        document_length = self._document_lengths.get(str(chunk.chunk_id), 0)
        if not query_terms or not term_counts or not self.average_document_length:
            return 0.0
        normalization = 1.0 - self.b + self.b * document_length / self.average_document_length
        score = 0.0
        for term in set(query_terms):
            term_frequency = term_counts.get(term, 0)
            if not term_frequency:
                continue
            numerator = term_frequency * (self.k1 + 1.0)
            denominator = term_frequency + self.k1 * normalization
            score += self._idf(term) * numerator / denominator
        return score

    def search(self, query: str | Sequence[str], *, limit: int = 30) -> list[tuple[Any, float]]:
        if limit <= 0:
            return []
        scored = [
            (chunk, self.score(query, chunk))
            for chunk in self.chunks
        ]
        scored = [(chunk, score) for chunk, score in scored if score > 0.0]
        scored.sort(key=lambda item: (-item[1], str(item[0].chunk_id)))
        return scored[: int(limit)]
