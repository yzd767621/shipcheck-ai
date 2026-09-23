"""Live mailbox connector: MIME parsing, polling, dedupe and replies, against a
fake IMAP / SMTP server (no network, no real account).

    python -m pytest -q tests/test_mailbox.py
"""
import imaplib
import json
import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

from shipcheck.mailbox import MailboxPoller, MailConfig, _split_subject, mail_id, parse_message  # noqa: E402

DATA = ROOT / "data"
PNG_LOGO = b"\x89PNG\r\n\x1a\n" + b"\0" * 900          # a small signature logo


def dataset_mail(email_id="email_004", html=False, logo=True, sender="ops@partner-forwarder.com"):
    e = json.loads((DATA / "inbox" / f"{email_id}.json").read_text(encoding="utf-8"))
    m = EmailMessage()
    m["From"] = f"Ops Team <{sender}>"
    m["To"] = "shipcheck.demo@gmail.com"
    m["Subject"] = e["subject"]
    m["Date"] = "Tue, 22 Sep 2026 09:15:00 +0800"
    m["Message-ID"] = f"<{email_id}.{sender.split('@')[0]}@mail.test>"
    if html:
        m.set_content("<html><body><p>Hi team,</p><p>" + e["body"].replace("\n", "<br>") + "</p></body></html>", subtype="html")
    else:
        m.set_content(e["body"])
    if logo:
        m.add_attachment(PNG_LOGO, maintype="image", subtype="png", filename="image001.png")
    for a in e["attachments"]:
        p = DATA / a
        m.add_attachment(p.read_bytes(), maintype="application", subtype="octet-stream", filename=p.name)
    return e, m.as_bytes()


class FakeIMAP:
    mailbox: dict = {}
    password = "goodpassword"
    logins = 0

    def __init__(self, host, port):
        self.host = host

    def login(self, user, pw):
        FakeIMAP.logins += 1
        if pw != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")

    def select(self, folder, readonly=False):
        assert readonly, "the connector must never modify the mailbox"
        return ("OK", [str(len(self.mailbox)).encode()]) if folder == '"INBOX"' else ("NO", [b"no such folder"])

    def response(self, code):
        return ("OK", [b"42"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.mailbox)]
        if cmd == "FETCH":
            assert "PEEK" in args[1], "fetch must not mark messages as read"
            raw = self.mailbox[args[0]]
            return "OK", [(b"1 (UID " + args[0] + b" BODY[] {%d}" % len(raw), raw), b")"]
        raise AssertionError(cmd)

    def logout(self):
        pass


class FakeSMTP:
    sent = []

    def __init__(self, host, port):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def cfg(**kw):
    base = dict(user="shipcheck.demo@gmail.com", password="goodpassword", send_enabled=True, smtp_host="smtp.gmail.com")
    base.update(kw)
    return MailConfig(**base)


@pytest.fixture
def pipeline():
    from loader import Inbox
    from shipcheck.pipeline import Pipeline

    class MailInbox(Inbox):
        def __init__(self, src, mail_dir):
            super().__init__(src)
            self.mail_dir = mail_dir

        def read_bytes(self, p):
            if p.startswith("mail/"):
                return (self.mail_dir / p.split("/", 1)[1]).read_bytes()
            return super().read_bytes(p)

    tmp = Path(tempfile.mkdtemp())
    inbox = MailInbox(str(DATA), tmp)
    return Pipeline(inbox, use_llm=False), tmp


def make_poller(pipe, tmp, config=None):
    store = {}

    def on_email(e, meta):
        r = pipe.process(e) | {"origin": "mail", "mail": meta}
        store[r["email_id"]] = r
        return r

    p = MailboxPoller(config or cfg(), tmp, on_email, known=lambda i: i in store,
                      imap_factory=FakeIMAP, smtp_factory=FakeSMTP)
    return p, store


# ------------------------------------------------------------------ parsing
def test_parse_keeps_documents_and_drops_signature_logo():
    e, raw = dataset_mail()
    p = parse_message(raw)
    assert p["from"] == "ops@partner-forwarder.com" and p["from_name"] == "Ops Team"
    assert p["subject"] == e["subject"]
    assert sorted(n for n, _ in p["files"]) == sorted(Path(a).name for a in e["attachments"])
    assert p["ignored"] == [{"name": "image001.png", "reason": "inline / signature image"}]
    assert p["meta"]["date"] == "2026-09-22T01:15:00+00:00"


