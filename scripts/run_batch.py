"""Process the whole inbox from the command line.

    python scripts/run_batch.py                    # rules + Claude (if ANTHROPIC_API_KEY set)
    python scripts/run_batch.py --no-llm           # rules only
    python scripts/run_batch.py --source http://localhost:8080 --submit

Writes out/results.json (full report) and out/submission.json (scoring shape).
"""
import argparse
import collections
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "data"))

from loader import Inbox  # noqa: E402
from shipcheck.pipeline import Pipeline, to_submission  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(ROOT / "data"))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--submit", action="store_true", help="POST to <source>/submit (HTTP source only)")
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    inbox = Inbox(args.source)
    pipe = Pipeline(inbox, use_llm=not args.no_llm)
    emails = inbox.emails()
    print(f"{len(emails)} emails · engine={'hybrid (rules + Claude)' if pipe.ai else 'rules only'}")
    with ThreadPoolExecutor(args.workers) as pool:
        results = list(pool.map(pipe.process, emails))

    out = Path(args.out)
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    sub = to_submission(results)
    (out / "submission.json").write_text(json.dumps(sub, indent=2), encoding="utf-8")

    print("categories:", dict(collections.Counter(v["category"] for v in sub.values())))
    print("BL checks:", dict(collections.Counter(v["status"] for v in sub.values() if v["category"] == "BL_COMPARISON")))
    print("review reasons:", dict(collections.Counter(v["review_reason"] for v in sub.values() if v["review_reason"])))
    errors = [r["email_id"] for r in results if r.get("status") == "ERROR"]
    if errors:
        print("ERRORS:", errors)
    print(f"wrote {out / 'results.json'} and {out / 'submission.json'}")
    if args.submit:
        board = inbox.submit(sub)
        print(json.dumps(board, indent=2)[:3000])


if __name__ == "__main__":
    main()
