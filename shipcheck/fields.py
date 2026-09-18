"""Semantic field alignment: map the many labels SI/BL documents use onto the
7 canonical fields, and detect what kind of document we are looking at.

"Port of Loading", "Load Port", "POL" and "PORT OF LOADING (装货港)" all mean
the same thing. The lexicon below handles the known variants exactly; unknown
labels fall back to keyword reasoning (`guess_field`) and, when an LLM is
configured, to the model (see llm.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

FIELDS = [
    "shipper",
    "consignee",
    "notify_party",
    "port_of_loading",
    "port_of_discharge",
    "container_count",
    "gross_weight_kg",
]

FIELD_LABELS = {
    "shipper": "Shipper",
    "consignee": "Consignee",
    "notify_party": "Notify party",
    "port_of_loading": "Port of loading",
    "port_of_discharge": "Port of discharge",
    "container_count": "Container count",
    "gross_weight_kg": "Gross weight (kg)",
}

PARTY_FIELDS = {"shipper", "consignee", "notify_party"}

# label regex -> canonical key. Keys starting with "_" are known non-target
# fields: recognising them stops a party block from swallowing the next line.
_LEXICON: list[tuple[str, str]] = [
    # parties
    (r"shipper\s*/\s*exporter", "shipper"),
    (r"shipper\s*\(principal or seller\)", "shipper"),
    (r"shipper", "shipper"),
    (r"exporter", "shipper"),
    (r"notify party\s*/\s*intermediate consignee", "notify_party"),
    (r"notify party", "notify_party"),
    (r"notify", "notify_party"),
    (r"consignee\s*\(non-negotiable\)", "consignee"),
    (r"consigned to", "consignee"),
    (r"to the order of", "consignee"),
    (r"consignee", "consignee"),
    # ports
    (r"port of loading\s*\(pol\)", "port_of_loading"),
    (r"port of loading", "port_of_loading"),
    (r"loading port", "port_of_loading"),
    (r"load port", "port_of_loading"),
    (r"pol", "port_of_loading"),
    (r"port of discharge\s*\(pod\)", "port_of_discharge"),
    (r"port of discharge", "port_of_discharge"),
    (r"discharge port", "port_of_discharge"),
    (r"discharging port", "port_of_discharge"),
    (r"pod", "port_of_discharge"),
    (r"place of delivery", "_place_of_delivery"),
    (r"final destination", "_place_of_delivery"),
    # quantities
    (r"no\.? of containers or packages", "container_count"),
    (r"no\.? of containers", "container_count"),
    (r"number of containers", "container_count"),
    (r"total containers", "container_count"),
    (r"container count", "container_count"),
    (r"container no\.?", "_container_table"),
    # `[^\s:(]*` absorbs glyph junk such as "Weight毛重" / "Weightnn" from font fallback
    (r"total gross (?:weight|wt\.?)[^\s:(]*(?:\s*\((?:kgs?|kilograms?)\))?", "gross_weight_kg"),
    (r"gross (?:weight|wt\.?)[^\s:(]*(?:\s*\((?:kgs?|kilograms?)\))?", "gross_weight_kg"),
    (r"total gross", "gross_weight_kg"),
    (r"net (?:weight|wt\.?)(?:\s*\((?:kgs?|kilograms?)\))?", "_net_weight"),
    (r"measurement|volume|cbm", "_measurement"),
    # other known fields
    (r"ocean vessel|vessel name|vessel|export carrier[^:]*", "_vessel"),
    (r"voy(?:age)?\.?(?: no\.?)?", "_voyage"),
    (r"kinds of packages; description of goods|description of goods|description|commodity", "_goods"),
    (r"hs code", "_hs"),
    (r"b/l number|b/l no\.?|bl no\.?|bill of lading no\.?", "_bl_no"),
    (r"booking (?:ref(?:erence)?|no\.?)", "_booking"),
    (r"oc no\.?|order no\.?|po no\.?", "_order"),
    (r"freight", "_freight"),
    (r"invoice no\.?|invoice date|seller|buyer|total amount|payment terms|incoterms", "_invoice"),
    (r"country of origin|certificate no\.?|issuing authority", "_certificate"),
]

# A trailing bilingual gloss like "(发货人)" or "(装货港)" is cosmetic.
_CJK_GLOSS = r"(?:\s*[（(][^()（）]*[一-鿿][^()（）]*[)）])*"
_COMPILED = [
    (re.compile(rf"^\s*(?:{pat}){_CJK_GLOSS}\s*(?:[:：]\s*|\s+(?=\S)|$)", re.I), key)
    for pat, key in sorted(_LEXICON, key=lambda x: -len(x[0]))
]


def match_label(line: str) -> tuple[str, str] | None:
    """If `line` starts with a known label, return (canonical_key, value)."""
    for rx, key in _COMPILED:
        m = rx.match(line)
        if m:
            value = line[m.end():].strip()
            # "POL" / "POD" etc. without a colon must be followed by a value
            # that is not just more label text.
            return key, value
    return None


def guess_field(label: str) -> str | None:
    """Keyword fallback for a `label: value` line whose label is not in the
    lexicon (e.g. 'Place of Receipt/Loading Port', 'Consignee Name')."""
    l = label.lower()
    if "notify" in l:
        return "notify_party"
    if "consign" in l or "order of" in l:
        return "consignee"
    if "shipper" in l or "exporter" in l:
        return "shipper"
    if "gross" in l:
        return "gross_weight_kg"
    if "net" in l and ("weight" in l or "wt" in l):
        return "_net_weight"
    if "container" in l and ("no" in l or "count" in l or "total" in l or "qty" in l or "quantity" in l):
        return "container_count" if "no." not in l or "of" in l else "_container_table"
    if "discharg" in l or "destination port" in l:
        return "port_of_discharge"
    if "loading" in l or "load port" in l or "origin port" in l:
        return "port_of_loading"
    return None


# --------------------------------------------------------------------------
# Document type
# --------------------------------------------------------------------------
DOC_SI = "SI"
DOC_BL = "BL"

_DOC_PATTERNS = [
    (r"bill of lading instruction|b/l instruction|bl instruction|shipping instruction|shipper'?s letter of instruction|\bs\.i\.", DOC_SI),
    (r"bill of lading|b/l draft|draft b/l|sea ?waybill|house bill", DOC_BL),
    (r"commercial invoice|proforma invoice|\binvoice\b", "COMMERCIAL_INVOICE"),
    (r"packing list", "PACKING_LIST"),
    (r"certificate of origin", "CERTIFICATE_OF_ORIGIN"),
    (r"booking confirmation", "BOOKING_CONFIRMATION"),
]


def detect_doc_type(lines: list[str]) -> str | None:
    """Look at the document header (first few non-empty lines)."""
    head = [l.strip() for l in lines if l.strip() and not set(l.strip()) <= set("=-_*")][:4]
    for line in head:
        low = line.lower()
        # skip label: value lines — the title is a bare heading
        if match_label(line) and ":" in line:
            continue
        for pat, kind in _DOC_PATTERNS:
            if re.search(pat, low):
                return kind
    # Explicit disclaimers anywhere in the body ("*** NOT AN SI OR BL ***")
    body = "\n".join(lines).lower()
    for pat, kind in _DOC_PATTERNS[2:]:
        if re.search(pat, body) and "not a" in body:
            return kind
    return None


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
@dataclass
class Extracted:
    values: dict[str, str] = field(default_factory=dict)          # canonical field -> raw value (multi-line for parties)
    labels: dict[str, str] = field(default_factory=dict)          # canonical field -> label as written
    evidence: dict[str, str] = field(default_factory=dict)        # canonical field -> source line(s)
    unknown_labels: list[str] = field(default_factory=list)       # label: value lines we could not place
    method: dict[str, str] = field(default_factory=dict)          # canonical field -> "lexicon" | "keyword" | "llm" | "human"


def extract_fields(lines: list[str]) -> Extracted:
    ex = Extracted()
    current: str | None = None   # party field currently accepting continuation lines

    for raw in lines:
        if not raw.strip() or set(raw.strip()) <= set("=-_*"):
            current = None
            continue
        indented = raw[:1] in (" ", "\t")
        line = raw.strip()

        hit = None if indented else match_label(line)
        if hit is None and not indented and ":" in line:
            label, _, value = line.partition(":")
            key = guess_field(label)
            if key:
                hit = (key, value.strip())
                ex.method.setdefault(key, "keyword")
            elif len(label) < 60:
                ex.unknown_labels.append(line)

        if hit:
            key, value = hit
            label_text = line[: len(line) - len(value)].rstrip(" :：") if value else line
            if key.startswith("_"):
                current = None
                continue
            # Prefer an explicit TOTAL over per-row figures; keep the first otherwise.
            if key in ex.values and not (key == "gross_weight_kg" and "total" in label_text.lower()):
                current = key if key in PARTY_FIELDS and key == current else None
                continue
            ex.values[key] = value
            ex.labels[key] = label_text
            ex.evidence[key] = line
            ex.method.setdefault(key, "lexicon")
            current = key if key in PARTY_FIELDS else None
            continue

        # continuation line (address / second name line) for the open party block
        if current:
            ex.values[current] = (ex.values[current] + "\n" + line).strip()
            ex.evidence[current] += "\n" + line
    return ex