def test_parse_html_only_body():
    _, raw = dataset_mail(html=True)
    body = parse_message(raw)["body"]
    assert "<" not in body and "Hi team," in body


def test_parse_duplicate_and_unsafe_names():
    m = EmailMessage()
    m["From"] = "a@b.com"
    m["Subject"] = "x"
    m.set_content("see attached")
    m.add_attachment(b"SHIPPING INSTRUCTION", maintype="text", subtype="plain", filename="../../etc/SI.txt")
    m.add_attachment(b"SHIPPING INSTRUCTION 2", maintype="text", subtype="plain", filename="SI.txt")
    m.add_attachment(b"sig", maintype="application", subtype="pkcs7-signature", filename="smime.p7s")
    p = parse_message(m.as_bytes())
    assert [n for n, _ in p["files"]] == ["SI.txt", "SI_2.txt"]
    assert p["ignored"][0]["name"] == "smime.p7s"


def test_mail_id_is_stable_and_sortable():
    _, raw = dataset_mail()
    a, b = mail_id(parse_message(raw)), mail_id(parse_message(raw))
    assert a == b and a.startswith("mail_20260922_011500_")


def test_allow_list():
    c = cfg(allow=["averis.com", "boss@partner.com"])
    assert c.allowed("ops@averis.com") and c.allowed("x@sg.averis.com") and c.allowed("boss@partner.com")
    assert not c.allowed("spam@evil.com") and not c.allowed("x@notaveris.com")
    assert cfg().allowed("anyone@anywhere.com")


