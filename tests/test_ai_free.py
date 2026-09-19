"""Tests for the free AI options: local OCR (Tesseract) and the Gemini provider.

OCR tests run only when the Tesseract binary is installed. Gemini is exercised
with a fake transport, so no key or network is needed.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shipcheck import llm, ocr  # noqa: E402
from shipcheck.pipeline import Pipeline, to_submission  # noqa: E402

needs_ocr = pytest.mark.skipif(not ocr.available(), reason="Tesseract not installed")


class FS:
    def read_bytes(self, p):
        return Path(p).read_bytes()


def run_scans(si, bl):
    pipe = Pipeline(FS(), use_llm=False)
    return pipe.process({"email_id": "s", "from": "a@b", "subject": "docs",
                         "body": "Attached SI and draft BL for checking (scanned copies).",
                         "attachments": [str(si), str(bl)]})


# ---------------------------------------------------------------- OCR
@needs_ocr
def test_ocr_reads_scans_but_a_person_confirms():
    d = ROOT / "demo" / "3_scanned_documents"
    if not d.exists():
        pytest.skip("run scripts/make_demo_kit.py first")
    r = run_scans(d / "SI_scan.pdf", d / "BL_scan.pdf")
    assert r["status"] == "NEEDS_REVIEW" and r["review_reason"] == "unreadable"      # never auto-approved
    assert r["preliminary"] and r["preliminary_defects"] == ["container_count"]       # 6 vs 7 containers spotted
    roles = {doc["role"] for doc in r["documents"]}
    assert roles == {"SI", "BL"}                                                      # doc type read from the scan
    assert all(doc["pre_read"]["engine"].startswith("OCR") for doc in r["documents"])
    assert to_submission([r])["s"]["status"] == "NEEDS_REVIEW"


@needs_ocr
def test_ocr_on_dataset_scan():
    r = run_scans(ROOT / "data/attachments/email_512_SI.pdf", ROOT / "data/attachments/email_512_BL.pdf")
    si = next(d for d in r["documents"] if d["role"] == "SI")
    assert si["pre_read"]["fields"]["port_of_discharge"].startswith("TUTICORIN")
    assert r["review_reason"] == "unreadable"


def test_corrupt_pdf_is_not_ocrd():
    r = run_scans(ROOT / "data/attachments/email_511_SI.txt", ROOT / "data/attachments/email_511_BL.pdf")
    assert r["review_reason"] == "unreadable" and not r.get("preliminary")


# ---------------------------------------------------------------- provider choice
def test_provider_selection(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SHIPCHECK_DISABLE_LLM", "SHIPCHECK_LLM"):
        monkeypatch.delenv(k, raising=False)
    assert llm.get_client() is None                                    # no key → rules only
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    c = llm.get_client()
    assert isinstance(c, llm.GeminiClient) and c.label == "Gemini"
    monkeypatch.setenv("SHIPCHECK_LLM", "none")
    assert llm.get_client() is None


# ---------------------------------------------------------------- Gemini request / response handling
class FakeModels:
    def __init__(self, reply, fail_first_with=None):
        self.reply, self.fail = reply, fail_first_with
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append(SimpleNamespace(model=model, contents=contents, config=config))
        if self.fail:
            exc, self.fail = self.fail, None
            raise exc
        return SimpleNamespace(text=json.dumps(self.reply), prompt_feedback=None)

    def list(self):
        return [SimpleNamespace(name="models/gemini-9.0-flash", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/gemini-9.0-flash-lite", supported_actions=["generateContent"]),
                SimpleNamespace(name="models/text-embedding", supported_actions=["embedContent"])]


def gemini_with(models):
    c = llm.GeminiClient("test-key")
    c.client = SimpleNamespace(models=models)
    return c


def test_gemini_classify_uses_json_schema_mode():
    m = FakeModels({"category": "SI_REQUEST", "confidence": 0.9, "rationale": "gives new SI details"})
    c = gemini_with(m)
    res = c.classify({"from": "x", "subject": "hi", "body": "Please prepare the SI", "attachments": []})
    assert res.category == "SI_REQUEST" and res.engine == "gemini"
    cfg = m.calls[0].config
    assert cfg.response_mime_type == "application/json"
    assert "additionalProperties" not in json.dumps(cfg.response_json_schema)


def test_gemini_extract_and_scan_parts():
    m = FakeModels({"port_of_loading": {"value": "SINGAPORE", "label": "Origin Terminal", "evidence": "Origin Terminal: SINGAPORE"}})
    c = gemini_with(m)
    out = c.extract("Origin Terminal: SINGAPORE", "SI", ["port_of_loading"])
    assert out["port_of_loading"]["value"] == "SINGAPORE"
    m2 = FakeModels({"doc_type": "BL", "legibility": 0.8, "fields": {k: "" for k in llm.FIELDS}})
    c2 = gemini_with(m2)
    scan = c2.read_scan([b"\x89PNG fake"])
    assert scan["doc_type"] == "BL" and scan["engine"] == "Gemini vision"
    assert m2.calls[0].contents[0].inline_data.mime_type == "image/png"


def test_gemini_picks_available_model_on_404():
    from google.genai import errors
    m = FakeModels({"subject": "RE", "body": "ok"}, fail_first_with=errors.ClientError(404, {"error": {"message": "not found"}}))
    c = gemini_with(m)
    c.draft_reply({"subject": "s", "from": "f", "status": "OK", "summary": "", "comparison": []})
    assert c.model == "gemini-9.0-flash" and m.calls[-1].model == "gemini-9.0-flash"
