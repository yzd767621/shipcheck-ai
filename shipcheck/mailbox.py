"""Live mailbox connector: read real emails over IMAP, reply over SMTP.

Works with any IMAP mailbox (Gmail, Outlook / Microsoft 365, Zoho, ...). For
Gmail, turn on 2-Step Verification and create an *app password*
(myaccount.google.com/apppasswords); the normal account password is refused.

    SHIPCHECK_IMAP_USER      mailbox address                     (enables the connector)
    SHIPCHECK_IMAP_PASSWORD  app password
    SHIPCHECK_IMAP_HOST      default imap.gmail.com
    SHIPCHECK_IMAP_FOLDER    default INBOX (a Gmail label such as "ShipCheck" works too)
    SHIPCHECK_MAIL_POLL      seconds between checks, default 60
    SHIPCHECK_MAIL_DAYS      only look at mail from the last N days, default 7
    SHIPCHECK_MAIL_MAX       newest N messages per check, default 25
    SHIPCHECK_MAIL_ALLOW     optional allow-list: "averis.com, ops@partner.com"
    SHIPCHECK_SMTP_SEND      "1" lets a reviewer send the drafted reply
    SHIPCHECK_SMTP_HOST      default derived from the IMAP host (imap.x -> smtp.x)

The mailbox is only read (BODY.PEEK, read-only SELECT): nothing is deleted,
moved or marked as read. Each message becomes the same email dict the
dataset loader produces, so it runs through the unchanged pipeline.
"""
from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import hashlib
import html
import imaplib
import os
import re
import smtplib
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

# Attachments that are never shipping documents (signatures, calendar invites, S/MIME).
_NOISE_EXT = {"p7s", "p7m", "ics", "vcf", "asc", "sig"}
_NOISE_NAMES = {"winmail.dat", "smime.p7s"}
_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"}
SIGNATURE_IMAGE_MAX = 20_000        # bytes; logos in signatures are small, scanned pages are not
ATTACHMENT_MAX = 15_000_000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class MailConfig:
    user: str = ""
    password: str = ""
    host: str = "imap.gmail.com"
    port: int = 993
    folder: str = "INBOX"
    poll_seconds: int = 60
    since_days: int = 7
    max_per_poll: int = 25
    allow: list[str] = field(default_factory=list)
    send_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465

    @classmethod
    def from_env(cls) -> "MailConfig":
        host = os.environ.get("SHIPCHECK_IMAP_HOST", "imap.gmail.com").strip()
        return cls(
            user=os.environ.get("SHIPCHECK_IMAP_USER", "").strip(),
            # Gmail shows app passwords as "abcd efgh ijkl mnop"; the spaces are not part of it.
            password=os.environ.get("SHIPCHECK_IMAP_PASSWORD", "").replace(" ", ""),
            host=host,
            port=_env_int("SHIPCHECK_IMAP_PORT", 993),
            folder=os.environ.get("SHIPCHECK_IMAP_FOLDER", "INBOX").strip() or "INBOX",
            poll_seconds=max(15, _env_int("SHIPCHECK_MAIL_POLL", 60)),
            since_days=max(1, _env_int("SHIPCHECK_MAIL_DAYS", 7)),
            max_per_poll=max(1, _env_int("SHIPCHECK_MAIL_MAX", 25)),
            allow=[a.strip().lower() for a in os.environ.get("SHIPCHECK_MAIL_ALLOW", "").split(",") if a.strip()],
            send_enabled=os.environ.get("SHIPCHECK_SMTP_SEND", "0") == "1",
            smtp_host=os.environ.get("SHIPCHECK_SMTP_HOST", "").strip() or re.sub(r"^imap\.", "smtp.", host),
            smtp_port=_env_int("SHIPCHECK_SMTP_PORT", 465),
        )

    @property
    def configured(self) -> bool:
        return bool(self.user and self.password)

    def allowed(self, sender: str) -> bool:
        s = (sender or "").lower()
        return not self.allow or any(s == a or s.endswith("@" + a.lstrip("@")) or s.endswith("." + a.lstrip("@"))
                                     for a in self.allow)