def test_env_config_strips_app_password_spaces(monkeypatch):
    monkeypatch.setenv("SHIPCHECK_IMAP_USER", "me@outlook.com")
    monkeypatch.setenv("SHIPCHECK_IMAP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("SHIPCHECK_IMAP_HOST", "imap.outlook.com")
    c = MailConfig.from_env()
    assert c.configured and c.password == "abcdefghijklmnop" and c.smtp_host == "smtp.outlook.com"


# ------------------------------------------------------------------ polling
def test_live_mail_gives_same_result_as_dataset(pipeline):
    pipe, tmp = pipeline
    ids = ["email_004", "email_005"]
    FakeIMAP.mailbox = {str(i + 1).encode(): dataset_mail(x)[1] for i, x in enumerate(ids)}
    poller, store = make_poller(pipe, tmp)
    new = poller.poll_once()
    assert len(new) == 2 and poller.state["connected"] and poller.state["last_error"] is None
    for eid, r in zip(ids, new):
        ref = pipe.process(json.loads((DATA / "inbox" / f"{eid}.json").read_text(encoding="utf-8")))
        assert (r["category"], r["status"], r["defect_fields"]) == (ref["category"], ref["status"], ref["defect_fields"])
        assert all(a.startswith("mail/") for a in r["attachments"]) and len(r["attachments"]) == len(ref["attachments"])


def test_poll_does_not_duplicate_after_restart(pipeline):
    pipe, tmp = pipeline
    FakeIMAP.mailbox = {b"1": dataset_mail()[1]}
    poller, store = make_poller(pipe, tmp)
    assert len(poller.poll_once()) == 1
    assert poller.poll_once() == []                                     # same process: UID already seen
    again = MailboxPoller(cfg(), tmp, poller.on_email, known=lambda i: i in store, imap_factory=FakeIMAP)
    assert again.poll_once() == [] and len(store) == 1                  # after a restart: known by Message-ID


def test_allow_list_skips_unknown_senders(pipeline):
    pipe, tmp = pipeline
    FakeIMAP.mailbox = {b"1": dataset_mail(sender="stranger@random.net")[1], b"2": dataset_mail("email_005")[1]}
    poller, store = make_poller(pipe, tmp, cfg(allow=["partner-forwarder.com"]))
    new = poller.poll_once()
    assert len(new) == 1 and poller.state["skipped"] == 1


def test_bad_password_is_explained(pipeline):
    pipe, tmp = pipeline
    poller, _ = make_poller(pipe, tmp, cfg(password="wrong"))
    assert poller.poll_once() == []
    assert not poller.state["connected"] and "app password" in poller.state["last_error"]


def test_unconfigured_does_nothing(pipeline):
    pipe, tmp = pipeline
    poller, _ = make_poller(pipe, tmp, MailConfig())
    assert poller.poll_once() == [] and not poller.state["configured"]


# ------------------------------------------------------------------ replies
def test_send_reply_threads_with_original(pipeline):
    pipe, tmp = pipeline
    FakeIMAP.mailbox = {b"1": dataset_mail()[1]}
    poller, _ = make_poller(pipe, tmp)
    r = poller.poll_once()[0]
    FakeSMTP.sent.clear()
    sent = poller.send_reply(r, "Subject: RE: amendment needed\n\nHi Ops Team,\nPlease amend.")
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "ops@partner-forwarder.com" and msg["Subject"] == "RE: amendment needed"
    assert msg["In-Reply-To"] == "<email_004.ops@mail.test>"
    assert msg.get_content().startswith("Hi Ops Team") and sent["to"] == msg["To"]
    # the sent reply, if it lands in the same folder, is not processed as a new email
    assert poller._handle(msg.as_bytes()) is None


def test_send_reply_refused_when_disabled(pipeline):
    pipe, tmp = pipeline
    poller, _ = make_poller(pipe, tmp, cfg(send_enabled=False))
    with pytest.raises(PermissionError):
        poller.send_reply({"from": "a@b.com", "subject": "x"}, "hello")


def test_split_subject():
    assert _split_subject("hello", "Draft BL") == ("Re: Draft BL", "hello\n")
    assert _split_subject("hello", "RE: Draft BL")[0] == "RE: Draft BL"


# ------------------------------------------------------------------ API
@pytest.fixture
def api(pipeline, monkeypatch):
    if "app" not in sys.modules:                  # keep the test store out of ./out
        tmp = Path(tempfile.mkdtemp())
        os.environ.setdefault("SHIPCHECK_DB", str(tmp / "t.db"))
        os.environ.setdefault("SHIPCHECK_UPLOADS", str(tmp / "uploads"))
        os.environ.setdefault("SHIPCHECK_DISABLE_LLM", "1")
    import app as appmod
    from fastapi.testclient import TestClient

    FakeIMAP.mailbox = {b"1": dataset_mail("email_005")[1]}
    poller = MailboxPoller(cfg(), appmod.MAIL_DIR, appmod._on_mail, known=lambda i: appmod.store.get(i) is not None,
                           imap_factory=FakeIMAP, smtp_factory=FakeSMTP)
    monkeypatch.setattr(appmod, "mailbox", poller)
    return TestClient(appmod.app), appmod        # no `with`: skip the 520-email startup batch


def test_api_check_review_and_send(api):
    client, appmod = api
    d = client.post("/api/mailbox/check").json()
    assert len(d["new"]) == 1 and d["connected"]
    eid = d["new"][0]["email_id"]
    row = next(r for r in client.get("/api/emails").json() if r["email_id"] == eid)
    assert row["origin"] == "mail" and row["received_at"]
    full = client.get(f"/api/emails/{eid}").json()
    assert full["mail"]["message_id"] and len(full["documents"]) == 2
    assert client.get(f"/api/attachments/{full['attachments'][0]}").status_code == 200
    assert client.post(f"/api/emails/{eid}/retry").json()["origin"] == "mail"     # retry keeps the mail metadata
    assert eid not in client.get("/api/submission").json()                      # never scored

    FakeSMTP.sent.clear()
    r = client.post(f"/api/emails/{eid}/send-reply", json={"text": "Subject: RE: x\n\nAll good.", "reviewer": "zd"}).json()
    assert r["replies"][0]["by"] == "zd" and len(FakeSMTP.sent) == 1
    assert any(a["action"] == "reply_sent" for a in client.get("/api/audit").json())


def test_api_send_only_for_live_mail(api):
    client, _ = api
    assert client.post("/api/emails/email_004/send-reply", json={"text": "hi"}).status_code in (400, 404)


def test_api_attachment_path_traversal_blocked(api):
    client, _ = api
    assert client.get("/api/attachments/mail/..%2F..%2Fapp.py").status_code == 404
