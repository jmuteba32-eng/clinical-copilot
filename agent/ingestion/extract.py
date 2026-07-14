"""Plain intake-form extraction: PDF -> all page images -> Claude vision -> strict Pydantic.

This is a plain function on purpose (Week 2 strategy: build the capability
standalone, wrap it in the agent graph LAST). It makes ONE external call (the
vision model) and does exactly one thing: turn a full intake form into a
validated IntakeFormExtraction, or raise if the model's output does not conform
to the schema. It does not touch OpenEMR or FHIR -- that is the next step.

The schema is the source of truth: raw model JSON is parsed through
IntakeFormExtraction.model_validate, so anything the model invents or malforms
is rejected here, not trusted downstream.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from dotenv import load_dotenv

# agent/.env for local CLI runs (same path convention as main.py, one level up).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import fitz  # pymupdf
from anthropic import Anthropic

from agent.ingestion.schemas import IntakeFormExtraction

_MODEL = "claude-sonnet-4-6"  # vision-capable; matches the project's model tier

_EXTRACTION_INSTRUCTION = """You are a careful clinical intake-form extractor.
You are given ALL pages of one patient intake form, in order, as images.
Extract what is written ACROSS THE WHOLE FORM into one JSON object of this shape:

{
  "patient_name": <str>,
  "date_of_birth": <str or null>,
  "sex": <str or null>,
  "race_ethnicity": <str or null>,
  "height": <str or null>,
  "weight": <str or null>,
  "chief_concern": <str>,
  "concern_onset": <str or null>,
  "concern_symptoms": <str or null>,
  "medications": [{"name": <str>, "dose": <str or null>}],
  "allergies": [{"allergen": <str>, "reaction": <str or null>}],
  "family_history": [{"relative": <str>, "condition": <str or null>, "living": <bool or null>, "deceased_age": <int or null>}]
}

Rules:
- Transcribe values EXACTLY as written. Do not infer, normalize, or invent.
- Pull each field from whichever page it appears on (name/demographics are early; chief concern is later).
- If a field is blank anywhere, use null (or an empty list).
- If allergies explicitly say None, return a single entry {"allergen": "None", "reaction": null}.
- Do NOT extract Social Security Number, address, phone, email, or insurance details.
- Return ONLY the JSON object. No prose, no markdown, no code fences.
"""


def _render_all_pages_to_png(pdf_path: Path) -> list[bytes]:
    """Render every page of a PDF to PNG bytes at a legible DPI."""
    doc = fitz.open(pdf_path)
    try:
        return [doc.load_page(i).get_pixmap(dpi=150).tobytes("png") for i in range(doc.page_count)]
    finally:
        doc.close()


def extract_intake_form(
    pdf_path: str | Path,
    source_id: str | None = None,
) -> IntakeFormExtraction:
    """Extract a full intake-form PDF (all pages) into a validated IntakeFormExtraction.

    Raises pydantic.ValidationError if the model output does not conform to the
    schema, and json.JSONDecodeError if the model does not return valid JSON --
    both are deliberate: malformed extraction must fail loudly, not pass silently.
    """
    pdf_path = Path(pdf_path)
    pages = _render_all_pages_to_png(pdf_path)

    content: list[dict] = []
    for png in pages:
        b64 = base64.standard_b64encode(png).decode("ascii")
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
        )
    content.append({"type": "text", "text": _EXTRACTION_INSTRUCTION})

    client = Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": content}],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip()

    data = json.loads(raw)
    data.setdefault("source_id", source_id or pdf_path.name)
    data.setdefault("source_type", "intake_form")
    data.setdefault("source_page", 1)  # multi-page doc; citation anchors to the form, page-level provenance added in the overlay step

    return IntakeFormExtraction.model_validate(data)


if __name__ == "__main__":
    import sys

    default_pdf = Path(__file__).resolve().parent / "test_docs" / "John_Doe-Intake-Form.pdf"
    path = sys.argv[1] if len(sys.argv) > 1 else str(default_pdf)
    result = extract_intake_form(path)
    print(result.model_dump_json(indent=2))
