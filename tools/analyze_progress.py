#!/usr/bin/env python3
"""Analyze ~/.ultimate-ai/progress-audit.jsonl — the machine-readable proof
stream for the '<=5s visible progress' guarantee.

Usage:
    python analyze_progress.py <audit.jsonl> [--run ID] [--max-gap 5.0] [--last]

Groups records by run id, sorts by timestamp and reports, per run: number of
progress records (ticks/beats/events), wall-clock span, and the MAX gap between
consecutive records. Exit 0 iff every analyzed run's max gap < max-gap.

The first record of a run is not counted as a gap (there is no previous record
to measure from); the run's END is not counted either (progress obligation ends
when the work event finishes and real output prints).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="analyze_progress.py")
    ap.add_argument("audit", help="path to progress-audit.jsonl")
    ap.add_argument("--run", default=None, help="analyze only this run id")
    ap.add_argument("--max-gap", type=float, default=5.0, help="fail if any run's max gap >= this (default 5.0)")
    ap.add_argument("--last", action="store_true", help="analyze only the newest run in the file")
    args = ap.parse_args(argv)

    runs: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    try:
        with open(args.audit, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = rec.get("ts")
                if not isinstance(ts, (int, float)):
                    continue
                runs[str(rec.get("run", "?"))].append((float(ts), rec))
    except OSError as exc:
        print(f"ERROR: cannot read {args.audit}: {exc}")
        return 2

    if not runs:
        print("ERROR: no records found")
        return 2

    selected = list(runs)
    if args.run:
        if args.run not in runs:
            print(f"ERROR: run id {args.run} not found (have: {', '.join(sorted(runs))})")
            return 2
        selected = [args.run]
    elif args.last:
        selected = [max(runs, key=lambda r: runs[r][0][0])]

    all_pass = True
    for rid in sorted(selected, key=lambda r: runs[r][0][0]):
        recs = sorted(runs[rid], key=lambda x: x[0])
        gaps = [(recs[i][0] - recs[i - 1][0]) for i in range(1, len(recs))]
        max_gap = max(gaps) if gaps else 0.0
        n_ticks = sum(1 for _, r in recs if r.get("kind") in ("tick", "beat"))
        n_events = sum(1 for _, r in recs if r.get("kind") in ("event", "defer"))
        span = recs[-1][0] - recs[0][0]
        t0 = datetime.fromtimestamp(recs[0][0]).strftime("%H:%M:%S")
        t1 = datetime.fromtimestamp(recs[-1][0]).strftime("%H:%M:%S")
        ok = max_gap < args.max_gap
        all_pass = all_pass and ok
        print(
            f"[{'PASS' if ok else 'FAIL'}] run {rid}: {len(recs)} records "
            f"({n_ticks} ticks/beats, {n_events} events) · {t0}→{t1} · span {span:.1f}s · "
            f"max gap {max_gap:.2f}s (bar < {args.max_gap}s)"
        )
        if not ok:
            # show the worst offender for root-causing
            worst_i = max(range(1, len(recs)), key=lambda i: recs[i][0] - recs[i - 1][0])
            a, b = recs[worst_i - 1][1], recs[worst_i][1]
            print(
                f"       worst gap {recs[worst_i][0] - recs[worst_i - 1][0]:.2f}s: "
                f"{datetime.fromtimestamp(recs[worst_i - 1][0]).strftime('%H:%M:%S')} "
                f"[{a.get('kind')}] {str(a.get('text'))[:60]!r}  ->  "
                f"{datetime.fromtimestamp(recs[worst_i][0]).strftime('%H:%M:%S')} "
                f"[{b.get('kind')}] {str(b.get('text'))[:60]!r}"
            )
    print(f"RESULT: {'ALL RUNS PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
