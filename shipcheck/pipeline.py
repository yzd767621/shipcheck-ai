"""End-to-end pipeline: email -> classify -> (parse, extract, compare) -> result.

Every email yields one JSON-serialisable result dict. Nothing fails silently:
an exception while processing an email produces status "ERROR" with the
traceback summary, which the UI surfaces with a Retry button.
"""
from __future__ import annotations

import datetime as dt
import re
import traceback

from . import llm
from .classifier import Classification, RuleClassifier, clean_body
from .compare import compare, compare_field, is_missing
from .fields import DOC_BL, DOC_SI, FIELD_LABELS, FIELDS, detect_doc_type, extract_fields
from .parsers import ParsedDoc, parse_attachment

REVIEW_REASONS = {
    "wrong_doc_type": "An attachment is not an SI / draft BL",
    "missing_attachment": "The SI or the draft BL is not attached",
    "unreadable": "An attachment could not be read (corrupt or scanned image)",
    "missing_value": "A required field is blank in the documents",
    "low_confidence": "The system is not confident in its decision",
}

CLASSIFY_LLM_THRESHOLD = 0.75

# "Please send the draft BL for X for checking" — the documents are expected
# later; this is a follow-up, not a failed comparison.
_AWAITING = re.compile(r"(?:send|share|provide) (?:us |me )?the draft b/?l", re.I)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Pipeline:
    def __init__(self, inbox, use_llm: bool | None = None):
        self.inbox = inbox
        self.rules = RuleClassifier()
        self.ai = llm.get_client() if (use_llm is None or use_llm) else None

    # ------------------------------------------------------------------ API
    def process(self, email: dict) -> dict:
        base = {
            "email_id": email["email_id"],
            "from": email.get("from", ""),
            "subject": email.get("subject", ""),
            "body": email.get("body", ""),
            "attachments": email.get("attachments") or [],
            "processed_at": now(),
            "engine": "hybrid" if self.ai else "rules",
        }
        try:
            base.update(self._process(email))
        except Exception as exc:  # visible failure, retryable from the UI
            base.update({
                "category": base.get("category") or "UNKNOWN",
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
                "error_trace": traceback.format_exc(limit=4),
                "needs_human": True,
            })
        return base

    # ------------------------------------------------------------- internals
    def _load_docs(self, email: dict) -> list[dict]:
        docs = []
        for path in email.get("attachments") or []:
            try:
                data = self.inbox.read_bytes(path)
            except Exception as exc:
                parsed = ParsedDoc(path, path.rsplit(".", 1)[-1], readable=False, issue=f"download failed ({exc})")
            else:
                parsed = parse_attachment(path, data)
            docs.append({"parsed": parsed, "doc_type": detect_doc_type(parsed.lines) if parsed.readable else None})
        return docs

    def _classify(self, email: dict, kinds: list[str]) -> Classification:
        c = self.rules.classify(email, kinds)
        if self.ai and c.confidence < CLASSIFY_LLM_THRESHOLD:
            try:
                ai = self.ai.classify(email, c)
                ai.signals = ai.signals + [f"rules suggested {c.category} ({c.confidence:.0%})"]
                return ai
            except Exception as exc:
                c.signals.append(f"LLM unavailable: {type(exc).__name__}")
        return c

    def _process(self, email: dict) -> dict:
        docs = self._load_docs(email)
        kinds = [d["doc_type"] for d in docs if d["doc_type"]]
        cls = self._classify(email, kinds)
        out = {
            "category": cls.category,
            "category_confidence": cls.confidence,
            "category_engine": cls.engine,
            "category_signals": cls.signals,
            "category_rationale": cls.rationale,
            "status": "OK",
            "review_reason": None,
            "review_detail": None,
            "has_defect": False,
            "defect_fields": [],
            "documents": [],
            "comparison": [],
            "summary": "",
            "needs_human": False,
        }
        if cls.category != "BL_COMPARISON":
            out["status"] = "OK"
            out["summary"] = "Classified only — no document check required."
            out["needs_human"] = cls.confidence < 0.5
            if out["needs_human"]:
                out["review_reason"] = "low_confidence"
                out["review_detail"] = "Category decided with low confidence — please confirm."
            return out
        return self._check_documents(email, docs, out)

    def _check_documents(self, email: dict, docs: list[dict], out: dict) -> dict:
        si = bl = None
        doc_views = []
        for d in docs:
            p: ParsedDoc = d["parsed"]
            view = {
                "path": p.path, "format": p.fmt, "readable": p.readable, "issue": p.issue,
                "doc_type": d["doc_type"], "role": None, "fields": {}, "unknown_labels": [],
                "text": p.text[:4000], "ai_read": None,
            }
            if p.readable:
                ex = extract_fields(p.lines)
                self._llm_fill(p, ex, d["doc_type"])
                view["fields"] = {k: {"value": ex.values[k], "label": ex.labels.get(k), "evidence": ex.evidence.get(k),
                                      "method": ex.method.get(k, "lexicon")} for k in ex.values}
                view["unknown_labels"] = ex.unknown_labels
            elif p.issue == "image_only" and self.ai:
                view["ai_read"] = self._llm_vision(p)
            # role: trust the document header over the file name
            role = d["doc_type"]
            if role is None and p.readable:
                role = DOC_SI if "_SI." in p.path.upper() else DOC_BL if "_BL." in p.path.upper() else None
            view["role"] = role
            if role == DOC_SI and si is None:
                si = view
            elif role == DOC_BL and bl is None:
                bl = view
            doc_views.append(view)
        out["documents"] = doc_views

        # ---------------------------------------------------- escalations
        def review(reason: str, detail: str):
            out.update(status="NEEDS_REVIEW", review_reason=reason, review_detail=detail, needs_human=True)
            out["summary"] = f"Needs review — {detail}"
            return out

        unreadable = [v for v in doc_views if not v["readable"]]
        if unreadable:
            names = ", ".join(f"{v['path'].split('/')[-1]} ({v['issue']})" for v in unreadable)
            hint = " An AI pre-read of the scan is attached for the reviewer to confirm." if any(v["ai_read"] for v in unreadable) else ""
            return review("unreadable", f"cannot read {names}.{hint}")

        wrong = [v for v in doc_views if v["doc_type"] not in (DOC_SI, DOC_BL, None)]
        if wrong:
            names = ", ".join(f"{v['path'].split('/')[-1]} is a {v['doc_type'].replace('_', ' ').title()}" for v in wrong)
            return review("wrong_doc_type", f"{names}, not an SI / draft BL.")
        if len(doc_views) >= 2 and (si is None or bl is None):
            return review("wrong_doc_type", "two documents attached but they are not one SI and one draft BL.")

        if not doc_views:
            if _AWAITING.search(clean_body(email.get("body", ""))):
                out.update(status="AWAITING_DOCUMENTS", needs_human=False)
                out["summary"] = "Draft BL requested for checking — documents not yet received; the check runs when they arrive."
                return out
            return review("missing_attachment", "no SI or draft BL attached to this comparison request.")
        if si is None or bl is None:
            missing = "draft BL" if si is not None else "SI"
            return review("missing_attachment", f"the {missing} is not attached.")

        # ---------------------------------------------------- compare
        si_vals = {k: v["value"] for k, v in si["fields"].items()}
        bl_vals = {k: v["value"] for k, v in bl["fields"].items()}
        cmp = compare(si_vals, bl_vals)
        out["comparison"] = [f.to_dict() | {"label": FIELD_LABELS[f.field]} for f in cmp.fields]
        mism = cmp.mismatches

        if cmp.missing:
            detail = "; ".join(f"{FIELD_LABELS[f.field]} blank in {'SI' if f.status == 'missing_si' else 'BL'}"
                               f" ({(f.si_raw if f.status == 'missing_si' else f.bl_raw) or 'absent'})" for f in cmp.missing)
            review("missing_value", detail + ".")
            if mism:
                out["summary"] += " Also differs: " + ", ".join(FIELD_LABELS[m] for m in mism) + "."
            out["partial_defect_fields"] = mism
            return out

        if mism:
            out.update(status="MISMATCH", has_defect=True, defect_fields=mism)
            out["summary"] = "; ".join(
                f"{FIELD_LABELS[f.field]}: SI {self._short(f.si_raw)} / BL {self._short(f.bl_raw)}"
                for f in cmp.fields if f.status == "mismatch")
            unsure = [f for f in cmp.fields if f.status == "mismatch" and f.confidence < 0.8]
            if unsure:
                out["needs_human"] = True
                out["review_detail"] = "Low-confidence difference: " + ", ".join(f"{FIELD_LABELS[f.field]} ({f.note})" for f in unsure)
        else:
            out["status"] = "OK"
            out["summary"] = "No mismatch detected."
        notes = [f"{FIELD_LABELS[f.field]}: {f.note}" for f in cmp.fields if f.note and f.status == "match"]
        if notes:
            out["notes"] = notes
        return out

    @staticmethod
    def _short(v: str | None) -> str:
        return (v or "—").split("\n")[0].strip()

    # ------------------------------------------------------------ LLM hooks
    def _llm_fill(self, p: ParsedDoc, ex, doc_type):
        """If the lexicon could not locate a field, ask the LLM to find it in
        the document text (it will return null for genuinely blank fields)."""
        missing = [k for k in FIELDS if k not in ex.values]
        if not self.ai or not missing:
            return
        try:
            found = self.ai.extract(p.text, doc_type or "shipping document", missing)
        except Exception:
            return
        for k in missing:
            item = found.get(k)
            if item and item.get("value") and not is_missing(item["value"]):
                ex.values[k] = item["value"]
                ex.labels[k] = item.get("label") or "(located by AI)"
                ex.evidence[k] = item.get("evidence") or item["value"]
                ex.method[k] = "llm"

    def _llm_vision(self, p: ParsedDoc):
        try:
            return self.ai.read_scan(p.image_pages)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- helpers
