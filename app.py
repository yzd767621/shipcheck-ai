"""ShipCheck AI — web app + REST API.

    uvicorn app:app --port 8000          # then open http://localhost:8000

Environment:
    SHIPCHECK_SOURCE   data folder or inbox server URL      (default ./data)
    SHIPCHECK_DB       SQLite path                          (default ./out/shipcheck.db)
    GEMINI_API_KEY     enables Google Gemini (free tier) — or ANTHROPIC_API_KEY for Claude
                       (classification fallback, field finding,
                       scanned-document pre-read, reply drafting)
"""
from __future__ import annotations

import collections
import contextlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "data"))

from loader import Inbox  # noqa: E402
from shipcheck import llm, ocr  # noqa: E402
from shipcheck.pipeline import Pipeline, apply_human_review, now, to_submission  # noqa: E402
from shipcheck.store import Store  # noqa: E402

SOURCE = os.environ.get("SHIPCHECK_SOURCE", str(ROOT / "data"))
UPLOAD_DIR = Path(os.environ.get("SHIPCHECK_UPLOADS", str(ROOT / "out" / "uploads")))
store = Store(os.environ.get("SHIPCHECK_DB", str(ROOT / "out" / "shipcheck.db")))


class UploadAwareInbox(Inbox):
    """The dataset inbox plus emails uploaded through the UI."""

    def read_bytes(self, att_path):
        if att_path.startswith("uploads/"):
            return (UPLOAD_DIR / att_path.split("/", 1)[1]).read_bytes()
        return super().read_bytes(att_path)


inbox = UploadAwareInbox(SOURCE)
pipeline = Pipeline(inbox)
# Background (re)processing of the whole inbox runs on rules + OCR only, so the
# AI model's free quota is kept for live actions: uploads, retries, replies.
AI_IN_BATCH = os.environ.get("SHIPCHECK_AI_BATCH", "0") == "1"
batch_pipeline = pipeline if AI_IN_BATCH else Pipeline(inbox, use_llm=False)
@contextlib.asynccontextmanager
async def _lifespan(_app):
    _startup()
    yield


app = FastAPI(title="ShipCheck AI", version="1.1.0", lifespan=_lifespan)
_job = {"running": False, "done": 0, "total": 0, "started": None, "finished": None}
_job_lock = threading.Lock()


# ------------------------------------------------------------------ batch
def _run_all(ids: list[str] | None = None):
    emails = inbox.emails()
    uploaded = [r for r in store.all() if r.get("uploaded")]
    if ids:
        emails = [e for e in emails if e["email_id"] in ids]
    with _job_lock:
        _job.update(running=True, done=0, total=len(emails), started=now(), finished=None)

    def one(e):
        prev = store.get(e["email_id"])
        if prev and prev.get("reviewed"):          # never overwrite a human decision
            res = prev
        else:
            res = batch_pipeline.process(e)
        store.put(res)
        with _job_lock:
            _job["done"] += 1

    try:
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(one, emails))
        for r in uploaded:
            store.put(r)
        store.log(now(), "*", "batch_run", {"emails": len(emails)})
    finally:
        with _job_lock:
            _job.update(running=False, finished=now())


def _startup():
    # Process anything not yet in the store (first boot, or a run interrupted by a restart).
    def resume():
        try:
            have = {r["email_id"] for r in store.all()}
            missing = [e["email_id"] for e in inbox.emails() if e["email_id"] not in have]
        except Exception:
            return
        if missing:
            _run_all(missing)

    threading.Thread(target=resume, daemon=True).start()


# ------------------------------------------------------------------ API
@app.get("/api/health")
def health():
    return {"ok": True, "emails": store.count(), **_engine(),
            "source": SOURCE}


@app.get("/api/status")
def status():
    return {**_job, **_engine()}


def _engine() -> dict:
    ai = pipeline.ai
    return {"llm": bool(ai), "provider": ai.label if ai else None, "model": ai.model if ai else None,
            "llm_reason": llm.STATUS.get("reason"), "llm_note": getattr(ai, "last_error", None) if ai else None,
            "ai_in_batch": AI_IN_BATCH, "ocr": ocr.available()}


