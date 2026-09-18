"""Robustness tests on inputs that are NOT in the hackathon dataset.

    python -m pytest -q
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from shipcheck.classifier import Classification, RuleClassifier  # noqa: E402
from shipcheck.compare import compare_field, is_missing, parse_container_count, parse_weight_kg  # noqa: E402
from shipcheck.fields import detect_doc_type, extract_fields, match_label  # noqa: E402
from shipcheck.parsers import parse_attachment  # noqa: E402
from shipcheck.pipeline import Pipeline, apply_human_review, to_submission  # noqa: E402


class MemInbox:
    def __init__(self, files):
        self.files = files

    def read_bytes(self, path):
        return self.files[path]


def run(email, files, ai=None):
    p = Pipeline(MemInbox(files), use_llm=False)
    p.ai = ai
    return p.process(email)


SI = """SHIPPING INSTRUCTION
Shipper: ACME PAPER SDN. BHD.
  1 JALAN AMPANG, KUALA LUMPUR
Consignee: GLOBAL TRADING LIMITED
Notify Party: SAME AS CONSIGNEE
Port of Loading: Port Klang (Westport), Malaysia
Port of Discharge: Ho Chi Minh City, Viet Nam
Number of Containers: 2 x 40'HC + 1 x 20'GP
Total Gross Weight: 45.500,00 KGS
Net Weight: 44,000 KGS
"""

BL_SAME = """BILL OF LADING
SHIPPER: ACME PAPER SDN BHD
CONSIGNEE: GLOBAL TRADING LTD
NOTIFY: SAME AS CONSIGNEE
Load Port: PORT KLANG (WESTPORT), MALAYSIA (MYPKG)
Discharging Port: HOCHIMINH CITY, VIETNAM (VNSGN)
Total Containers: 3 (THREE) CONTAINERS
Gross Wt (kgs): 45.5 MT
"""


def email(atts, body="Please check the draft BL against the SI and revert with any discrepancy."):
    return {"email_id": "t1", "from": "a@b.c", "subject": "RE: docs", "body": body, "attachments": atts}


# ---------------------------------------------------------------- normalisation
@pytest.mark.parametrize("raw,kg", [("21,577 KG", 21577), ("45.500,00 KGS", 45500), ("45.5 MT", 45500),
                                     ("243588", 243588), ("1.234.567 kg", 1234567), ("22 000 KGS", 22000)])
def test_weight(raw, kg):
    assert parse_weight_kg(raw) == pytest.approx(kg)


@pytest.mark.parametrize("raw,n", [("3 x 40'HC", 3), ("2 x 40'HC + 1 x 20'GP", 3), ("THREE (3) CONTAINERS", 3),
                                    ("four containers", 4), ("12", 12)])
def test_containers(raw, n):
    assert parse_container_count(raw) == n


@pytest.mark.parametrize("raw", ["N/A", "TBA", "____MT", "_______ MTS", "", "  ", None, "-"])
def test_placeholders(raw):
    assert is_missing(raw)


def test_formatting_is_not_a_discrepancy():
    assert compare_field("consignee", "Global Trading Limited", "GLOBAL TRADING LTD.").status == "match"
    assert compare_field("port_of_discharge", "Ho Chi Minh City, Viet Nam", "HOCHIMINH CITY, VIETNAM (VNSGN)").status == "match"
    assert compare_field("gross_weight_kg", "22,000 KG", "22000").status == "match"


def test_real_discrepancies_are_flagged():
    assert compare_field("container_count", "3 x 40'HC", "4 x 40'HC").status == "mismatch"
    assert compare_field("port_of_loading", "SINGAPORE", "PORT KLANG (WESTPORT), MALAYSIA").status == "mismatch"
    assert compare_field("gross_weight_kg", "21,114 KG", "23,114 KG").status == "mismatch"


# ---------------------------------------------------------------- labels
@pytest.mark.parametrize("line,key", [("Load Port: X", "port_of_loading"), ("POL X", "port_of_loading"),
                                       ("PORT OF LOADING (装货港): X", "port_of_loading"), ("To the Order of: X", "consignee"),
                                       ("Notify Party/Intermediate Consignee: X", "notify_party"),
                                       ("Gross Weight毛重(KGS): X", "gross_weight_kg"), ("NET WEIGHT: X", "_net_weight")])
def test_label_alignment(line, key):
    assert match_label(line)[0] == key


def test_net_weight_is_not_gross():
    ex = extract_fields(SI.splitlines())
    assert parse_weight_kg(ex.values["gross_weight_kg"]) == 45500


def test_doc_types():
    assert detect_doc_type(["BILL OF LADING INSTRUCTION", "x"]) == "SI"
    assert detect_doc_type(["SEA WAYBILL", "x"]) == "BL"
    assert detect_doc_type(["PACKING LIST", "x"]) == "PACKING_LIST"


# ---------------------------------------------------------------- end to end
def test_messy_but_matching_pair_is_ok():
    r = run(email(["a/x_SI.txt", "a/x_BL.txt"]), {"a/x_SI.txt": SI.encode(), "a/x_BL.txt": BL_SAME.encode()})
    assert r["category"] == "BL_COMPARISON"
    assert r["status"] == "OK", r["comparison"]


def test_single_field_mismatch():
    bl = BL_SAME.replace("3 (THREE) CONTAINERS", "4 CONTAINERS")
    r = run(email(["x_SI.txt", "x_BL.txt"]), {"x_SI.txt": SI.encode(), "x_BL.txt": bl.encode()})
    assert r["status"] == "MISMATCH" and r["defect_fields"] == ["container_count"]


def test_files_swapped_by_name_are_routed_by_content():
    # SI content in the "_BL" file and vice versa — roles come from headers, not names
    r = run(email(["x_BL.txt", "x_SI.txt"]), {"x_BL.txt": SI.encode(), "x_SI.txt": BL_SAME.encode()})
    assert r["status"] == "OK"


def test_docx_three_column_table_and_xlsx():
    import docx
    import openpyxl
    d = docx.Document()
    d.add_paragraph("DRAFT BILL OF LADING")
    t = d.add_table(rows=0, cols=2)
    for k, v in [("Shipper", "ACME PAPER SDN BHD"), ("Consignee", "GLOBAL TRADING LTD"), ("Notify", "SAME AS CONSIGNEE"),
                 ("Port of Loading", "PORT KLANG (WESTPORT), MALAYSIA"), ("Port of Discharge", "HOCHIMINH CITY, VIETNAM"),
                 ("No. of Containers", "3"), ("Gross Weight (KG)", "45,500")]:
        c = t.add_row().cells
        c[0].text, c[1].text = k, v
    b = io.BytesIO(); d.save(b)
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SHIPPING INSTRUCTION"])
    for line in SI.splitlines()[1:]:
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1); ws.append([k, v.strip()])
    x = io.BytesIO(); wb.save(x)
    r = run(email(["s.xlsx", "b.docx"]), {"s.xlsx": x.getvalue(), "b.docx": b.getvalue()})
    assert r["status"] == "OK", r["comparison"]


def test_corrupt_file_escalates_unreadable():
    r = run(email(["x_SI.txt", "x_BL.pdf"]), {"x_SI.txt": SI.encode(), "x_BL.pdf": b"%PDF-1.5 garbage\x00\xff"})
    assert r["status"] == "NEEDS_REVIEW" and r["review_reason"] == "unreadable"


def test_blank_value_escalates_missing_value():
    r = run(email(["x_SI.txt", "x_BL.txt"]), {"x_SI.txt": SI.replace("45.500,00 KGS", "TBA").encode(), "x_BL.txt": BL_SAME.encode()})
    assert r["review_reason"] == "missing_value"
    fixed = apply_human_review(r, {"si": {"gross_weight_kg": "45,500 KG"}})
    assert fixed["status"] == "OK" and fixed["history"]


def test_dropped_attachments_vs_awaiting_documents():
    r = run(email([], "Please compare the SI and draft BL and confirm."), {})
    assert r["review_reason"] == "missing_attachment"
    r = run(email([], "Please assist to send the draft BL for ABC123 for checking asap."), {})
    assert r["category"] == "BL_COMPARISON" and r["status"] == "AWAITING_DOCUMENTS"
    assert to_submission([r])["t1"]["status"] == "OK"


def test_misleading_subject_does_not_drive_category():
    e = {"email_id": "x", "from": "", "subject": "RE_ TO CONFIRM DOCS _ 5RSG-1", "attachments": [],
         "body": "Hi,\n\nQuery on invoice 5250075931: is the THC included?\n\nBest Regards,\nX"}
    assert RuleClassifier().classify(e).category == "INVOICE_QUERY"


def test_processing_errors_are_visible():
    class Boom(MemInbox):
        def read_bytes(self, path):
            raise IOError("storage offline")
    p = Pipeline(Boom({}), use_llm=False)
    r = p.process(email(["x_SI.txt", "x_BL.txt"]))
    assert r["status"] == "NEEDS_REVIEW" and r["review_reason"] == "unreadable"


# ---------------------------------------------------------------- LLM wiring (stubbed)
class FakeAI:
    def __init__(self):
        self.calls = []

    def classify(self, email, hint):
        self.calls.append("classify")
        return Classification("SI_REQUEST", 0.9, ["SI_REQUEST: Claude — stub"], engine="claude", rationale="stub")

    def extract(self, text, doc_type, keys):
        self.calls.append("extract")
        return {k: ({"value": "SINGAPORE", "label": "Place of Receipt/Loading", "evidence": "..."} if k == "port_of_loading" else None) for k in keys}

    def read_scan(self, pages):
        self.calls.append("read_scan")
        return {"doc_type": "SI", "legibility": 0.6, "fields": {}}


def test_llm_used_only_when_rules_are_unsure():
    ai = FakeAI()
    run({"email_id": "q", "from": "", "subject": "hello", "body": "see you tomorrow", "attachments": []}, {}, ai)
    assert ai.calls == ["classify"]
    ai = FakeAI()
    run(email([]), {}, ai)
    assert "classify" not in ai.calls


def test_llm_locates_unknown_label_but_never_overrides_blank():
    si = SI.replace("Port of Loading: Port Klang (Westport), Malaysia", "Origin Terminal (Place of Receipt/Loading) - SINGAPORE")
    ai = FakeAI()
    r = run(email(["x_SI.txt", "x_BL.txt"]), {"x_SI.txt": si.encode(), "x_BL.txt": BL_SAME.encode()}, ai)
    assert "extract" in ai.calls
    assert r["defect_fields"] == ["port_of_loading"]
    si_doc = next(d for d in r["documents"] if d["role"] == "SI")
    assert si_doc["fields"]["port_of_loading"]["method"] == "llm"
