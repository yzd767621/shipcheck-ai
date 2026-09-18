"""Email intent classification.

Two engines share one interface:

* `RuleClassifier` — an explainable weighted-signal scorer. It reads the body
  first (subjects in this inbox are frequently recycled "RE_ TO CONFIRM DOCS"
  threads and are misleading), looks at the attachments, and returns a
  confidence plus the signals that fired. Runs offline, costs nothing, and is
  the guardrail the LLM is checked against.
* `llm.ClaudeClient.classify` — Claude, used when the rule engine is unsure
  (see pipeline.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORIES = ["BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"]

CATEGORY_HELP = {
    "BL_COMPARISON": "Part of the draft-BL checking workflow: asks to check / compare a draft Bill of Lading "
                     "against the Shipping Instruction, or chases the draft BL so it can be checked",
    "SI_REQUEST": "Sends / asks us to prepare a new Shipping Instruction or booking",
    "INVOICE_QUERY": "Billing: invoice questions, charges, cancellations, missing GR",
    "GENERAL": "Operational updates, reports, reminders, notifications, announcements",
    "SPAM": "Unsolicited, phishing, scams or marketing",
}


@dataclass
class Classification:
    category: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    engine: str = "rules"
    rationale: str = ""


# (regex, weight, human-readable signal)  — evaluated on the cleaned body
_SIGNALS: dict[str, list[tuple[str, float, str]]] = {
    "BL_COMPARISON": [
        (r"check (?:the )?draft b/?l against (?:the )?s/?i", 5, "asks to check draft BL against SI"),
        (r"compare (?:the )?s/?i and (?:the )?draft b/?l", 5, "asks to compare SI and draft BL"),
        (r"(?:attached|find attached)[^.]{0,40}\b(?:si|shipping instruction)\b[^.]{0,30}\b(?:draft )?(?:bl|bill of lading)\b", 4, "SI and draft BL attached"),
        (r"verify the b/?l matches", 4, "asks to verify BL matches"),
        (r"confirm the b/?l is in order", 4, "asks to confirm BL is in order"),
        (r"revert with any discrepanc", 3, "asks for discrepancies"),
        (r"please check the details and confirm", 2, "asks to check details"),
        (r"\bfor checking\b", 1, "for checking"),
        (r"(?:send|share|provide) (?:us |me )?the draft b/?l[^.]{0,60}for (?:checking|verification|review)", 5,
         "requests the draft BL for checking (documents to follow)"),
    ],
    "SI_REQUEST": [
        (r"please find shipping instruction for", 5, "provides shipping instruction details"),
        (r"\bpol\s*:.*\bpod\s*:", 3, "POL/POD given in body"),
        (r"(?:prepare|issue|raise|create) (?:a |the )?(?:new )?(?:si|shipping instruction)", 4, "asks to prepare SI"),
        (r"\bshipper\s*:", 1, "shipper given in body"),
        (r"\bconsignee\s*:", 1, "consignee given in body"),
    ],
    "INVOICE_QUERY": [
        (r"\binvoice\b", 2, "mentions invoice"),
        (r"query on invoice", 4, "invoice query"),
        (r"\bthc\b|local charge", 2, "local charges"),
        (r"\bgr\b[^.]{0,30}missing|missing[^.]{0,10}\bgr\b|post the gr", 4, "missing GR for billing"),
        (r"cancel invoice|reverse the pgi", 4, "invoice cancellation"),
        (r"d&d|detention|demurrage", 3, "D&D / detention charges"),
        (r"release payment|confirm the amount|billing", 2, "payment / amount confirmation"),
    ],
    "GENERAL": [
        (r"outstanding (?:bl|b/l|list)", 3, "outstanding list"),
        (r"berthing report|berthed on schedule", 4, "berthing report"),
        (r"update summary|loading completed|documents to follow", 3, "operational update"),
        (r"automated notification|no action required|rpa bot", 4, "automated notification"),
        (r"reminder:|submit si & aed|by end of day", 3, "reminder"),
        (r"happy|new year|office resumes|holiday", 4, "announcement"),
    ],
    "SPAM": [
        (r"https?://(?!www\.(?:aprilasia|paperone)\.com)\S+", 2, "external link"),
        (r"congratulations|you have won|claim (?:your|now)|gift card|prize|lottery", 5, "prize bait"),
        (r"verify your account|mailbox has exceeded|avoid (?:deactivation|suspension)", 5, "credential phishing"),
        (r"limited time offer|buy now|% off|exclusive offer", 4, "marketing"),
        (r"bank officer|business proposal|usd \d+(?:\.\d+)? million|inheritance", 5, "advance-fee scam"),
        (r"unpaid customs fee|parcel will be returned|package could not be delivered", 5, "delivery scam"),
        (r"bitcoin|crypto|guaranteed \d+% returns", 4, "investment scam"),
        (r"within 24 hours", 1, "urgency pressure"),
    ],
}

_SUBJECT_SPAM = re.compile(r"weird trick|90% off|guaranteed|bitcoin|valued customer|you have won|free ", re.I)
_COMPILED = {c: [(re.compile(p, re.I | re.S), w, s) for p, w, s in sigs] for c, sigs in _SIGNALS.items()}

_BANNER = re.compile(r"WARNING: This email originated outside.*?(?:\n\s*\n|$)", re.S | re.I)
_QUOTED = re.compile(r"\n_{5,}.*$|\nFrom: .*$|\n-{2,} ?Original Message.*$", re.S)
_SIGNOFF = re.compile(r"\n\s*(?:best regards|regards|thanks|thank you|warm regards|best),?\s*\n.*$", re.S | re.I)


def clean_body(body: str) -> str:
    """Strip the external-sender banner, quoted thread history and signature
    so only the newest message drives the decision."""
    b = _BANNER.sub("", body or "")
    b = _QUOTED.sub("", b)
    b = _SIGNOFF.sub("", "\n" + b.strip() + "\n")
    return b.strip()


class RuleClassifier:
    name = "rules"

    def classify(self, email: dict, attachment_kinds: list[str] | None = None) -> Classification:
        body = clean_body(email.get("body", ""))
        subject = email.get("subject", "")
        atts = email.get("attachments") or []
        kinds = attachment_kinds or []

        scores = {c: 0.0 for c in CATEGORIES}
        signals: list[str] = []
        for cat, sigs in _COMPILED.items():
            for rx, w, label in sigs:
                if rx.search(body):
                    scores[cat] += w
                    signals.append(f"{cat}: {label}")

        # Attachments are strong evidence: an SI travelling with a BL-slot
        # document means someone wants them compared.
        if atts:
            has_si = "SI" in kinds or any(re.search(r"_SI\.", a, re.I) for a in atts)
            has_bl_slot = "BL" in kinds or any(re.search(r"_BL\.", a, re.I) for a in atts)
            if has_si and has_bl_slot:
                scores["BL_COMPARISON"] += 4
                signals.append("BL_COMPARISON: SI + BL documents attached")
            elif has_si or has_bl_slot:
                scores["BL_COMPARISON"] += 2
                signals.append("BL_COMPARISON: shipping document attached")

        if _SUBJECT_SPAM.search(subject):
            scores["SPAM"] += 2
            signals.append("SPAM: spam-like subject")

        best = max(scores, key=scores.get)
        total = sum(scores.values())
        if scores[best] == 0:
            return Classification("GENERAL", 0.35, ["no strong signal — defaulting to GENERAL"], scores)
        ranked = sorted(scores.values(), reverse=True)
        margin = ranked[0] - ranked[1]
        confidence = min(0.99, 0.5 + 0.5 * (scores[best] / total) * min(1.0, margin / 4 + 0.25))
        return Classification(best, round(confidence, 2), [s for s in signals if s.startswith(best)] or signals, scores)
