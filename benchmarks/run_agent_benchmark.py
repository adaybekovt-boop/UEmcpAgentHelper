#!/usr/bin/env python3
"""End-to-end agent benchmark: plain Unreal MCP workflow vs. UE-Direct.

Same scenarios, same model, same project, same acceptance checks. The only
difference between conditions is whether UE-Direct (skill + hooks + project
instructions) is installed.

  python benchmarks/run_agent_benchmark.py --project D:/Bench/BenchProject \
      --agent claude --condition baseline --model <model> --repeat 3
  python benchmarks/run_agent_benchmark.py ... --condition uedirect
  python benchmarks/report.py benchmarks/results/*.jsonl

Requirements: a THROWAWAY Unreal 5.8 project with the Unreal MCP plugin enabled
and configured for the agent (.mcp.json / .codex/config.toml), the editor
running, and the agent CLI on PATH. Unattended runs need a permissive
permission mode, so pass it yourself via --agent-args, in a throwaway
project only.

Measured per run (from the agent CLI's own JSON output, nothing estimated):
input/output/cached tokens, cost (Claude reports it; for Codex pass --price-*),
tool calls, MCP calls, tool-result bytes, tool errors, wall time, then
independent acceptance checks done by this runner through `ued`.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UED = os.path.join(ROOT, "skills", "ue-direct", "scripts", "ued.py")
sys.path.insert(0, ROOT)
import install  # noqa: E402

ROCKS_SETUP = {"ops": [{"op": "scatter", "id": "bench_rocks", "asset": "{MESH}", "count": 100,
                        "area": {"min": [-5000, -5000], "max": [5000, 5000]}, "min_dist": 300, "seed": 5},
                       {"class": "DirectionalLight", "id": "bench_sun", "rot": [0, -45, 30]}]}
BROKEN_SETUP = {"ops": [{"op": "grid", "id": "bench_props", "asset": "{MESH}", "count": [5, 4], "step": [400, 400]},
                        {"op": "transform", "target": "id:bench_props#3", "offset": [0, 0, 350]},
                        {"op": "transform", "target": "id:bench_props#7", "offset": [0, 0, 500]},
                        {"asset": "{MESH}", "id": "bench_dupe_a", "loc": [800, 400, 0]},
                        {"asset": "{MESH}", "id": "bench_dupe_b", "loc": [800, 400, 0]}]}


def ued(project, *args, stdin=None, timeout=1800):
    p = subprocess.run([sys.executable, UED, "--project", project] + list(args), input=stdin,
                       capture_output=True, text=True, timeout=timeout)
    lines = p.stdout.strip().splitlines()
    return p.returncode, (json.loads(lines[-1]) if lines else {})


# ------------------------------------------------------------------ parsers

def parse_claude(lines):
    m = {"tool_calls": 0, "mcp_calls": 0, "tool_result_bytes": 0, "tool_errors": 0}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "tool_use":
                    m["tool_calls"] += 1
                    if str(c.get("name", "")).startswith("mcp__"):
                        m["mcp_calls"] += 1
        elif ev.get("type") == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    m["tool_result_bytes"] += len(json.dumps(c.get("content")))
                    m["tool_errors"] += 1 if c.get("is_error") else 0
        elif ev.get("type") == "result":
            u = ev.get("usage") or {}
            m.update(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"),
                     cache_read_tokens=u.get("cache_read_input_tokens"),
                     cache_write_tokens=u.get("cache_creation_input_tokens"),
                     cost_usd=ev.get("total_cost_usd"), turns=ev.get("num_turns"),
                     agent_error=bool(ev.get("is_error")), final_text=(ev.get("result") or "")[-2000:])
    return m


def parse_codex(lines):
    m = {"tool_calls": 0, "mcp_calls": 0, "tool_result_bytes": 0, "tool_errors": 0,
         "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") in ("mcp_tool_call", "command_execution"):
            m["tool_calls"] += 1
            m["mcp_calls"] += 1 if item["type"] == "mcp_tool_call" else 0
            out = item.get("aggregated_output") or item.get("result") or ""
            m["tool_result_bytes"] += len(out if isinstance(out, str) else json.dumps(out))
            m["tool_errors"] += 1 if item.get("status") == "failed" or item.get("error") else 0
        elif ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            m["final_text"] = (item.get("text") or "")[-2000:]
        elif ev.get("type") == "turn.completed":
            u = ev.get("usage") or {}
            m["input_tokens"] += u.get("input_tokens", 0)
            m["output_tokens"] += u.get("output_tokens", 0)
            m["cache_read_tokens"] += u.get("cached_input_tokens", 0)
    return m


def run_agent(agent, prompt, project, model, extra):
    if agent == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
        if model:
            cmd += ["--model", model]
    else:
        cmd = ["codex", "exec", "--json"]
        if model:
            cmd += ["--model", model]
        cmd += [prompt]
    cmd += extra
    t0 = time.time()
    p = subprocess.run(cmd, cwd=project, capture_output=True, text=True, timeout=3 * 3600)
    wall = time.time() - t0
    lines = p.stdout.splitlines()
    m = parse_claude(lines) if agent == "claude" else parse_codex(lines)
    m["wall_s"] = round(wall, 1)
    m["exit_code"] = p.returncode
    return m, p.stdout


# ------------------------------------------------------------------ checks

def acceptance(project, checks, level):
    """Independent checks through ued. Never saves the agent's work for it."""
    res = {}
    if checks.get("read_only"):
        code, st = ued(project, "gate")
        res["no_unsaved_changes"] = st.get("dirty", {}).get("maps", 1) == 0
        return res
    code, audit = ued(project, "audit")
    res["no_fake_assets"] = audit.get("fake", 1) == 0
    code, v = ued(project, "verify", "--reload", "--level", level)
    res["level_saved_and_reloads"] = bool(v.get("reloaded")) and v.get("checks", {}).get("level_on_disk", False)
    res["unsaved_left"] = not v.get("checks", {}).get("clean", False)
    n = v.get("actors", 0)
    res["actors"] = n
    if "min_actors" in checks:
        res["min_actors_ok"] = n >= checks["min_actors"]
    if "max_actors" in checks:
        res["max_actors_ok"] = n <= checks["max_actors"]
    code, ins = ued(project, "inspect")
    issues = ins.get("issues") or {}
    res["no_scene_defects"] = not any(issues.values())
    code, shot = ued(project, "shot", "--keep-camera")
    res["screenshot"] = shot.get("file")   # for human quality rating
    return res


