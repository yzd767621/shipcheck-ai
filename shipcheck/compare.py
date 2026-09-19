"""Value normalisation and SI-vs-BL comparison.

The goal is "no false alarms": formatting differences (case, punctuation,
thousands separators, `KG` suffix, UN/LOCODE in brackets, address lines, legal
suffix spelling) must NOT be reported, while a different company, port, count
or weight must be.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .fields import FIELDS, PARTY_FIELDS

# Values that mean "the customer left this blank".
_PLACEHOLDER = re.compile(
    r"^\s*(?:n/?a|nil|none|tba|tbc|tbd|to be advised|to be confirmed|unknown|pending|\?+|-+|_+.*|\.+|x+)\s*$",
    re.I,
)


def is_missing(raw: str | None) -> bool:
    if raw is None:
        return True
    first = raw.strip().split("\n")[0].strip()
    if not first:
        return True
    if _PLACEHOLDER.match(first):
        return True
    # "____ MT", "_______ KGS": blank-with-unit
    if re.fullmatch(r"[_\s.]*(?:mt|mts|kg|kgs|tons?)?\s*", first, re.I) and "_" in first:
        return True
    return False


# ---------------------------------------------------------------- parties
_SUFFIXES = [
    (r"\bLIMITED\b", "LTD"),
    (r"\bCOMPANY\b", "CO"),
    (r"\bCORPORATION\b", "CORP"),
    (r"\bINCORPORATED\b", "INC"),
    (r"\bPRIVATE\b", "PTE"),
    (r"\bSENDIRIAN BERHAD\b", "SDN BHD"),
    (r"\bBERHAD\b", "BHD"),
    (r"\bAND\b", "&"),
]


def norm_party(raw: str) -> str:
    name = raw.strip().split("\n")[0]
    name = name.split(" | ")[0]
    s = name.upper()
    s = re.sub(r"[.,;:'\"`]", " ", s)
    for pat, rep in _SUFFIXES:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\s*&\s*", " & ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- ports
_COUNTRY_ALIASES = {
    "USA": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US", "U S A": "US", "U S": "US",
    "UNITED ARAB EMIRATES": "UAE", "U A E": "UAE",
    "KOREA": "SOUTH KOREA", "REPUBLIC OF KOREA": "SOUTH KOREA", "KOREA REPUBLIC OF": "SOUTH KOREA",
    "VIET NAM": "VIETNAM", "TURKIYE": "TURKEY", "UK": "UNITED KINGDOM",
}
_CITY_ALIASES = {
    "HO CHI MINH CITY": "HOCHIMINH CITY", "HO CHI MINH": "HOCHIMINH CITY", "HCMC": "HOCHIMINH CITY",
    "SAIGON": "HOCHIMINH CITY", "HOCHIMINH": "HOCHIMINH CITY",
    "NHAVA SHEVA (JNPT)": "NHAVA SHEVA", "JNPT": "NHAVA SHEVA", "NHAVA SHEVA": "NHAVA SHEVA",
    "PORT KLANG": "PORT KLANG (WESTPORT)", "WESTPORT": "PORT KLANG (WESTPORT)",
    "JEBEL ALI": "JEBEL ALI",
}


def norm_port(raw: str) -> str:
    s = raw.strip().split("\n")[0].upper()
    s = re.sub(r"\s*\([A-Z]{2}\s?[A-Z0-9]{3}\)\s*$", "", s)        # trailing UN/LOCODE "(MYPKG)"
    s = re.sub(r"\s+", " ", s).strip(" ,.")
    parts = [p.strip(" .") for p in s.split(",")]
    city = _CITY_ALIASES.get(parts[0], parts[0])
    country = ", ".join(parts[1:])
    country = _COUNTRY_ALIASES.get(re.sub(r"[.]", " ", country).strip(), country)
    return f"{city}, {country}" if country else city


def port_code(raw: str) -> str | None:
    m = re.search(r"\(([A-Z]{2}\s?[A-Z0-9]{3})\)\s*$", raw.strip().split("\n")[0].upper())
    return m.group(1).replace(" ", "") if m else None


# ---------------------------------------------------------------- numbers
_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}


def parse_container_count(raw: str) -> int | None:
    s = raw.strip().split("\n")[0]
    groups = re.findall(r"(\d+)\s*[xX×*]\s*\d{2}", s)                     # 3 x 40'HC + 1 x 20'GP
    if groups:
        return sum(int(g) for g in groups)
    m = re.search(r"\((\d+)\)", s) or re.search(r"\b(\d+)\b", s)
    if m:
        return int(m.group(1))
    for w, n in _WORDS.items():
        if re.search(rf"\b{w}\b", s, re.I):
            return n
    return None


def parse_weight_kg(raw: str) -> float | None:
    s = raw.strip().split("\n")[0].upper()
    m = re.search(r"\d[\d,.\s]*", s)
    if not m:
        return None
    num = m.group(0).strip().replace(" ", "")
    # 1.234.567,89 (EU) vs 1,234,567.89 (US) vs 21.577 (ambiguous)
    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = num.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    elif num.count(".") > 1:
        num = num.replace(".", "")
    try:
        value = float(num)
    except ValueError:
        return None
    unit = s[m.end():]
    if re.match(r"\s*(MT|MTS|TON|TONS|TONNES?)\b", unit):
        value *= 1000
    elif re.match(r"\s*(LB|LBS)\b", unit):
        value *= 0.45359237
    return round(value, 2)


# ---------------------------------------------------------------- compare
@dataclass
class FieldResult:
    field: str
    si_raw: str | None
    bl_raw: str | None
    si_norm: str | None = None
    bl_norm: str | None = None
    status: str = "match"            # match | mismatch | missing_si | missing_bl
    confidence: float = 1.0
    note: str = ""

    def to_dict(self):
        return self.__dict__.copy()


@dataclass
class Comparison:
    fields: list[FieldResult] = field(default_factory=list)

    @property
    def mismatches(self) -> list[str]:
        return [f.field for f in self.fields if f.status == "mismatch"]

    @property
    def missing(self) -> list[FieldResult]:
        return [f for f in self.fields if f.status.startswith("missing")]


def _normalise(key: str, raw: str):
    if key in PARTY_FIELDS:
        return norm_party(raw)
    if key in ("port_of_loading", "port_of_discharge"):
        return norm_port(raw)
    if key == "container_count":
        return parse_container_count(raw)
    if key == "gross_weight_kg":
        return parse_weight_kg(raw)
    return raw.strip().upper()


def compare_field(key: str, si_raw: str | None, bl_raw: str | None) -> FieldResult:
    r = FieldResult(key, si_raw, bl_raw)
    if is_missing(si_raw):
        r.status, r.note = "missing_si", "SI value is blank or a placeholder"
        return r
    if is_missing(bl_raw):
        r.status, r.note = "missing_bl", "BL value is blank or a placeholder"
        return r
    a, b = _normalise(key, si_raw), _normalise(key, bl_raw)
    r.si_norm, r.bl_norm = (None if a is None else str(a)), (None if b is None else str(b))
    if a is None or b is None:
        r.status = "missing_si" if a is None else "missing_bl"
        r.note = "value present but could not be interpreted"
        return r

    if key == "gross_weight_kg":
        same = abs(a - b) < 0.5
    elif key in ("port_of_loading", "port_of_discharge"):
        same = a == b or re.sub(r"[\s,]+", " ", a) == re.sub(r"[\s,]+", " ", b)
    else:
        same = a == b
    if same:
        r.status = "match"
        if key in ("port_of_loading", "port_of_discharge"):
            ca, cb = port_code(si_raw), port_code(bl_raw)
            if ca and cb and ca != cb:
                r.note = f"port names agree; location codes differ ({ca} vs {cb})"
                r.confidence = 0.8
        return r

    r.status = "mismatch"
    if key in PARTY_FIELDS or key.startswith("port"):
        sim = SequenceMatcher(None, str(a), str(b)).ratio()
        if sim > 0.9:
            r.confidence = 0.7
            r.note = f"near-identical text (similarity {sim:.0%}) — possible typo"
        if key.startswith("port"):
            ca, cb = port_code(si_raw), port_code(bl_raw)
            if ca and ca == cb:
                r.note = (r.note + "; " if r.note else "") + f"same location code {ca} but different port name"
    elif key == "gross_weight_kg":
        r.note = f"difference {b - a:+,.0f} kg"
    elif key == "container_count":
        r.note = f"difference {b - a:+d}"
    return r


def compare(si_values: dict[str, str], bl_values: dict[str, str]) -> Comparison:
    return Comparison([compare_field(k, si_values.get(k), bl_values.get(k)) for k in FIELDS])
