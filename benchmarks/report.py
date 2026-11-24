#!/usr/bin/env python3
"""Aggregate benchmark result files into a markdown comparison table.

  python benchmarks/report.py benchmarks/results/*.jsonl

Medians per (scenario, condition). Reduction % is reported only where both
conditions have data. Nothing is extrapolated.
"""

import json
import statistics
import sys

METRICS = ["input_tokens", "output_tokens", "cache_read_tokens", "tool_calls", "mcp_calls", "tool_result_bytes",
           "tool_errors", "wall_s", "cost_usd"]


def load(paths):
    rows = []
    for p in paths:
        with open(p) as fh:
            rows.extend(json.loads(line) for line in fh if line.strip())
    return rows


def med(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return statistics.median(vals) if vals else None


def check_rate(rows):
    passed = total = 0
    for r in rows:
        for k, v in r["checks"].items():
            if isinstance(v, bool) and k != "unsaved_left":
                total += 1
                passed += 1 if v else 0
    return (passed / total) if total else None


def main(paths):
    rows = load(paths)
    groups = {}
    for r in rows:
        groups.setdefault((r["scenario"], r["condition"]), []).append(r)
    scenarios = sorted({s for s, _ in groups})
    print("| scenario | metric | baseline | uedirect | change |")
    print("|---|---|---|---|---|")
    for s in scenarios:
        b, u = groups.get((s, "baseline"), []), groups.get((s, "uedirect"), [])
        for m in METRICS:
            mb = med([r["metrics"].get(m) for r in b])
            mu = med([r["metrics"].get(m) for r in u])
            change = "%+.0f%%" % (100.0 * (mu - mb) / mb) if mb and mu is not None else "n/a"
            print("| %s | %s | %s | %s | %s |" % (s, m, mb, mu, change))
        print("| %s | checks passed | %s | %s | |" % (s, _pct(check_rate(b)), _pct(check_rate(u))))
        print("| %s | runs (n) | %d | %d | |" % (s, len(b), len(u)))


def _pct(v):
    return "n/a" if v is None else "%.0f%%" % (100 * v)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