def apply_human_review(result: dict, decision: dict) -> dict:
    """Apply a reviewer's decision and recompute the outcome.

    decision = {
      "category": optional new category,
      "si": {field: value}, "bl": {field: value}   # corrected / confirmed values
      "status": optional forced status ("OK" | "MISMATCH" | "NEEDS_REVIEW"),
      "reviewer": str, "note": str
    }
    """
    r = dict(result)
    hist = list(r.get("history") or [])
    hist.append({"at": now(), "by": decision.get("reviewer") or "reviewer", "decision": decision,
                 "previous": {k: r.get(k) for k in ("category", "status", "review_reason", "defect_fields")}})
    r["history"] = hist

    if decision.get("category"):
        r["category"] = decision["category"]
        r["category_engine"] = "human"
        r["category_confidence"] = 1.0

    if r["category"] != "BL_COMPARISON":
        r.update(status="OK", review_reason=None, review_detail=None, has_defect=False, defect_fields=[],
                 needs_human=False, summary="Classified only — confirmed by reviewer.")
        return r

    si_vals, bl_vals = {}, {}
    for d in r.get("documents") or []:
        target = si_vals if d.get("role") == DOC_SI else bl_vals if d.get("role") == DOC_BL else None
        if target is not None:
            target.update({k: v["value"] for k, v in (d.get("fields") or {}).items()})
    for k, v in (decision.get("si") or {}).items():
        if v not in (None, ""):
            si_vals[k] = v
    for k, v in (decision.get("bl") or {}).items():
        if v not in (None, ""):
            bl_vals[k] = v

    if si_vals or bl_vals:
        cmp = [compare_field(k, si_vals.get(k), bl_vals.get(k)) for k in FIELDS]
        r["comparison"] = [f.to_dict() | {"label": FIELD_LABELS[f.field]} for f in cmp]
        mism = [f.field for f in cmp if f.status == "mismatch"]
        missing = [f for f in cmp if f.status.startswith("missing")]
        if missing:
            r.update(status="NEEDS_REVIEW", review_reason="missing_value", has_defect=False, defect_fields=[],
                     review_detail="Still blank: " + ", ".join(FIELD_LABELS[f.field] for f in missing))
        elif mism:
            r.update(status="MISMATCH", review_reason=None, review_detail=None, has_defect=True, defect_fields=mism,
                     summary="; ".join(f"{FIELD_LABELS[f.field]}: SI {Pipeline._short(f.si_raw)} / BL {Pipeline._short(f.bl_raw)}"
                                       for f in cmp if f.status == "mismatch"))
        else:
            r.update(status="OK", review_reason=None, review_detail=None, has_defect=False, defect_fields=[],
                     summary="No mismatch detected.")

    if decision.get("status"):
        r["status"] = decision["status"]
        if r["status"] != "NEEDS_REVIEW":
            r["review_reason"] = None
        if r["status"] != "MISMATCH":
            r["has_defect"], r["defect_fields"] = False, []
    r["needs_human"] = r["status"] == "NEEDS_REVIEW"
    r["reviewed"] = True
    if decision.get("note"):
        r["review_note"] = decision["note"]
    return r


def to_submission(results: list[dict]) -> dict:
    sub = {}
    for r in results:
        cat = r.get("category")
        if cat not in ("BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"):
            cat = "BL_COMPARISON" if r.get("attachments") else "GENERAL"
        status = r.get("status")
        reason = r.get("review_reason")
        if cat != "BL_COMPARISON":
            status, reason = "OK", None
        elif status == "ERROR":
            status, reason = "NEEDS_REVIEW", "unreadable"
        elif status == "AWAITING_DOCUMENTS":      # nothing to compare yet, nothing flagged
            status, reason = "OK", None
        elif status != "NEEDS_REVIEW":
            reason = None
        sub[r["email_id"]] = {
            "category": cat,
            "status": status,
            "review_reason": reason,
            "has_defect": bool(status == "MISMATCH" and r.get("defect_fields")),
            "defect_fields": list(r.get("defect_fields") or []) if status == "MISMATCH" else [],
        }
    return sub
