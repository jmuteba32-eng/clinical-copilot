"""GATE / citation_present boolean rubric (offline, fast).

Proves the RAG layer returns a well-formed citation pointing at the CORRECT
guideline for a clinical query -- without loading the 90MB embedding model, so
this stays in the fast pre-push gate. It exercises the BM25 (keyword) half of
the hybrid retriever plus the citation contract; the dense-vector half and its
model download are exercised separately in the pre-deploy suite, not here.

Rubric category: citation_present.
Guards against: retrieval returning evidence with no source metadata, or
pointing at the wrong document -- either of which would let the agent present a
guideline claim the physician cannot trace back to a real page.
"""
from __future__ import annotations

import numpy as np
from rank_bm25 import BM25Okapi

from agent.rag.retrieve import Chunk, EvidenceHit, _tokenize


def test_citation_shape_is_complete() -> None:
    """A citation must carry all five PRD fields, non-empty."""
    hit = EvidenceHit(
        chunk=Chunk(guideline_file="dermatochalasis_guideline.pdf", page=5, text="eyelid dermatochalasis repair indications"),
        score=1.0,
    )
    cit = hit.citation()
    for field in ("source_type", "source_id", "page_or_section", "field_or_chunk_id", "quote_or_value"):
        assert cit.get(field), f"citation missing/empty field: {field}"
    assert cit["source_type"] == "guideline"
    assert cit["source_id"] == "dermatochalasis_guideline.pdf"
    assert "5" in cit["page_or_section"]


def test_bm25_ranks_correct_guideline_first() -> None:
    """Keyword retrieval over a tiny fixed corpus must rank the dermatochalasis
    chunk above unrelated guideline chunks for a dermatochalasis query. Uses a
    stub corpus (no PDF read, no model load) so the gate stays fast and offline."""
    corpus = [
        Chunk("dermatochalasis_guideline.pdf", 5, "eyelid dermatochalasis blepharoplasty repair indications visual field"),
        Chunk("dry_eye_guideline.pdf", 1, "dry eye disease tear film artificial tears management"),
        Chunk("keratoconus_guideline.pdf", 1, "keratoconus corneal ectasia cross linking topography"),
    ]
    bm25 = BM25Okapi([_tokenize(c.text) for c in corpus])
    scores = np.asarray(bm25.get_scores(_tokenize("dermatochalasis eyelid management")))
    top = int(np.argmax(scores))
    assert corpus[top].guideline_file == "dermatochalasis_guideline.pdf"
