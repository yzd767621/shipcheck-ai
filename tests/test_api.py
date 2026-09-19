"""API tests: run the web app against the bundled dataset in a temp store.

    python -m pytest -q
"""
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("store")
    os.environ["SHIPCHECK_DB"] = str(tmp / "t.db")
    os.environ["SHIPCHECK_UPLOADS"] = str(tmp / "uploads")
    os.environ["SHIPCHECK_DISABLE_LLM"] = "1"
    from fastapi.testclient import TestClient
    import app as appmod

    with TestClient(appmod.app) as c:
        for _ in range(120):                       # startup processes the inbox in the background
            s = c.get("/api/status").json()
            if c.get("/api/health").json()["emails"] >= 520 and not s["running"]:
                break
            time.sleep(0.5)
        yield c


def test_inbox_processed(client):
    assert client.get("/api/health").json()["emails"] == 520


def test_insights(client):
    d = client.get("/api/insights").json()
    assert d["total"] == 520 and d["mismatches"] == 46 and d["open_reviews"] == 20
    assert d["hours_saved"] > 0 and 0 < d["automation_rate"] <= 1
    assert d["hotspots"] and {"domain", "checks", "mismatches", "rate"} <= set(d["hotspots"][0])


def test_csv_and_report(client):
    csv = client.get("/api/export/discrepancies.csv").text.splitlines()
    assert csv[0].startswith("email_id,") and len(csv) > 46
    html = client.get("/report/email_025").text
    assert "discrepancy report" in html and "Port of discharge" in html


def test_review_then_audit(client):
    r = client.post("/api/emails/email_516/review", json={"reviewer": "t", "si": {"gross_weight_kg": "235,550 KG"}}).json()
    assert r["status"] == "OK" and r["reviewed"]
    assert any(a["action"] == "human_review" and a["email_id"] == "email_516" for a in client.get("/api/audit").json())
    assert client.get("/api/insights").json()["open_reviews"] == 19


def test_upload(client):
    data = (ROOT / "data" / "attachments" / "email_025_SI.txt").read_bytes()
    bl = (ROOT / "data" / "attachments" / "email_025_BL.txt").read_bytes()
    r = client.post("/api/upload", data={"subject": "check", "body": "Please check the draft BL against the SI."},
                    files=[("files", ("a_SI.txt", data)), ("files", ("a_BL.txt", bl))]).json()
    assert r["status"] == "MISMATCH" and set(r["defect_fields"]) == {"port_of_discharge", "container_count"}
    assert len(client.get("/api/submission").json()) == 520          # uploads never leak into the scoring export


def test_reviewed_cases_are_kept_with_reviewer(client):
    r = client.post("/api/emails/email_517/review",
                    json={"reviewer": "ana", "note": "ports confirmed by phone",
                          "si": {"port_of_loading": "SINGAPORE", "port_of_discharge": "CALLAO, PERU"}}).json()
    assert r["reviewed"] and not r["needs_human"]
    row = next(x for x in client.get("/api/emails").json() if x["email_id"] == "email_517")
    assert row["reviewed"] and row["reviewed_by"] == "ana" and row["reviewed_from"] == "missing_value"
    assert row["review_note"] == "ports confirmed by phone" and row["reviewed_at"]


def test_ai_check_reports_reason_without_key(client):
    d = client.post("/api/ai-check").json()
    assert d["ok"] is False and d["message"]
    assert "llm_reason" in client.get("/api/status").json()
