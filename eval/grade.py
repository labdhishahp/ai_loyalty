"""Score completed investigations against the answer key. Makes no model calls.

Reads runs already in the database, so grading costs nothing and can be re-run
after any change to the criteria without spending on new investigations.

  python -m eval.grade                 grade every completed run
  python -m eval.grade <run_id>        grade one
"""

from __future__ import annotations

import sys

from psycopg.rows import dict_row

from core import db

from .answer_key import GoldQuestion, match


def grade(answer: dict, gold: GoldQuestion) -> tuple[int, list[tuple]]:
    """Score an answer against the rubric for the question it was asked.

    The gold question is a required argument rather than a module-level
    default: passing it explicitly is what makes it impossible to score an
    answer against criteria belonging to a different question.
    """
    results = []
    earned = 0
    for criterion in gold.criteria:
        try:
            passed = bool(criterion.check(answer))
        except Exception:                                   # noqa: BLE001
            passed = False          # a malformed answer fails, it does not crash
        earned += criterion.weight if passed else 0
        results.append((criterion, passed))
    return earned, results


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    with db.direct_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        runs = cur.execute("""
            select run_id, question, provider, model, steps_used, cost_usd,
                   final_answer
            from ops.agent_runs
            where final_answer is not null
              and (%s::text is null or run_id::text = %s)
            order by created_at desc
        """, (wanted, wanted)).fetchall()

    if not runs:
        print("No completed runs with findings to grade.", file=sys.stderr)
        return 1

    scored = unscored = 0
    for run in runs:
        print(f"\n{'=' * 74}")
        print(f"{str(run['run_id'])[:8]}  {run['provider']}/{run['model']}  "
              f"{run['steps_used']} steps  ${float(run['cost_usd']):.3f}")
        print(f"{run['question']}")

        gold = match(run["question"])
        if gold is None:
            # Not a failure of the answer -- a gap in the rubric. Reporting it
            # as a low score would conflate "we cannot mark this" with "this was
            # poor", and the second is a claim about the agent that the first
            # does not support.
            unscored += 1
            print("\n  UNSCORED — no gold question matches this. Add one to "
                  "eval/answer_key.py to mark answers like it.")
            continue

        scored += 1
        earned, results = grade(run["final_answer"], gold)
        pct = earned / gold.total_weight * 100
        print(f"\n  rubric: {gold.key}")
        print(f"  SCORE {earned}/{gold.total_weight}  ({pct:.0f}%)\n")
        for criterion, passed in results:
            print(f"    {'PASS' if passed else 'MISS'}  [{criterion.weight}] "
                  f"{criterion.description}")

    print(f"\n{'=' * 74}")
    print(f"{scored} run(s) scored, {unscored} unscored for want of a rubric.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