@app.post("/api/ai-check")
def ai_check():
    """Make one tiny real call to the AI model and report exactly what happened."""
    ai = pipeline.ai
    if not ai:
        return {"ok": False, "provider": None, "message": llm.STATUS.get("reason") or "AI model not configured."}
    sample = {"from": "ops@example.com", "subject": "Quick check",
              "body": "Hi, please check the draft BL against the SI and revert with any discrepancy.", "attachments": []}
    try:
        res = ai.classify(sample)
        return {"ok": True, "provider": ai.label, "model": ai.model,
                "message": f"{ai.label} answered: {res.category} ({res.confidence:.0%}) — {res.rationale}"}
    except Exception as exc:
        msg = str(exc)
        hint = ""
        low = msg.lower()
        if "api key not valid" in low or "api_key_invalid" in low or "permission" in low or "401" in msg or "403" in msg:
            hint = " The key was rejected: copy it again from aistudio.google.com/apikey and update GEMINI_API_KEY."
        elif "429" in msg or "quota" in low or "resource_exhausted" in low:
            hint = " Free-tier limit reached; wait a minute and try again."
        elif "404" in msg or "not found" in low:
            hint = " Model not available for this key; set GEMINI_MODEL to a model listed in AI Studio."
        return {"ok": False, "provider": ai.label, "model": ai.model,
                "message": f"{type(exc).__name__}: {msg[:300]}{hint}"}


@app.post("/api/run")
def run(body: dict | None = None):
    if _job["running"]:
        raise HTTPException(409, "a run is already in progress")
    ids = (body or {}).get("ids")
    threading.Thread(target=_run_all, args=(ids,), daemon=True).start()
    return {"started": True}


def _slim(r: dict) -> dict:
    keys = ("email_id", "from", "subject", "category", "category_confidence", "category_engine", "status",
            "review_reason", "defect_fields", "summary", "needs_human", "reviewed", "uploaded", "attachments",
            "resolution")
    out = {k: r.get(k) for k in keys}
    human = [h for h in r.get("history") or [] if h.get("by") != "system"]
    if human:
        last = human[-1]
        out.update(reviewed_by=last.get("by"), reviewed_at=last.get("at"),
                   reviewed_from=(last.get("previous") or {}).get("review_reason"),
                   review_note=(last.get("decision") or {}).get("note") or None)
    return out


@app.get("/api/emails")
def emails():
    return [_slim(r) for r in store.all()]


@app.get("/api/emails/{email_id}")
def email(email_id: str):
    r = store.get(email_id)
    if not r:
        raise HTTPException(404)
    return r


@app.post("/api/emails/{email_id}/retry")
def retry(email_id: str):
    prev = store.get(email_id)
    if not prev:
        raise HTTPException(404)
    e = {k: prev.get(k) for k in ("email_id", "from", "subject", "body", "attachments")}
    res = pipeline.process(e)
    res["uploaded"] = prev.get("uploaded")
    res["history"] = (prev.get("history") or []) + [{"at": now(), "by": "system", "decision": {"action": "retry"}}]
    store.put(res)
    store.log(now(), email_id, "retry", {"status": res["status"]})
    return res


class Review(BaseModel):
    reviewer: str = "reviewer"
    note: str = ""
    category: str | None = None
    status: str | None = None
    si: dict[str, str] = {}
    bl: dict[str, str] = {}
    action: str | None = None        # "close" = keep the finding, wait for the sender


@app.post("/api/emails/{email_id}/review")
def review(email_id: str, body: Review):
    prev = store.get(email_id)
    if not prev:
        raise HTTPException(404)
    res = apply_human_review(prev, body.model_dump())
    store.put(res)
    store.log(now(), email_id, "human_review", body.model_dump())
    return res


@app.post("/api/emails/{email_id}/draft-reply")
def draft_reply(email_id: str):
    r = store.get(email_id)
    if not r:
        raise HTTPException(404)
    if pipeline.ai:
        try:
            return {"engine": pipeline.ai.label, "text": pipeline.ai.draft_reply(r)}
        except Exception as exc:
            fallback = _template_reply(r)
            return {"engine": "template", "text": fallback, "warning": f"{pipeline.ai.label} unavailable ({type(exc).__name__}) — template used"}
    return {"engine": "template", "text": _template_reply(r)}