def setup_level(project, scenario, prefix, mesh):
    kind = scenario["checks"].get("setup")
    if not kind:
        return
    level = {"place_10": prefix + "Place10", "place_100": prefix + "Place100", "broken": prefix + "Broken"}[kind]
    ued(project, "level", "new", level, "--save-dirty")
    plan = json.dumps(BROKEN_SETUP if kind == "broken" else ROCKS_SETUP).replace("{MESH}", mesh)
    if kind == "place_10":
        plan = plan.replace('"count": 100', '"count": 10')
    code, res = ued(project, "batch", "-", "--save", stdin=plan)
    if code:
        raise SystemExit("setup failed for %s: %s" % (scenario["id"], res))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--agent", choices=["claude", "codex"], required=True)
    ap.add_argument("--condition", choices=["baseline", "uedirect"], required=True)
    ap.add_argument("--model")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--scenarios", help="comma list (default: all)")
    ap.add_argument("--mesh", default="/Engine/BasicShapes/Cube", help="mesh used by setup levels")
    ap.add_argument("--agent-args", default="", help="extra CLI args for the agent, e.g. permission flags")
    ap.add_argument("--price-in", type=float, help="USD per 1M input tokens (Codex cost)")
    ap.add_argument("--price-out", type=float, help="USD per 1M output tokens (Codex cost)")
    a = ap.parse_args()
    project = os.path.abspath(a.project)
    if a.condition == "uedirect":
        install.install(project, {"claude"} if a.agent == "claude" else {"codex"})
    else:
        install.uninstall(project)
    names = sorted(f[:-5] for f in os.listdir(os.path.join(HERE, "scenarios")) if f.endswith(".json"))
    if a.scenarios:
        names = [n for n in names if n in a.scenarios.split(",")]
    out_path = os.path.join(HERE, "results", "%d-%s-%s.jsonl" % (int(time.time()), a.agent, a.condition))
    raw_dir = out_path[:-6] + "-raw"
    os.makedirs(raw_dir, exist_ok=True)
    for rep in range(a.repeat):
        for name in names:
            with open(os.path.join(HERE, "scenarios", name + ".json")) as fh:
                sc = json.load(fh)
            prefix = "/Game/Bench/%s_r%d_%s/" % (a.condition, rep, name)
            prompt = sc["prompt"].replace("/Game/Bench/", prefix)
            checks = dict(sc["checks"])
            level = checks.get("level", "").replace("/Game/Bench/", prefix)
            setup_level(project, sc, prefix, a.mesh)
            metrics, raw = run_agent(a.agent, prompt, project, a.model, a.agent_args.split())
            with open(os.path.join(raw_dir, "%s_r%d.jsonl" % (name, rep)), "w") as fh:
                fh.write(raw)
            if a.agent == "codex" and a.price_in is not None and a.price_out is not None:
                metrics["cost_usd"] = round(((metrics["input_tokens"] - metrics["cache_read_tokens"]) * a.price_in
                                             + metrics["output_tokens"] * a.price_out) / 1e6, 4)
            row = {"scenario": name, "rep": rep, "agent": a.agent, "model": a.model, "condition": a.condition,
                   "metrics": metrics, "checks": acceptance(project, checks, level)}
            with open(out_path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
            print(json.dumps({"scenario": name, "rep": rep, "tokens_in": metrics.get("input_tokens"),
                              "mcp_calls": metrics.get("mcp_calls"), "checks": row["checks"]}))
    print("results:", out_path)


if __name__ == "__main__":
    main()