# ------------------------------------------------------------------ parsing
def _html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", markup)
    markup = re.sub(r"(?i)</t[dh]>", "\t", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def _safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .") or "attachment"
    return name[-120:]


def _is_noise(part, name: str, data: bytes) -> str | None:
    """Why an attachment should be ignored, or None to keep it."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if name.lower() in _NOISE_NAMES or ext in _NOISE_EXT:
        return "signature / calendar file"
    if not data:
        return "empty"
    if len(data) > ATTACHMENT_MAX:
        return f"larger than {ATTACHMENT_MAX // 1_000_000} MB"
    if ext in _IMAGE_EXT or part.get_content_maintype() == "image":
        inline = (part.get_content_disposition() or "inline") == "inline" and part.get("Content-ID")
        if inline or len(data) < SIGNATURE_IMAGE_MAX:
            return "inline / signature image"
    return None


def parse_message(raw: bytes) -> dict:
    """RFC 822 bytes -> {"from", "subject", "body", "files": [(name, bytes)], "ignored", "meta"}."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
    body_part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if body_part is not None:
        try:
            body = body_part.get_content()
        except (LookupError, UnicodeDecodeError):
            body = (body_part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        if body_part.get_content_subtype() == "html":
            body = _html_to_text(body)

    files, ignored, used = [], [], set()
    for part in msg.iter_attachments():
        fname = part.get_filename()
        if not fname:
            ext = part.get_content_subtype()
            fname = f"attachment.{'txt' if ext == 'plain' else ext}"
        fname = _safe_name(fname)
        data = part.get_payload(decode=True) or b""
        why = _is_noise(part, fname, data)
        if why:
            ignored.append({"name": fname, "reason": why})
            continue
        base, n = fname, 2
        while fname.lower() in used:                  # two attachments with the same name
            stem, dot, ext = base.rpartition(".")
            fname = f"{stem or base}_{n}{dot}{ext if stem else ''}"
            n += 1
        used.add(fname.lower())
        files.append((fname, data))

    try:
        date = email.utils.parsedate_to_datetime(str(msg.get("Date"))).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        date = dt.datetime.now(dt.timezone.utc)
    return {
        "from": addr.lower(),
        "from_name": name,
        "subject": str(msg.get("Subject", "") or "").strip(),
        "body": body.strip(),
        "files": files,
        "ignored": ignored,
        "meta": {
            "message_id": str(msg.get("Message-ID", "") or "").strip(),
            "references": str(msg.get("References", "") or "").strip(),
            "to": str(msg.get("To", "") or ""),
            "date": date.isoformat(timespec="seconds"),
        },
    }


def mail_id(parsed: dict) -> str:
    """Stable, time-sortable id: survives restarts and IMAP UID renumbering."""
    key = parsed["meta"]["message_id"] or (parsed["from"] + parsed["subject"] + parsed["meta"]["date"])
    stamp = parsed["meta"]["date"][:19].replace("-", "").replace(":", "").replace("T", "_")
    return f"mail_{stamp}_{hashlib.sha1(key.encode()).hexdigest()[:6]}"


# ------------------------------------------------------------------ poller
class MailboxPoller:
    """Checks the mailbox every `poll_seconds` and hands new messages to `on_email`.

    on_email(email_dict, meta) -> result dict; `known(email_id)` says whether a
    message was already processed (so a restart does not duplicate anything).
    """

    def __init__(self, cfg: MailConfig, save_dir: Path, on_email: Callable[[dict, dict], dict],
                 known: Callable[[str], bool], imap_factory=imaplib.IMAP4_SSL, smtp_factory=smtplib.SMTP_SSL):
        self.cfg = cfg
        self.save_dir = Path(save_dir)
        self.on_email = on_email
        self.known = known
        self._imap_factory = imap_factory
        self._smtp_factory = smtp_factory
        self._seen: set[tuple[str, bytes]] = set()     # (uidvalidity, uid) already handled this run
        self._lock = threading.Lock()                   # one check at a time (timer + "Check now")
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.state = {"configured": cfg.configured, "account": cfg.user, "folder": cfg.folder,
                      "host": cfg.host, "poll_seconds": cfg.poll_seconds, "send_enabled": cfg.send_enabled and cfg.configured,
                      "allow": cfg.allow, "connected": False, "checking": False, "last_check": None,
                      "last_error": None, "received": 0, "skipped": 0, "last_new": []}

    # -- lifecycle -------------------------------------------------------
    def start(self):
        if not self.cfg.configured:
            return
        threading.Thread(target=self._loop, daemon=True, name="mailbox").start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def check_now(self):
        self._wake.set()

    def _loop(self):
        while not self._stop.is_set():
            self.poll_once()
            self._wake.wait(self.cfg.poll_seconds)
            self._wake.clear()

    # -- one check -------------------------------------------------------
    def poll_once(self) -> list[dict]:
        if not self.cfg.configured:
            return []
        with self._lock:
            self.state["checking"] = True
            try:
                new = self._poll()
                self.state.update(connected=True, last_error=None)
            except Exception as exc:
                new = []
                self.state.update(connected=False, last_error=_explain(exc))
            finally:
                self.state.update(checking=False, last_check=_now())
            if new:
                self.state["last_new"] = [{"email_id": r["email_id"], "from": r.get("from"), "subject": r.get("subject"),
                                           "status": r.get("status"), "category": r.get("category")} for r in new]
            return new

    def _poll(self) -> list[dict]:
        imap = self._imap_factory(self.cfg.host, self.cfg.port)
        try:
            imap.login(self.cfg.user, self.cfg.password)
            typ, data = imap.select(_quote(self.cfg.folder), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"folder {self.cfg.folder!r} not found")
            validity = _uidvalidity(imap)
            since = (dt.date.today() - dt.timedelta(days=self.cfg.since_days)).strftime("%d-%b-%Y")
            typ, data = imap.uid("SEARCH", None, "SINCE", since)
            uids = (data[0] or b"").split() if typ == "OK" and data else []
            todo = [u for u in uids if (validity, u) not in self._seen][-self.cfg.max_per_poll:]
            results = []
            for uid in todo:
                typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in data or [] if isinstance(p, tuple) and len(p) > 1), None)
                self._seen.add((validity, uid))
                if not raw:
                    continue
                res = self._handle(raw)
                if res is not None:
                    results.append(res)
            return results
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _handle(self, raw: bytes) -> dict | None:
        parsed = parse_message(raw)
        eid = mail_id(parsed)
        if self.known(eid):
            return None
        if "shipcheck" in parsed["meta"]["message_id"].lower():
            return None                                   # a reply this app sent, showing up in the folder
        if not self.cfg.allowed(parsed["from"]):
            self.state["skipped"] += 1
            return None
        self.save_dir.mkdir(parents=True, exist_ok=True)
        atts = []
        for name, data in parsed["files"]:
            stored = f"{eid}_{name}"
            (self.save_dir / stored).write_bytes(data)
            atts.append(f"mail/{stored}")
        em = {"email_id": eid, "from": parsed["from"], "subject": parsed["subject"],
              "body": parsed["body"], "attachments": atts}
        meta = dict(parsed["meta"], from_name=parsed["from_name"], ignored_attachments=parsed["ignored"],
                    folder=self.cfg.folder, account=self.cfg.user)
        res = self.on_email(em, meta)
        self.state["received"] += 1
        return res

    # -- replies ---------------------------------------------------------
    def send_reply(self, result: dict, text: str) -> dict:
        """Send `text` to the original sender as a reply in the same thread."""
        if not self.state["send_enabled"]:
            raise PermissionError("Sending is off. Set SHIPCHECK_SMTP_SEND=1 on the server to allow it.")
        to = (result.get("from") or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", to):
            raise ValueError(f"no valid sender address to reply to ({to!r})")
        subject, body = _split_subject(text, result.get("subject") or "")
        meta = result.get("mail") or {}
        msg = EmailMessage()
        msg["From"] = self.cfg.user
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        own_id = email.utils.make_msgid(idstring="shipcheck", domain=self.cfg.user.split("@")[-1] or None)
        msg["Message-ID"] = own_id
        if meta.get("message_id"):
            msg["In-Reply-To"] = meta["message_id"]
            msg["References"] = f"{meta.get('references', '')} {meta['message_id']} {own_id}".strip()
        else:
            msg["References"] = own_id
        msg.set_content(body)
        with self._smtp_factory(self.cfg.smtp_host, self.cfg.smtp_port) as smtp:
            smtp.login(self.cfg.user, self.cfg.password)
            smtp.send_message(msg)
        return {"at": _now(), "to": to, "subject": subject, "message_id": own_id}


def _split_subject(text: str, original: str) -> tuple[str, str]:
    """Drafts start with a "Subject: ..." line; use it as the subject, not the body."""
    lines = (text or "").strip().splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip()
        body = "\n".join(lines[1:]).strip()
    else:
        subject, body = "", (text or "").strip()
    if not subject:
        subject = original if original.lower().startswith("re:") else f"Re: {original}"
    return subject, body + "\n"


def _quote(folder: str) -> str:
    return folder if folder.startswith('"') else '"' + folder.replace('"', '\\"') + '"'


def _uidvalidity(imap) -> str:
    try:
        typ, data = imap.response("UIDVALIDITY")
        return (data[0] or b"").decode() if data and data[0] else ""
    except Exception:
        return ""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _explain(exc: Exception) -> str:
    msg = str(exc) or type(exc).__name__
    low = msg.lower()
    if "application-specific password" in low or "authenticationfailed" in low or "invalid credentials" in low \
            or "login failed" in low or "535" in msg:
        return ("Login refused. For Gmail, use a 16-character app password (myaccount.google.com/apppasswords), "
                "not the normal password.")
    if "folder" in low and "not found" in low:
        return msg + ". Check SHIPCHECK_IMAP_FOLDER (Gmail labels are case-sensitive)."
    if isinstance(exc, OSError):
        return f"Cannot reach the mail server ({type(exc).__name__}: {msg[:160]}). Check SHIPCHECK_IMAP_HOST."
    return f"{type(exc).__name__}: {msg[:240]}"
