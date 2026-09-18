"""Claude integration (Anthropic API).

Claude is used where rules run out:
  1. classify   — emails the rule engine is unsure about (confidence < 0.75)
  2. extract    — a field whose label the lexicon does not recognise
  3. read_scan  — vision pre-read of image-only PDFs, shown to the human
                  reviewer as a suggestion (never auto-accepted)
  4. draft_reply— the discrepancy email back to the sender

All calls use structured JSON outputs so responses are machine-checkable.
If no credentials are configured, get_client() returns None and the pipeline
runs rules-only — the app still works end to end.
"""
from __future__ import annotations

import base64
import json
import os

from .classifier import CATEGORIES, CATEGORY_HELP, Classification, clean_body
from .fields import FIELD_LABELS, FIELDS

MODEL = os.environ.get("SHIPCHECK_MODEL", "claude-opus-5")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def get_client():
    if os.environ.get("SHIPCHECK_DISABLE_LLM") == "1":
        return None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        return ClaudeClient()
    except Exception:
        return None


class ClaudeClient:
    def __init__(self, model: str = MODEL):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3, timeout=120)
        self.model = model

    # ------------------------------------------------------------ core call
    def _json(self, system: str, content, schema: dict, max_tokens: int = 4000, effort: str = "low") -> dict:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            # On a safety decline, re-run server-side on Anthropic's recommended fallback model.
            extra_headers={"anthropic-beta": FALLBACK_BETA},
            extra_body={"fallbacks": "default"},
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("model output truncated")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)

    # ------------------------------------------------------------ classify
    def classify(self, email: dict, hint: Classification | None = None) -> Classification:
        cats = "\n".join(f"- {c}: {CATEGORY_HELP[c]}" for c in CATEGORIES)
        system = (
            "You triage a shipping-documentation team's shared inbox. Classify the email by what the "
            "sender is asking this team to do in the NEWEST message. Subjects are often recycled from old "
            "threads (e.g. 'RE_ TO CONFIRM DOCS') and can be misleading — decide from the body and attachments.\n"
            "Important distinctions:\n"
            "- BL_COMPARISON for anything in the draft-BL checking workflow: asking to check/compare a draft BL "
            "against an SI (even if an attachment is missing or wrong), or chasing the draft BL so it can be checked.\n"
            "- SI_REQUEST when the sender provides details for a new Shipping Instruction.\n"
            "- INVOICE_QUERY for invoices, charges, D&D, GR, cancellations.\n"
            f"Categories:\n{cats}"
        )
        atts = email.get("attachments") or []
        user = (
            f"From: {email.get('from', '')}\nSubject: {email.get('subject', '')}\n"
            f"Attachments: {', '.join(a.split('/')[-1] for a in atts) or 'none'}\n\n"
            f"Body (newest message only):\n{clean_body(email.get('body', ''))[:3000]}"
        )
        if hint:
            user += f"\n\n(A rule engine guessed {hint.category} with {hint.confidence:.0%} confidence.)"
        schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": CATEGORIES},
                "confidence": {"type": "number"},
                "rationale": {"type": "string"},
            },
            "required": ["category", "confidence", "rationale"],
            "additionalProperties": False,
        }
        data = self._json(system, user, schema, max_tokens=1500)
        conf = max(0.0, min(1.0, float(data["confidence"])))
        return Classification(data["category"], round(conf, 2), [f"{data['category']}: Claude — {data['rationale']}"],
                              engine="claude", rationale=data["rationale"])

    # ------------------------------------------------------------ extract
    def extract(self, text: str, doc_type: str, keys: list[str]) -> dict:
        system = (
            "You extract shipment fields from a shipping document. Labels vary between documents "
            "('Port of Loading' = 'Load Port' = 'POL'; 'Consignee' = 'To the Order of'). Return the value "
            "exactly as written in the document (first line only for parties). Gross weight is the TOTAL "
            "gross weight, never net weight. If a field is absent, blank or a placeholder such as N/A, TBA or "
            "'____', return null — never guess."
        )
        props = {
            k: {"anyOf": [{"type": "null"}, {
                "type": "object",
                "properties": {"value": {"type": "string"}, "label": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["value", "label", "evidence"], "additionalProperties": False}]}
            for k in keys
        }
        schema = {"type": "object", "properties": props, "required": keys, "additionalProperties": False}
        wanted = ", ".join(f"{k} ({FIELD_LABELS[k]})" for k in keys)
        user = f"Document type: {doc_type}\nFind: {wanted}\n\n<document>\n{text[:12000]}\n</document>"
        return self._json(system, user, schema, max_tokens=3000)

    # ------------------------------------------------------------ vision
    def read_scan(self, pages: list[bytes]) -> dict:
        content = []
        for png in pages[:4]:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": base64.standard_b64encode(png).decode()}})
        content.append({"type": "text", "text": (
            "This is a scanned shipping document. Say whether it is a Shipping Instruction (SI), a draft Bill "
            "of Lading (BL) or something else, and transcribe these fields exactly as printed: "
            + ", ".join(FIELDS) + ". Use null where a value is not legible. Rate legibility 0-1.")})
        field_schema = {"anyOf": [{"type": "null"}, {"type": "string"}]}
        schema = {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["SI", "BL", "OTHER"]},
                "legibility": {"type": "number"},
                "fields": {"type": "object", "properties": {k: field_schema for k in FIELDS},
                           "required": FIELDS, "additionalProperties": False},
            },
            "required": ["doc_type", "legibility", "fields"],
            "additionalProperties": False,
        }
        system = "You transcribe scanned logistics documents faithfully. Never invent characters you cannot read."
        return self._json(system, content, schema, max_tokens=3000, effort="medium")

    # ------------------------------------------------------------ reply
    def draft_reply(self, result: dict) -> str:
        rows = [f"- {c['label']}: SI '{(c.get('si_raw') or '').splitlines()[0] if c.get('si_raw') else '—'}' / "
                f"BL '{(c.get('bl_raw') or '').splitlines()[0] if c.get('bl_raw') else '—'}' ({c['status']})"
                for c in result.get("comparison") or []]
        system = ("You write short, polite, professional emails for a shipping documentation desk. "
                  "Plain text, no markdown, under 180 words. Only state facts given to you.")
        user = (f"Original subject: {result.get('subject')}\nSender: {result.get('from')}\n"
                f"Outcome: {result.get('status')} — {result.get('summary')}\n"
                f"Review reason: {result.get('review_reason') or 'n/a'}\nField check (SI is the reference):\n"
                + "\n".join(rows) + "\n\nWrite the reply asking for the draft BL to be amended to match the SI "
                "(or, if NEEDS_REVIEW, asking for what is missing; if OK, confirming the draft BL is in order).")
        schema = {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                  "required": ["subject", "body"], "additionalProperties": False}
        data = self._json(system, user, schema, max_tokens=2000)
        return f"Subject: {data['subject']}\n\n{data['body']}"
