#!/usr/bin/env python3
"""Send a dataset email (with its SI / draft BL attachments) to the live mailbox,
so a demo shows a real email arriving and being checked.

    set SHIPCHECK_IMAP_USER=shipcheck.demo@gmail.com
    set SHIPCHECK_IMAP_PASSWORD=<16-character app password>
    python scripts/send_demo_email.py                    # email_004 -> the mailbox itself
    python scripts/send_demo_email.py --email email_005 --to someone@example.com
    python scripts/send_demo_email.py --list             # dataset emails that have attachments

Uses the same account and app password as the connector (SMTP over SSL).
"""
import argparse
import json
import mimetypes
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shipcheck.mailbox import MailConfig  # noqa: E402

DATA = ROOT / "data"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", default="email_004", help="dataset email id to send (default email_004)")
    ap.add_argument("--to", help="recipient (default: the monitored mailbox itself)")
    ap.add_argument("--list", action="store_true", help="list dataset emails with attachments and exit")
    args = ap.parse_args()

    if args.list:
        for p in sorted((DATA / "inbox").glob("email_*.json")):
            e = json.loads(p.read_text(encoding="utf-8"))
            if e.get("attachments"):
                print(f"{e['email_id']}  {', '.join(Path(a).suffix for a in e['attachments']):<14} {e['subject'][:70]}")
        return

    cfg = MailConfig.from_env()
    if not cfg.configured:
        sys.exit("Set SHIPCHECK_IMAP_USER and SHIPCHECK_IMAP_PASSWORD first (same values as the server).")
    src = DATA / "inbox" / f"{args.email}.json"
    if not src.exists():
        sys.exit(f"No dataset email {args.email!r} (try --list).")
    e = json.loads(src.read_text(encoding="utf-8"))

    msg = EmailMessage()
    msg["From"] = cfg.user
    msg["To"] = args.to or cfg.user
    msg["Subject"] = e.get("subject") or args.email
    msg.set_content(e.get("body") or "")
    for att in e.get("attachments") or []:
        path = DATA / att
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        main_t, sub_t = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main_t, subtype=sub_t, filename=path.name)

    with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port) as smtp:
        smtp.login(cfg.user, cfg.password)
        smtp.send_message(msg)
    print(f"Sent {args.email} ({len(e.get('attachments') or [])} attachments) to {msg['To']}: {msg['Subject']}")


if __name__ == "__main__":
    main()
