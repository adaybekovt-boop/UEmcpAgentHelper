#!/usr/bin/env python3
"""Offline harness measurement (no Unreal Editor, fake engine).

Measures what UE-Direct itself costs the agent for N-object placement:
number of agent tool invocations, bytes the agent reads back, and harness
wall time. These are real measurements of UE-Direct's side only. They do NOT
measure engine time, model tokens, or any baseline workflow; use
run_agent_benchmark.py against a real editor for the comparison.

  python benchmarks/harness_bench.py
"""

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import helpers  # noqa: E402


class _Bench(helpers.ProjectTestCase):
    def runTest(self):
        pass


def measure(n):
    t = _Bench()
    t.setUp()
    try:
        plan = json.dumps({"ops": [{"op": "scatter", "id": "p", "asset": "/Game/Props/SM_Rock", "count": n,
                                    "area": {"min": [0, 0], "max": [n * 40, n * 40]}, "seed": 1}]})
        agent_calls = 0
        read_bytes = 0
        t0 = time.time()
        for argv, stdin in ((["level", "new", "/Game/Maps/Bench"], None), (["batch", "-"], plan), (["save"], None),
                            (["verify", "--reload"], None)):
            code, res = t.ued(*argv, stdin=stdin)
            assert code == 0, res
            agent_calls += 1
            read_bytes += len(json.dumps(res, separators=(",", ":")))
        wall = time.time() - t0
        return {"objects": n, "agent_tool_calls": agent_calls, "editor_round_trips": t.transport.calls,
                "agent_reads_bytes": read_bytes, "plan_bytes_written_by_agent": len(plan),
                "harness_wall_s": round(wall, 2)}
    finally:
        t.tearDown()


if __name__ == "__main__":
    rows = [measure(n) for n in (10, 100, 1000)]
    print("| objects | agent tool calls | editor round trips | bytes agent reads | plan bytes | harness s |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        print("| %(objects)d | %(agent_tool_calls)d | %(editor_round_trips)d | %(agent_reads_bytes)d | "
              "%(plan_bytes_written_by_agent)d | %(harness_wall_s)s |" % r)
