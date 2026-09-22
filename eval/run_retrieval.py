"""Measure retrieval quality against a gold set. No Anthropic calls.

WHY RETRIEVAL IS EVALUATED SEPARATELY from answer quality. If they were measured
together, a bad answer would have two possible causes -- the agent reasoned
poorly, or it never saw the document -- and no way to tell them apart. Scoring
retrieval on its own makes the second cause visible and fixable.

recall@k is the metric that matters here: the agent gets several passages and
only needs the right document to be among them. Its position matters much less
than its presence, which is why recall leads and MRR is reported alongside.

Reference point: the sibling RAG project measured recall@3 = 0.826 with this
same model on a 28-question set. Landing far below that points at the chunking
or the gold set, not the model.

Run:  python -m eval.run_retrieval
"""

from __future__ import annotations

import json
import pathlib
import sys

from core import db
from knowledge import retrieval

GOLD = pathlib.Path(__file__).parent / "retrieval_gold.jsonl"
KS = (1, 3, 5)


def main() -> int:
    cases = [json.loads(line) for line in GOLD.read_text().splitlines() if line.strip()]
    hits = {k: 0 for k in KS}
    reciprocal = 0.0
    misses: list[tuple[str, list[str]]] = []

    with db.direct_connection() as conn:
        for case in cases:
            passages = retrieval.search(conn, case["question"], limit=max(KS))
            found = [p.slug for p in passages]
            expected = set(case["expect"])

            for k in KS:
                if expected & set(found[:k]):
                    hits[k] += 1
            rank = next((i + 1 for i, slug in enumerate(found) if slug in expected),
                        None)
            reciprocal += 1 / rank if rank else 0.0
            if not expected & set(found[:3]):
                misses.append((case["question"], found[:3]))

    total = len(cases)
    print(f"\nRetrieval eval: {total} questions\n")
    for k in KS:
        print(f"  recall@{k}  {hits[k] / total:.3f}  ({hits[k]}/{total})")
    print(f"  MRR       {reciprocal / total:.3f}")

    if misses:
        print(f"\n  {len(misses)} question(s) with no expected document in the top 3:")
        for question, found in misses:
            print(f"    - {question}")
            print(f"      got: {', '.join(found) or '(nothing)'}")
    return 0 if hits[3] / total >= 0.75 else 1


if __name__ == "__main__":
    raise SystemExit(main())
