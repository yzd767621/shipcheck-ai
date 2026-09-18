"""ShipCheck AI — web app + REST API.

    uvicorn app:app --port 8000          # then open http://localhost:8000

Environment:
    SHIPCHECK_SOURCE   data folder or inbox server URL      (default ./data)
    SHIPCHECK_DB       SQLite path                          (default ./out/shipcheck.db)
    ANTHROPIC_API_KEY  enables Claude (classification fallback, field finding,
                       scanned-document pre-read, reply drafting)
"""
from __future__ import annotations

import collections
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
from shipcheck import llm  # noqa: E402
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
app = FastAPI(title="ShipCheck AI", version="1.0.0")
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
            res = pipeline.process(e)
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


@app.on_event("startup")
def _startup():
    if store.count() == 0:
        threading.Thread(target=_run_all, daemon=True).start()


# ------------------------------------------------------------------ API
@app.get("/api/health")
def health():
    return {"ok": True, "emails": store.count(), "llm": bool(pipeline.ai), "model": llm.MODEL if pipeline.ai else None,
            "source": SOURCE}


@app.get("/api/status")
def status():
    return {**_job, "llm": bool(pipeline.ai), "model": llm.MODEL if pipeline.ai else None}


@app.post("/api/run")
def run(body: dict | None = None):
    if _job["running"]:
        raise HTTPException(409, "a run is already in progress")
    ids = (body or {}).get("ids")
    threading.Thread(target=_run_all, args=(ids,), daemon=True).start()
    return {"started": True}


def _slim(r: dict) -> dict:
    keys = ("email_id", "from", "subject", "category", "category_confidence", "category_engine", "status",
            "review_reason", "defect_fields", "summary", "needs_human", "reviewed", "uploaded", "attachments")
    return {k: r.get(k) for k in keys}


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
            return {"engine": "claude", "text": pipeline.ai.draft_reply(r)}
        except Exception as exc:
            fallback = _template_reply(r)
            return {"engine": "template", "text": fallback, "warning": f"Claude unavailable ({type(exc).__name__})"}
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
    return FileResponse(ROOT / "web" / "index.html")