def _template_reply(r: dict) -> str:
    name = (r.get("from") or "").split("@")[0]
    lines = [f"Subject: RE: {r.get('subject', '')}", "", f"Hi {name},", ""]
    if r.get("status") == "MISMATCH":
        lines.append("We have checked the draft BL against the SI. Please amend the following to match the SI:")
        for c in r.get("comparison") or []:
            if c["status"] == "mismatch":
                lines.append(f"  - {c['label']}: SI {(c['si_raw'] or '').splitlines()[0]}  |  draft BL {(c['bl_raw'] or '').splitlines()[0]}")
    elif r.get("status") == "NEEDS_REVIEW":
        lines.append(f"We could not complete the check: {r.get('review_detail')}")
        lines.append("Could you please resend the correct documents / confirm the missing details?")
    else:
        lines.append("We have checked the draft BL against the SI — all details are in order.")
    lines += ["", "Best regards,", "Shipping Documentation"]
    return "\n".join(lines)


@app.post("/api/upload")
async def upload(subject: str = Form(...), body: str = Form(""), sender: str = Form("demo@example.com"),
                 files: list[UploadFile] = File(default=[])):
    """Drop a new email into the inbox (for live demos / testing messy inputs)."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    n = 1 + sum(1 for r in store.all() if r.get("uploaded"))
    eid = f"upload_{n:03d}"
    atts = []
    for f in files:
        if not f.filename:
            continue
        safe = f"{eid}_{Path(f.filename).name}"
        (UPLOAD_DIR / safe).write_bytes(await f.read())
        atts.append(f"uploads/{safe}")
    e = {"email_id": eid, "from": sender, "subject": subject, "body": body, "attachments": atts}
    res = pipeline.process(e)
    res["uploaded"] = True
    store.put(res)
    store.log(now(), eid, "upload", {"attachments": atts})
    return res


@app.get("/api/attachments/{path:path}")
def attachment(path: str):
    try:
        data = inbox.read_bytes(path)
    except Exception:
        raise HTTPException(404)
    from fastapi.responses import Response
    media = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
    return Response(data, media_type=media, headers={"Content-Disposition": f'inline; filename="{Path(path).name}"'})


@app.get("/api/stats")
def stats():
    rs = store.all()
    cat = collections.Counter(r.get("category") for r in rs)
    st = collections.Counter(r.get("status") for r in rs if r.get("category") == "BL_COMPARISON")
    reasons = collections.Counter(r.get("review_reason") for r in rs if r.get("status") == "NEEDS_REVIEW")
    fields = collections.Counter(f for r in rs for f in (r.get("defect_fields") or []))
    engines = collections.Counter(r.get("category_engine") for r in rs)
    return {"total": len(rs), "categories": cat, "bl_status": st, "review_reasons": reasons,
            "defect_fields": fields, "engines": engines,
            "open_reviews": sum(1 for r in rs if r.get("needs_human")),
            "errors": sum(1 for r in rs if r.get("status") == "ERROR")}


@app.get("/api/audit")
def audit():
    return store.audit()


# Manual-effort assumptions for the time-saved estimate (minutes). Shown in the
# UI next to the number so the claim is transparent and adjustable.
MANUAL_TRIAGE_MIN = float(os.environ.get("SHIPCHECK_TRIAGE_MIN", "1.5"))
MANUAL_CHECK_MIN = float(os.environ.get("SHIPCHECK_CHECK_MIN", "10"))
HUMAN_REVIEW_MIN = float(os.environ.get("SHIPCHECK_REVIEW_MIN", "4"))


def _domain(sender: str) -> str:
    return (sender or "").split("@")[-1].lower() or "unknown"


@app.get("/api/insights")
def insights():
    rs = [r for r in store.all()]
    n = len(rs)
    checked = [r for r in rs if r.get("category") == "BL_COMPARISON" and r.get("status") in ("OK", "MISMATCH")]
    mism = [r for r in checked if r.get("status") == "MISMATCH"]
    review = [r for r in rs if r.get("needs_human")]
    resolved_auto = n - len(review)

    manual_min = n * MANUAL_TRIAGE_MIN + len(checked) * MANUAL_CHECK_MIN
    residual_min = len(review) * HUMAN_REVIEW_MIN
    saved_h = max(0.0, (manual_min - residual_min) / 60)

    # Sender hotspots: who sends drafts that most often disagree with the SI?
    by_dom: dict[str, dict] = {}
    for r in checked:
        d = by_dom.setdefault(_domain(r.get("from")), {"domain": _domain(r.get("from")), "checks": 0, "mismatches": 0,
                                                       "fields": collections.Counter()})
        d["checks"] += 1
        if r["status"] == "MISMATCH":
            d["mismatches"] += 1
            d["fields"].update(r.get("defect_fields") or [])
    hotspots = sorted(by_dom.values(), key=lambda d: (-d["mismatches"], -d["checks"]))
    for d in hotspots:
        d["rate"] = round(d["mismatches"] / d["checks"], 3) if d["checks"] else 0
        d["top_field"] = d["fields"].most_common(1)[0][0] if d["fields"] else None
        d["fields"] = dict(d["fields"])

    # Discrepancy size: how far off are weights and counts?
    weight_deltas, count_deltas = [], []
    for r in mism:
        for c in r.get("comparison") or []:
            if c.get("status") != "mismatch" or c.get("si_norm") is None or c.get("bl_norm") is None:
                continue
            try:
                delta = float(c["bl_norm"]) - float(c["si_norm"])
            except ValueError:
                continue
            (weight_deltas if c["field"] == "gross_weight_kg" else count_deltas if c["field"] == "container_count" else []).append(delta)

    conf = collections.Counter()
    for r in rs:
        c = r.get("category_confidence") or 0
        conf["≥ 90%" if c >= 0.9 else "75–89%" if c >= 0.75 else "50–74%" if c >= 0.5 else "< 50%"] += 1

    fmt = collections.Counter((d.get("format") or "?").upper() for r in rs for d in (r.get("documents") or []))
    return {
        "total": n,
        "bl_checks": sum(1 for r in rs if r.get("category") == "BL_COMPARISON"),
        "compared": len(checked),
        "mismatches": len(mism),
        "mismatch_rate": round(len(mism) / len(checked), 3) if checked else 0,
        "open_reviews": len(review),
        "automation_rate": round(resolved_auto / n, 3) if n else 0,
        "hours_saved": round(saved_h, 1),
        "assumptions": {"triage_min": MANUAL_TRIAGE_MIN, "check_min": MANUAL_CHECK_MIN, "review_min": HUMAN_REVIEW_MIN},
        "categories": collections.Counter(r.get("category") for r in rs),
        "bl_status": collections.Counter(r.get("status") for r in rs if r.get("category") == "BL_COMPARISON"),
        "defect_fields": collections.Counter(f for r in mism for f in (r.get("defect_fields") or [])),
        "review_reasons": collections.Counter(r.get("review_reason") for r in review if r.get("review_reason")),
        "hotspots": hotspots[:8],
        "weight_deltas": sorted(weight_deltas),
        "count_deltas": sorted(count_deltas),
        "confidence": conf,
        "engines": collections.Counter(r.get("category_engine") for r in rs),
        "formats": fmt,
        "reviewed": sum(1 for r in rs if r.get("reviewed")),
    }


@app.get("/api/export/discrepancies.csv")
def export_csv():
    import csv
    import io
    from fastapi.responses import Response

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email_id", "from", "subject", "status", "review_reason", "field", "si_value", "bl_value", "note"])
    for r in store.all():
        if r.get("category") != "BL_COMPARISON" or r.get("status") not in ("MISMATCH", "NEEDS_REVIEW"):
            continue
        rows = [c for c in r.get("comparison") or [] if c.get("status") != "match"]
        if not rows:
            w.writerow([r["email_id"], r.get("from"), r.get("subject"), r.get("status"), r.get("review_reason"), "", "", "",
                        r.get("review_detail") or ""])
        for c in rows:
            first = lambda v: (v or "").splitlines()[0] if v else ""
            w.writerow([r["email_id"], r.get("from"), r.get("subject"), r.get("status"), r.get("review_reason"),
                        c.get("label"), first(c.get("si_raw")), first(c.get("bl_raw")), c.get("note") or c.get("status")])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="discrepancies.csv"'})


@app.get("/report/{email_id}", response_class=HTMLResponse)
def report(email_id: str):
    """Printable one-page discrepancy report (browser Print → Save as PDF)."""
    import html

    r = store.get(email_id)
    if not r:
        raise HTTPException(404)
    e = html.escape
    first = lambda v: e((v or "—").splitlines()[0])
    verdict = {"OK": ("No mismatch detected", "ok"), "MISMATCH": (f"{len(r.get('defect_fields') or [])} field(s) need amendment", "bad"),
               "NEEDS_REVIEW": ("Needs human review", "warn"), "AWAITING_DOCUMENTS": ("Awaiting documents", "info")}.get(
        r.get("status"), (r.get("status") or "", "info"))
    rows = "".join(
        f"<tr class='{ 'bad' if c['status']=='mismatch' else 'warn' if c['status'].startswith('missing') else ''}'>"
        f"<td>{e(c['label'])}</td><td>{first(c.get('si_raw'))}</td><td>{first(c.get('bl_raw'))}</td>"
        f"<td>{'Match' if c['status']=='match' else 'Mismatch' if c['status']=='mismatch' else 'Missing'}"
        f"{'<br><small>'+e(c['note'])+'</small>' if c.get('note') else ''}</td></tr>"
        for c in r.get("comparison") or [])
    hist = "".join(f"<li>{e(h.get('at',''))} · {e(str(h.get('by','')))} · {e(json.dumps(h.get('decision',{}))[:160])}</li>"
                   for h in r.get("history") or [])
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Discrepancy report · {e(email_id)}</title>
<style>
body{{font:13px/1.5 Inter,system-ui,sans-serif;color:#111827;max-width:820px;margin:32px auto;padding:0 24px}}
h1{{font-size:20px;margin:0}} .muted{{color:#6b7280}} .brand{{font-weight:700;color:#4f46e5}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #e5e7eb;padding-bottom:14px;margin-bottom:18px}}
.v{{display:inline-block;padding:6px 12px;border-radius:8px;font-weight:600;margin:6px 0 14px}}
.v.ok{{background:#ecfdf3;color:#067647}} .v.bad{{background:#fef3f2;color:#b42318}} .v.warn{{background:#fffaeb;color:#b54708}} .v.info{{background:#eff4ff;color:#3538cd}}
table{{width:100%;border-collapse:collapse;margin-top:8px}} th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #eaecf0;vertical-align:top}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#6b7280}} tr.bad td{{background:#fef3f2}} tr.warn td{{background:#fffaeb}}
td:nth-child(2),td:nth-child(3){{font-family:ui-monospace,Consolas,monospace;font-size:12px}}
.btn{{border:1px solid #d0d5dd;background:#fff;border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit}}
@media print{{.noprint{{display:none}} body{{margin:0}}}}
</style></head><body>
<div class="top"><div><div class="brand">ShipCheck AI</div><h1>SI vs draft BL discrepancy report</h1>
<div class="muted">{e(r.get('subject',''))}<br>{e(email_id)} · from {e(r.get('from',''))} · checked {e(r.get('processed_at',''))}</div></div>
<button class="btn noprint" onclick="print()">Print / Save PDF</button></div>
<div class="v {verdict[1]}">{e(verdict[0])}</div>
<p>{e(r.get('summary') or '')}{('<br><b>Review:</b> '+e(r.get('review_detail'))) if r.get('review_detail') else ''}</p>
<table><thead><tr><th>Field</th><th>Shipping Instruction (reference)</th><th>Draft Bill of Lading</th><th>Result</th></tr></thead><tbody>{rows or '<tr><td colspan=4 class=muted>No comparison available.</td></tr>'}</tbody></table>
{('<h3>Review history</h3><ul class=muted>'+hist+'</ul>') if hist else ''}
<p class="muted" style="margin-top:24px">Generated by ShipCheck AI · category {e(r.get('category',''))} ({int((r.get('category_confidence') or 0)*100)}% confidence, {e(r.get('category_engine') or '')})</p>
</body></html>"""


@app.get("/api/submission")
def submission():
    rs = [r for r in store.all() if not r.get("uploaded")]
    return JSONResponse(to_submission(rs), headers={"Content-Disposition": 'attachment; filename="submission.json"'})


@app.post("/api/submit")
def submit_for_score():
    """Forward the submission to the organisers' self-evaluation server
    (only when SHIPCHECK_SOURCE is the HTTP inbox server)."""
    if not inbox.is_http:
        raise HTTPException(400, "Self-evaluation needs SHIPCHECK_SOURCE=http://<inbox-server>:8080")
    rs = [r for r in store.all() if not r.get("uploaded")]
    return inbox.submit(to_submission(rs))


# ------------------------------------------------------------------ UI
@app.get("/", response_class=HTMLResponse)
def index():
    # Always revalidate so a redeploy shows up immediately instead of a cached old UI.
    return FileResponse(ROOT / "web" / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"})
