"""Hybrid guideline retrieval: BM25 (keyword) + local dense vectors (semantic).

Week 2 RAG, built as a plain function (wrapped in the agent graph LAST). Given a
clinical query it returns the top-k guideline chunks, each with source metadata
so every piece of evidence the agent uses can be cited back to a file + page.

Hybrid = BM25 + sentence-transformers cosine, scores min-max normalized and
summed. Cohere rerank is a deliberate Phase-2 upgrade (needs an external API
key); the PRD names "Cohere Rerank OR EQUIVALENT", and a normalized hybrid of
sparse+dense retrieval is a defensible equivalent for a 3-document corpus. This
is documented as a known upgrade in W2_ARCHITECTURE.md, not hidden as a gap.

The index is built once from the PDFs in ./guidelines and cached in-process.
Embedding model is local (all-MiniLM-L6-v2): no API key, runs offline, which
also lets the eval gate exercise retrieval without any network call.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

import numpy as np
from rank_bm25 import BM25Okapi

import os
_RAG_MODE = os.getenv("RAG_MODE", "hybrid").lower()

_GUIDELINES_DIR = Path(__file__).resolve().parent / "guidelines"
_EMBED_MODEL_NAME = "all-MiniLM-L6-v2"

# Paragraph-level chunking. Page-sized chunks were the root cause of the ranking
# defect: a ~2000-character page gives BM25 dozens of common words to match, so
# a "QUALITY OF OPHTHALMIC CARE" appendix scored within 4% of the correct
# dermatochalasis page. Smaller chunks make term frequency meaningful again, and
# make quote_or_value an actual supporting quote instead of the page's header.
_MIN_CHUNK_CHARS = 120
_MAX_CHUNK_CHARS = 600

# Only return evidence that clears this fraction of the top hit's score.
# Page-level chunks make long boilerplate pages (appendices, reference lists)
# score on generic words like "patient" and "care", which would otherwise be
# presented to a physician as supporting evidence. A claim's citation must
# actually support it: weak matches are dropped, not shown.
_MIN_RELATIVE_SCORE = 0.4

# UNUSED / documented negative result: filtering stopwords out of the query was
# tried to stop boilerplate appendix pages from out-scoring clinical content.
# It made ranking WORSE (the correct dermatochalasis page dropped from #1 to #2
# and an unrelated page took the top slot), so it is not wired in. The real
# cause is page-sized chunks; the fix is finer chunking or a reranker -- both
# tracked as Phase-2 work in W2_ARCHITECTURE.md.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from",
    "has", "have", "how", "i", "in", "is", "it", "its", "of", "on", "or", "s",
    "should", "that", "the", "their", "there", "this", "to", "was", "what",
    "when", "where", "which", "who", "why", "will", "with", "would",
})


def _tokenize_query(text: str) -> list[str]:
    """Tokenize a QUERY, dropping stopwords so content words drive the score."""
    tokens = [t.strip("?.,;:!()'\"") for t in text.lower().split()]
    content = [t for t in tokens if t and t not in _STOPWORDS]
    # If the query is nothing but stopwords, fall back to the raw tokens rather
    # than scoring against an empty query.
    return content or [t for t in tokens if t]


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage plus where it came from."""
    guideline_file: str
    page: int  # 1-based
    text: str
    index: int = 0  # nth chunk within this page; disambiguates citation ids


@dataclass(frozen=True)
class EvidenceHit:
    """A retrieved chunk with its combined hybrid score and a citation dict."""
    chunk: Chunk
    score: float

    def citation(self) -> dict:
        # PRD citation shape: {source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}
        return {
            "source_type": "guideline",
            "source_id": self.chunk.guideline_file,
            "page_or_section": f"page {self.chunk.page}",
            "field_or_chunk_id": f"{self.chunk.guideline_file}#p{self.chunk.page}c{self.chunk.index}",
            "quote_or_value": self.chunk.text[:300],
        }


