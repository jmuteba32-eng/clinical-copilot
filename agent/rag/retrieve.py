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

import numpy as np
from rank_bm25 import BM25Okapi

import os
_RAG_MODE = os.getenv("RAG_MODE", "hybrid").lower()

_GUIDELINES_DIR = Path(__file__).resolve().parent / "guidelines"
_EMBED_MODEL_NAME = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class Chunk:
    """One retrievable passage plus where it came from."""
    guideline_file: str
    page: int  # 1-based
    text: str


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
            "field_or_chunk_id": f"{self.chunk.guideline_file}#p{self.chunk.page}",
            "quote_or_value": self.chunk.text[:300],
        }


def _load_chunks() -> list[Chunk]:
    """One chunk per page of each guideline PDF. Page-level chunking is
    deliberately coarse for the thin slice; finer chunking is a Phase-2 knob."""
    import fitz  # pymupdf — lazy: gate imports only need Chunk/EvidenceHit/_tokenize

    chunks: list[Chunk] = []
    for pdf in sorted(_GUIDELINES_DIR.glob("*.pdf")):
        doc = fitz.open(pdf)
        try:
            for i in range(doc.page_count):
                text = doc.load_page(i).get_text().strip()
                if text:
                    chunks.append(Chunk(guideline_file=pdf.name, page=i + 1, text=text))
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
    return [EvidenceHit(chunk=chunks[i], score=float(combined[i])) for i in top_idx]


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "dermatochalasis management"
    for hit in retrieve_evidence(q):
        c = hit.chunk
        snippet = c.text[:120].replace("\n", " ").encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"
        )
        print(f"[{hit.score:.3f}] {c.guideline_file} p{c.page}: {snippet}...")
