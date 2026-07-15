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

from agent.ingestion.schemas import IntakeFormExtraction, LabReportExtraction

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

_LAB_EXTRACTION_INSTRUCTION = """You are a careful clinical lab-report extractor.
You are given all pages of one laboratory report as images.
Extract what is printed into one JSON object of this shape:

{
  "patient_name": <str>,
  "patient_age": <str or null>,
  "patient_sex": <str or null>,
  "lab_name": <str or null>,
  "referring_physician": <str or null>,
  "sample_type": <str or null>,
  "collection_date": <str or null>,
  "reported_date": <str or null>,
  "interpretation": <str or null>,
  "results": [
    {
      "test_name": <str>,
      "value": <str>,
      "unit": <str or null>,
      "reference_range": <str or null>,
      "abnormal_flag": <"High" | "Low" | "Borderline" | null>,
      "section": <str or null>
    }
  ]
}

Rules:
- Transcribe EXACTLY as printed. Do not infer, normalize, convert units, or invent.
- Keep every value as a string exactly as shown (e.g. "150000", "12.5", "<0.01").
- abnormal_flag: ONLY use the flag the report itself prints next to that row.
  If the report prints no flag for a row, use null. Do NOT compute or guess a
  flag by comparing the value to the reference range -- the lab's own flag is
  the authority.
- section: the panel heading the row appears under (e.g. "HEMOGLOBIN",
  "DIFFERENTIAL WBC COUNT", "BLOOD INDICES"), or null.
- Include EVERY result row on the report, in document order.
- collection_date: use the "Collected on" value as printed.
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


def extract_lab_report(
    pdf_path: str | Path,
    source_id: str | None = None,
) -> LabReportExtraction:
    """Extract a lab-report PDF (all pages) into a validated LabReportExtraction.

    Same contract as extract_intake_form: raises on malformed JSON or schema
    violation rather than coercing. The schema is the source of truth.
    """
    pdf_path = Path(pdf_path)
    pages = _render_all_pages_to_png(pdf_path)

    content: list[dict] = []
    for png in pages:
        b64 = base64.standard_b64encode(png).decode("ascii")
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}
        )
    content.append({"type": "text", "text": _LAB_EXTRACTION_INSTRUCTION})

    client = Anthropic()
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
    )

    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1].lstrip("json").strip()

    data = json.loads(raw)
    data.setdefault("source_id", source_id or pdf_path.name)
    data.setdefault("source_type", "lab_pdf")
    data.setdefault("source_page", 1)

    return LabReportExtraction.model_validate(data)


if __name__ == "__main__":
    import sys

    doc_type = sys.argv[1] if len(sys.argv) > 1 else "intake_form"
    if doc_type not in ("intake_form", "lab_pdf"):
        print("usage: python -m agent.ingestion.extract [intake_form|lab_pdf] [path]")
        raise SystemExit(2)

    defaults = {
        "intake_form": Path(__file__).resolve().parent / "test_docs" / "John_Doe-Intake-Form.pdf",
        "lab_pdf": Path(__file__).resolve().parent / "test_docs" / "CBC_Lab_Report.pdf",
    }
    path = sys.argv[2] if len(sys.argv) > 2 else str(defaults[doc_type])

    if doc_type == "lab_pdf":
        result = extract_lab_report(path)
    else:
        result = extract_intake_form(path)
    print(result.model_dump_json(indent=2))
