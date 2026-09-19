# Demo kit: show every capability in 5 minutes

Each folder is one scenario. Click **New email** in the app, paste the subject and message from `email.txt`, drag in the other files, and click **Process email**.

| # | Folder | Shows | Result with no key (rules + OCR) | Result with a free Gemini key |
|---|---|---|---|---|
| 1 | `1_messy_but_matching` | Excel SI vs Word BL with Chinese labels, `64.250,00 KGS` vs `64.25 MT`, `Ho Chi Minh City, Viet Nam` vs `HOCHIMINH CITY, VIETNAM`, `2 x 40'HC + 1 x 20'GP` vs `3 (THREE) CONTAINERS`, a NET WEIGHT trap | **Verified**: no false alarms | same |
| 2 | `2_real_discrepancies` | 4 errors hidden in similar documents | **Mismatch** on consignee, port of discharge, containers (+1) and weight (+500 kg). BUSAN/PUSAN is marked as a *possible typo* (same port code) and sent to a person | same + Gemini-written amendment email |
| 3 | `3_scanned_documents` | Image-only PDFs | **Needs review: unreadable**. OCR reads both scans, pre-fills the review form and suggests the 6 vs 7 container difference | Gemini vision also reads the scans; its values take priority |
| 4 | `4_wrong_document` | A packing list sent in place of the draft BL | **Needs review: wrong document type**, detected from the content, not the file name | same |
| 5 | `5_blank_value_human_fix` | SI gross weight is `TBA` | **Needs review: missing value**. Type `88,420 KG`, click Confirm, and the result becomes Verified live | same |
| 6 | `6_unknown_label_ai` | A new SI template: "Origin Terminal (Place of Receipt)" | **Needs review: missing value** (rules never guess) | Gemini finds the port of loading, tagged *found by Gemini* in the table |
| 7 | `7_ambiguous_email` | Unusual wording, no attachments | General, 35% confidence, flagged for a person | Gemini classifies it and explains why |

Regenerate the files: `python scripts/make_demo_kit.py`

## 5-minute video script

| Time | Show | Say |
|---|---|---|
| 0:00–0:30 | Title slide / team | "Shipping teams get BL checks, SI requests, invoice queries and spam in one inbox. A missed discrepancy means amendments, delays and fees." |
| 0:30–1:10 | **Insights** page | "We processed all 520 emails. 96% were resolved without a person, about 30 staff-hours saved. 42% of draft BLs had errors, mostly container counts. And here are the counterparties that send the most faulty drafts." |
| 1:10–1:50 | **Inbox**: open a Mismatch (e.g. `email_025`), click a red row | "Every email shows why it was classified. Here the port and the container count are wrong. Click a field and the exact source line lights up in both documents, so staff trust it without re-reading." Click **Report**, then **Draft reply**. |
| 1:50–2:30 | Upload demo **1**, then **2** | "Different formats and labels, one in Chinese, EU number formats: no false alarms. Now a real one: four errors found, and BUSAN vs PUSAN is marked as a possible typo instead of guessed." |
| 2:30–3:20 | Upload **3** and **4**, open the **Review queue** | "When it can't decide, it doesn't guess: a scanned file is read by OCR and goes to a person with the values pre-filled; a packing list in the BL slot is caught from its content." |
| 3:20–3:50 | Upload **5**, fill in the weight, press **Ctrl+Enter** | "The reviewer fixes the blank value and the report recomputes instantly. Every decision is in the audit trail." Show **Activity**. |
| 3:50–4:20 | Upload **6** (and **7**) | "A new customer template the rules have never seen: Gemini finds the field and it's labelled 'found by Gemini'." |
| 4:20–4:50 | Architecture slide | "Hybrid: fast, auditable rules plus free AI (Gemini and Tesseract OCR) for the long tail. FastAPI in Docker on the cloud. Scored 1.0 on the organisers' self-evaluation: 520/520 classified, 46/46 defects, 20/20 escalations." |
| 4:50–5:00 | Close | Team name, repo, live link |

**Before recording:** open the live site a minute early (the free tier sleeps) and press **Ctrl+Shift+R**. Upload the demo emails in the order you'll present them. Use `Ctrl+K` to jump to any email.
