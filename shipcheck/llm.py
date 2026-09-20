"""Generative-AI layer, with two interchangeable providers:

  * Google Gemini (free tier via Google AI Studio)  — GEMINI_API_KEY
  * Anthropic Claude                               — ANTHROPIC_API_KEY

The model is used where rules run out:
  1. classify    — emails the rule engine is unsure about (confidence < 0.75)
  2. extract     — a field whose label the lexicon does not recognise
  3. read_scan   — vision pre-read of scanned pages, shown to the human
                   reviewer as a suggestion (never auto-accepted)
  4. draft_reply — the discrepancy email back to the sender

Every call asks for JSON that matches a schema, so answers are machine-checkable.
Without a key (or when a call fails / hits a free-tier limit) the pipeline
silently falls back to the rules engine — the app keeps working end to end.

Choose explicitly with SHIPCHECK_LLM=gemini|claude|none (default: auto).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time

from .classifier import CATEGORIES, CATEGORY_HELP, Classification, clean_body
from .fields import FIELD_LABELS, FIELDS

CLAUDE_MODEL = os.environ.get("SHIPCHECK_MODEL", "claude-opus-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


# Why the AI model is (not) active — shown in the UI so a missing or mistyped
# key is obvious. Only variable NAMES are ever reported, never values.
STATUS: dict = {"reason": None}


def _env_key(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v and v.strip().strip('"').strip("'").strip():
            return v.strip().strip('"').strip("'").strip()
    return None


def _near_misses() -> list[str]:
    """Env var names that look like an attempt at an AI key but are not exactly right."""
    exact = {"GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    out = []
    for name in os.environ:
        up = name.upper()
        if name not in exact and ("GEMINI" in up or ("GOOGLE" in up and "KEY" in up) or "ANTHROPIC" in up):
            out.append(repr(name))
    return out


def get_client():
    choice = os.environ.get("SHIPCHECK_LLM", "auto").strip().lower()
    if os.environ.get("SHIPCHECK_DISABLE_LLM") == "1" or choice in ("none", "off", "rules"):
        STATUS["reason"] = f"AI model switched off (SHIPCHECK_LLM={choice or 'off'})"
        return None
    gemini_key = _env_key("GEMINI_API_KEY", "GOOGLE_API_KEY")
    claude_key = _env_key("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    order = {"gemini": ["gemini"], "claude": ["claude"]}.get(choice, ["gemini", "claude"])
    errors = []
    for name in order:
        try:
            if name == "gemini" and gemini_key:
                client = GeminiClient(gemini_key)
                STATUS["reason"] = None
                return client
            if name == "claude" and claude_key:
                client = ClaudeClient()
                STATUS["reason"] = None
                return client
        except Exception as exc:
            errors.append(f"{name} failed to start: {type(exc).__name__}: {exc}")
    if errors:
        STATUS["reason"] = "; ".join(errors)
    elif _near_misses():
        STATUS["reason"] = ("No usable key. Found variable(s) " + ", ".join(_near_misses()) +
                            " — the name must be exactly GEMINI_API_KEY (no spaces).")
    elif os.environ.get("GEMINI_API_KEY") is not None:
        STATUS["reason"] = "GEMINI_API_KEY is set but empty."
    else:
        STATUS["reason"] = ("No GEMINI_API_KEY found in this server's environment. If you just added it, "
                            "redeploy so the running server picks it up.")
    return None


# ---------------------------------------------------------------------- shared
class BaseLLM:
    """Task prompts live here; providers only implement `_json`."""

    name = "llm"         # short id stored on results ("gemini" / "claude")
    label = "AI"         # display name
    model = ""

    cooldown_until = 0.0
    last_error: str | None = None

    def _json(self, system: str, parts: list, schema: dict, effort: str = "low") -> dict:
        """Guarded call: after a usage-limit error (HTTP 429) pause the AI for a
        while instead of hammering the API — the rules engine covers meanwhile,
        and the remaining free quota is kept for live, user-triggered actions."""
        wait = self.cooldown_until - time.time()
        if wait > 0:
            raise RuntimeError(f"{self.label} paused after reaching its usage limit; retry in {int(wait) + 1}s")
        try:
            out = self._provider_json(system, parts, schema, effort)
            self.last_error = None
            return out
        except Exception as exc:
            msg = str(exc)
            low = msg.lower()
            if "429" in msg or "resource_exhausted" in low or "quota" in low or "rate limit" in low:
                daily = "perday" in low.replace(" ", "").replace("_", "") or "per day" in low
                self.cooldown_until = time.time() + (600 if daily else 60)
                self.last_error = (f"{self.label} free-tier limit reached at {time.strftime('%H:%M', time.gmtime())} UTC; "
                                   f"using rules + OCR until it resets")
            else:
                self.last_error = f"{type(exc).__name__}: {msg[:160]}"
            raise

    def _provider_json(self, system: str, parts: list, schema: dict, effort: str = "low") -> dict:
        """parts: list of str or ("image/png", bytes). Returns the parsed JSON object."""
        raise NotImplementedError

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
            f"Categories:\n{cats}\n"
            "confidence is a number between 0 and 1. rationale is one short sentence."
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
        data = self._json(system, [user], schema)
        if data.get("category") not in CATEGORIES:
            raise ValueError(f"unexpected category {data.get('category')!r}")
        conf = max(0.0, min(1.0, float(data.get("confidence") or 0)))
        why = data.get("rationale", "")
        return Classification(data["category"], round(conf, 2), [f"{data['category']}: {self.label} — {why}"],
                              engine=self.name, rationale=why)

    # ------------------------------------------------------------ extract
    def extract(self, text: str, doc_type: str, keys: list[str]) -> dict:
        system = (
            "You extract shipment fields from a shipping document. Labels vary between documents "
            "('Port of Loading' = 'Load Port' = 'POL' = 'Origin Terminal'; 'Consignee' = 'To the Order of'). "
            "Return each value exactly as written in the document (first line only for parties), the label it "
            "was under, and the full source line as evidence. Gross weight is the TOTAL gross weight, never net "
            "weight. If a field is absent, blank or a placeholder such as N/A, TBA or '____', return empty "
            "strings for it — never guess."
        )
        item = {"type": "object",
                "properties": {"value": {"type": "string"}, "label": {"type": "string"}, "evidence": {"type": "string"}},
                "required": ["value", "label", "evidence"], "additionalProperties": False}
        schema = {"type": "object", "properties": {k: item for k in keys}, "required": keys, "additionalProperties": False}
        wanted = ", ".join(f"{k} ({FIELD_LABELS[k]})" for k in keys)
        user = f"Document type: {doc_type}\nFind: {wanted}\n\n<document>\n{text[:12000]}\n</document>"
        data = self._json(system, [user], schema)
        return {k: v for k, v in data.items() if k in keys and isinstance(v, dict) and (v.get("value") or "").strip()}

    # ------------------------------------------------------------ vision
    def read_scan(self, pages: list[bytes]) -> dict:
        parts: list = [("image/png", png) for png in pages[:4]]
        parts.append(
            "This is a scanned shipping document. Say whether it is a Shipping Instruction (SI), a draft Bill "
            "of Lading (BL) or something else (OTHER), and transcribe these fields exactly as printed: "
            + ", ".join(FIELDS) + ". Use an empty string where a value is missing or not legible. "
            "legibility is a number between 0 and 1.")
        schema = {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["SI", "BL", "OTHER"]},
                "legibility": {"type": "number"},
                "fields": {"type": "object", "properties": {k: {"type": "string"} for k in FIELDS},
                           "required": FIELDS, "additionalProperties": False},
            },
            "required": ["doc_type", "legibility", "fields"],
            "additionalProperties": False,
        }
        system = "You transcribe scanned logistics documents faithfully. Never invent characters you cannot read."
        data = self._json(system, parts, schema, effort="medium")
        data["fields"] = {k: (v or "").strip() for k, v in (data.get("fields") or {}).items() if k in FIELDS}
        data["engine"] = f"{self.label} vision"
        return data

    # ------------------------------------------------------------ reply
    def draft_reply(self, result: dict) -> str:
        def first(v):
            return (v or "").splitlines()[0] if v else "—"
        rows = [f"- {c['label']}: SI '{first(c.get('si_raw'))}' / BL '{first(c.get('bl_raw'))}' ({c['status']})"
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
        data = self._json(system, [user], schema)
        return f"Subject: {data['subject']}\n\n{data['body']}"


# ---------------------------------------------------------------------- Gemini
def _strip_additional(schema):
    """Gemini's JSON-schema mode does not need `additionalProperties`; drop it."""
    if isinstance(schema, dict):
        return {k: _strip_additional(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_additional(v) for v in schema]
    return schema


class GeminiClient(BaseLLM):
    name = "gemini"
    label = "Gemini"

    def __init__(self, api_key: str, model: str = GEMINI_MODEL):
        from google import genai
        from google.genai import errors, types

        self.types, self.errors = types, errors
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(
            timeout=120_000,
            retry_options=types.HttpRetryOptions(attempts=3, http_status_codes=[500, 502, 503, 504])))
        self.model = model
        self._resolved = False
        self._lock = threading.Lock()

    def _pick_model(self) -> str | None:
        """Configured model not available on this key: choose the best current Flash model."""
        names = []
        for m in self.client.models.list():
            name = (m.name or "").split("/")[-1]
            actions = m.supported_actions or []
            if "flash" in name and "generateContent" in actions and not any(x in name for x in ("image", "tts", "live", "audio", "embedding")):
                names.append(name)
        stable = [n for n in names if "preview" not in n and "exp" not in n] or names
        stable.sort(key=lambda n: ("lite" in n, [-int(x) if x.isdigit() else 0 for x in n.replace("-", ".").split(".")]))
        return stable[0] if stable else None

    def _call(self, contents, config):
        return self.client.models.generate_content(model=self.model, contents=contents, config=config)

    def _provider_json(self, system, parts, schema, effort="low"):
        t = self.types
        contents = [t.Part.from_text(text=p) if isinstance(p, str) else t.Part.from_bytes(data=p[1], mime_type=p[0])
                    for p in parts]
        config = t.GenerateContentConfig(system_instruction=system, response_mime_type="application/json",
                                         response_json_schema=_strip_additional(schema))
        try:
            resp = self._call(contents, config)
        except self.errors.ClientError as exc:
            if exc.code != 404 or self._resolved:
                raise
            with self._lock:
                self._resolved = True
                picked = self._pick_model()
            if not picked:
                raise
            self.model = picked
            resp = self._call(contents, config)
        text = resp.text
        if not text:
            raise RuntimeError(f"empty response from Gemini ({resp.prompt_feedback})")
        return json.loads(text)


# ---------------------------------------------------------------------- Claude
class ClaudeClient(BaseLLM):
    name = "claude"
    label = "Claude"

    def __init__(self, model: str = CLAUDE_MODEL):
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3, timeout=120)
        self.model = model

    def _provider_json(self, system, parts, schema, effort="low"):
        content = []
        for p in parts:
            if isinstance(p, str):
                content.append({"type": "text", "text": p})
            else:
                content.append({"type": "image", "source": {"type": "base64", "media_type": p[0],
                                                             "data": base64.standard_b64encode(p[1]).decode()}})
        params = dict(
            model=self.model,
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        try:
            # On a safety decline, re-run server-side on Anthropic's recommended fallback model.
            resp = self.client.messages.create(**params, extra_headers={"anthropic-beta": FALLBACK_BETA},
                                               extra_body={"fallbacks": "default"})
        except self.anthropic.BadRequestError:
            # Account/model without the fallback beta: the core request still works without it.
            resp = self.client.messages.create(**params)
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined the request")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("model output truncated")
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text)
