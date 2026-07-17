"""Generate degraded ("scanned") copies of a clean PDF, for testing extraction
against the messy inputs a clinic front desk actually uploads.

The PRD's scenario is a SCANNED lab PDF, and its stated hard problem is that a
VLM "can read a scanned form, but it can also hallucinate field labels or
overstate confidence." Our fixtures are clean, digitally-generated PDFs -- they
do not test that at all. This script produces fixtures that do.

Each profile mimics a different real-world failure:
  light   -- a decent office scanner: slight skew, mild noise
  medium  -- a tired photocopier: lower DPI, visible grain, contrast loss
  harsh   -- a phone photo in bad light: heavy skew, blur, JPEG artifacts
  faxed   -- 1-bit threshold, the classic "we faxed it over" artifact

Run:
    python -m agent.ingestion.degrade agent/ingestion/test_docs/CBC_Lab_Report.pdf
Writes CBC_Lab_Report__light.pdf, __medium.pdf, etc. alongside the source.
"""
from __future__ import annotations

import io
from pathlib import Path

import fitz  # pymupdf
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

# dpi, rotation_deg, blur_radius, noise_sigma, jpeg_quality, contrast, threshold
_PROFILES: dict[str, dict] = {
    "light":  dict(dpi=150, rotate=0.4, blur=0.3, noise=4,  jpeg=85, contrast=1.0, threshold=None),
    "medium": dict(dpi=110, rotate=1.2, blur=0.7, noise=10, jpeg=60, contrast=0.85, threshold=None),
    "harsh":  dict(dpi=90,  rotate=2.5, blur=1.2, noise=18, jpeg=35, contrast=0.7,  threshold=None),
    "faxed":  dict(dpi=100, rotate=0.8, blur=0.5, noise=6,  jpeg=70, contrast=1.0,  threshold=150),
}


def _degrade_image(img: Image.Image, p: dict) -> Image.Image:
    img = img.convert("L")  # scanners are grayscale

    if p["rotate"]:
        img = img.rotate(p["rotate"], resample=Image.BICUBIC, fillcolor=255, expand=False)

    if p["contrast"] != 1.0:
        img = ImageEnhance.Contrast(img).enhance(p["contrast"])

    if p["blur"]:
        img = img.filter(ImageFilter.GaussianBlur(radius=p["blur"]))

    if p["noise"]:
        arr = np.asarray(img, dtype=np.float32)
        arr += np.random.normal(0, p["noise"], arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    if p["threshold"] is not None:
        img = img.point(lambda x: 255 if x > p["threshold"] else 0, mode="1").convert("L")

    if p["jpeg"]:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=p["jpeg"])
        buf.seek(0)
        img = Image.open(buf).convert("L")

    return img


def degrade_pdf(src: Path, profile: str) -> Path:
    p = _PROFILES[profile]
    doc = fitz.open(src)
    out = fitz.open()
    try:
        for i in range(doc.page_count):
            pix = doc.load_page(i).get_pixmap(dpi=p["dpi"])
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img = _degrade_image(img, p)

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            page = out.new_page(width=pix.width, height=pix.height)
            page.insert_image(fitz.Rect(0, 0, pix.width, pix.height), stream=buf.getvalue())

        dest = src.with_name(f"{src.stem}__{profile}.pdf")
        out.save(dest)
        return dest
    finally:
        doc.close()
        out.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python -m agent.ingestion.degrade <path.pdf> [profile ...]")
        raise SystemExit(2)

    src = Path(sys.argv[1])
    profiles = sys.argv[2:] or list(_PROFILES)
    np.random.seed(42)  # reproducible fixtures -- a test fixture that changes every run is not a fixture
    for prof in profiles:
        dest = degrade_pdf(src, prof)
        print(f"{prof:8} -> {dest.name}  ({dest.stat().st_size:,} bytes)")
