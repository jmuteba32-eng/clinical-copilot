"""GATE / guideline chunks carry a locatable bounding box (offline).

Step 3 overlay contract: every guideline chunk built from a text-layer PDF must
resolve to a bounding box on its page, so a cited passage can be highlighted in
the source. A chunk whose anchor text is not on the page must yield bbox=None --
never a wrong box. Runs offline over the real guideline corpus; no model, no API.
"""
from __future__ import annotations

from agent.rag.retrieve import _load_chunks, _locate_bbox


def test_guideline_chunks_are_locatable_on_their_page() -> None:
    """The real corpus must resolve a box for the overwhelming majority of chunks.
    A regression in chunking or anchoring that broke search_for would drop this."""
    chunks = _load_chunks()
    assert chunks, "no guideline chunks were built"
    located = [c for c in chunks if c.bbox is not None]
    ratio = len(located) / len(chunks)
    assert ratio >= 0.9, f"only {ratio:.0%} of chunks got a bbox; overlay would mostly fall back"


def test_located_boxes_are_in_page_bounds_and_well_formed() -> None:
    """A box must be a real rect: x0<x1, y0<y1, and inside a plausible page.
    Guards against a scale/union bug producing inverted or runaway rects."""
    for c in _load_chunks():
        if c.bbox is None:
            continue
        x0, y0, x1, y1 = c.bbox
        assert x1 > x0 and y1 > y0, f"degenerate box on {c.guideline_file} p{c.page}: {c.bbox}"
        # No standard page exceeds ~1000pt on a side; a box beyond that is a scale bug.
        assert 0 <= x0 < 1000 and 0 <= y0 < 1000, f"out-of-bounds box on {c.guideline_file} p{c.page}: {c.bbox}"


def test_text_absent_from_page_yields_no_box() -> None:
    """A chunk whose text is not on the page must return None, not a wrong box.
    This is the fallback contract the UI relies on (open to page, draw nothing)."""
    import fitz
    from agent.rag.retrieve import _GUIDELINES_DIR

    pdf = sorted(_GUIDELINES_DIR.glob("*.pdf"))[0]
    doc = fitz.open(pdf)
    try:
        page = doc.load_page(0)
        assert _locate_bbox(page, "zzqxw not present on any guideline page zzqxw") is None
    finally:
        doc.close()