def _split_page_text(text: str) -> list[str]:
    """Split one page's text into paragraph-sized chunks."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paras:
        p = re.sub(r"\s+", " ", p)
        if len(p) > _MAX_CHUNK_CHARS:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(p), _MAX_CHUNK_CHARS):
                chunks.append(p[i:i + _MAX_CHUNK_CHARS])
            continue
        if not buf:
            buf = p
        elif len(buf) + 1 + len(p) <= _MAX_CHUNK_CHARS:
            buf = buf + " " + p
        else:
            chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)

    merged: list[str] = []
    for c in chunks:
        if merged and len(c) < _MIN_CHUNK_CHARS and len(merged[-1]) + len(c) + 1 <= _MAX_CHUNK_CHARS:
            merged[-1] = merged[-1] + " " + c
        else:
            merged.append(c)
    return [c for c in merged if len(c) >= 40]


def _load_chunks() -> list[Chunk]:
    """Paragraph-level chunks from each guideline PDF, tagged with their page."""
    import fitz  # pymupdf — lazy: gate imports only need Chunk/EvidenceHit/_tokenize

    chunks: list[Chunk] = []
    for pdf in sorted(_GUIDELINES_DIR.glob("*.pdf")):
        doc = fitz.open(pdf)
        try:
            for i in range(doc.page_count):
                text = doc.load_page(i).get_text().strip()
                if not text:
                    continue
                for j, piece in enumerate(_split_page_text(text)):
                    chunks.append(Chunk(guideline_file=pdf.name, page=i + 1, text=piece, index=j))
        finally:
            doc.close()
    return chunks


def _tokenize(text: str) -> list[str]:
    return [t for t in text.lower().split() if t]


def _minmax(x: np.ndarray) -> np.ndarray:
    lo, hi = float(x.min()), float(x.max())
    if hi - lo < 1e-12:
        return np.zeros_like(x)
    return (x - lo) / (hi - lo)


@lru_cache(maxsize=1)
def _build_index():
    chunks = _load_chunks()
    if not chunks:
        raise RuntimeError(f"No guideline text found in {_GUIDELINES_DIR}")
    bm25 = BM25Okapi([_tokenize(c.text) for c in chunks])
    if _RAG_MODE == "bm25":
        return chunks, bm25, None, None
    from sentence_transformers import SentenceTransformer  # lazy: keeps the offline gate fast
    model = SentenceTransformer(_EMBED_MODEL_NAME)
    embeddings = model.encode([c.text for c in chunks], normalize_embeddings=True)
    return chunks, bm25, model, np.asarray(embeddings)


def retrieve_evidence(query: str, k: int = 3) -> list[EvidenceHit]:
    """Return top-k guideline chunks for a query, scored by hybrid BM25+dense (or BM25 only if RAG_MODE=bm25)."""
    chunks, bm25, model, embeddings = _build_index()

    bm25_scores = np.asarray(bm25.get_scores(_tokenize(query)), dtype=float)

    if model is None or embeddings is None:
        combined = bm25_scores
    else:
        q_emb = model.encode([query], normalize_embeddings=True)[0]
        dense_scores = embeddings @ np.asarray(q_emb)
        combined = _minmax(bm25_scores) + _minmax(dense_scores)

    top_idx = np.argsort(combined)[::-1][:k]
    hits = [EvidenceHit(chunk=chunks[i], score=float(combined[i])) for i in top_idx]

    if not hits or hits[0].score <= 0:
        return []
    cutoff = hits[0].score * _MIN_RELATIVE_SCORE
    return [h for h in hits if h.score >= cutoff]


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "dermatochalasis management"
    for hit in retrieve_evidence(q):
        c = hit.chunk
        snippet = c.text[:120].replace("\n", " ").encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"
        )
        print(f"[{hit.score:.3f}] {c.guideline_file} p{c.page}: {snippet}...")
