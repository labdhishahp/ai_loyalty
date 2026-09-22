"""Run an investigation from the command line and watch it work.

  python -m agent ask "Why has engagement among Gold customers in the UK dropped?"
  python -m agent show <run_id>
"""

from __future__ import annotations

import argparse
import json
import sys

from core import db

from . import runtime


def _print_step(run: dict) -> None:
    # A run can reach a terminal state without recording a step -- a provider
    # error on the very first turn, or a budget already exhausted. Printing
    # "the last step" then crashes the CLI and hides the actual error.
    if not run["steps"]:
        return
    step = run["steps"][-1]
    calls = [c for c in run["tool_calls"] if c["step_no"] == step["step_no"]]
    print(f"\n── step {step['step_no']}  "
          f"({step['input_tokens']:,} in / {step['output_tokens']:,} out, "
          f"{step['duration_ms']}ms, {step['stop_reason']})")
    if step["assistant_text"]:
        text = step["assistant_text"].strip()
        print(f"   {text[:400]}{'…' if len(text) > 400 else ''}")
    for call in calls:
        mark = "ok " if call["ok"] else "ERR"
        args = json.dumps(call["arguments"], default=str)
        print(f"   [{mark}] {call['tool']}({args[:110]})")
        if call["ok"] and call["result"]:
            print(f"         -> {str(call['result'].get('summary'))[:160]}")
        elif not call["ok"]:
            print(f"         -> {call['error'][:160]}")


def show(run: dict) -> None:
    print(f"\n{'=' * 78}\nrun {run['run_id']}  [{run['status']}]")
    print(f"  {run['provider']}/{run['model']}  {run['steps_used']} steps  "
          f"{run['input_tokens']:,} in / {run['output_tokens']:,} out  "
          f"${float(run['cost_usd']):.3f}")
    if run["error"]:
        print(f"  error: {run['error']}")
    answer = run["final_answer"]
    if not answer:
        return
    print(f"\nVERDICT: {answer['verdict']}\n\n{answer['headline']}")
    print(f"\nmetrics: {', '.join(answer['metrics_used'])}")
    print(f"comparison: {answer['comparison_basis']}")
    print(f"cohort: {answer['cohort_basis']}")
    print("\nCAUSES")
    for cause in answer["causes"]:
        print(f"  [{cause['importance']}/{cause['confidence']}] {cause['name']}")
        print(f"      {cause['explanation']}")
        print(f"      evidence: {', '.join(cause['evidence_call_ids'])}")
    print("\nRULED OUT")
    for item in answer["ruled_out"]:
        print(f"  {item['name']}: {item['why_not']}")
    print(f"\nlimitations: {answer['limitations']}")
    print(f"next: {answer['recommended_next']}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m agent")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--provider", default=None)
    ask.add_argument("--max-advances", type=int, default=40)
    shw = sub.add_parser("show")
    shw.add_argument("run_id")
    args = parser.parse_args()

    with db.direct_connection() as conn:
        conn.execute("set time zone 'UTC'")
        if args.command == "show":
            run = runtime.load_run(conn, args.run_id)
            if run is None:
                print("no such run", file=sys.stderr)
                return 1
            show(run)
            return 0

        run_id = runtime.create_run(conn, args.question, actor="cli",
                                    provider_name=args.provider)
        print(f"run {run_id}")
        run = runtime.run_to_completion(conn, run_id,
                                        max_advances=args.max_advances,
                                        on_step=_print_step)
        show(run)
        return 0 if run["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
