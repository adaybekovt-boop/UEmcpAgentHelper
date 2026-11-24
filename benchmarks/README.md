# Benchmarks

Two tools, with different claims.

## 1. `harness_bench.py`: UE-Direct's own overhead (offline, measured)
Runs place → save → reload for 10, 100 and 1,000 objects through the real CLI against the fake engine. It measures the agent-side cost of UE-Direct: tool calls, bytes the agent reads, and plan size.

Last local run:

| objects | agent tool calls | editor round trips | bytes agent reads | plan bytes |
|---|---|---|---|---|
| 10 | 4 | 6 | 535 | 139 |
| 100 | 4 | 6 | 538 | 142 |
| 1000 | 4 | 9 | 542 | 145 |

This shows the output size stays constant as N grows. It says nothing about model tokens or engine time, and it is not a comparison with another workflow.

## 2. `run_agent_benchmark.py`: end-to-end comparison (needs a real editor)

The baseline is an agent with Unreal MCP only. The comparison is the same agent with UE-Direct installed. Both runs use the same scenarios (`scenarios/*.json`), model, project and acceptance checks.

```bash
python benchmarks/run_agent_benchmark.py --project D:/Bench/Proj --agent claude --condition baseline --model <id> --repeat 3 --agent-args "<permission flags>"
```

```bash
python benchmarks/run_agent_benchmark.py --project D:/Bench/Proj --agent claude --condition uedirect --model <id> --repeat 3 --agent-args "<permission flags>"
```

```bash
python benchmarks/report.py benchmarks/results/*.jsonl
```

Per run, it records:
- **From the agent CLI's own JSON output**: input, output and cached tokens; cost (reported by Claude Code; for Codex, pass `--price-in/--price-out`); tool calls; MCP calls; tool-result bytes; tool errors; wall time.
- **Acceptance checks done independently through `ued`**: level saved and reloadable; actor count in range; no fake `.umap`/`.uasset`; no scene defects; unsaved changes left behind (a save-correctness failure). Plus a screenshot path for **human quality rating**. Visual quality is not auto-scored.

Every setup level and every scenario run gets its own `/Game/Bench/<condition>_r<n>_<scenario>/` path, so runs don't interfere. Use a throwaway project. Unattended agents need permissive modes.

### Status
No end-to-end results have been published. The project targets are:
- 60–80% fewer tokens in suitable mass operations;
- 80–95% fewer MCP calls in batch scenarios;
- 100% native saves for successful editor tasks;
- 0% JSON-for-level substitutions.

These are goals to test, not results. Publish raw `results/*.jsonl` alongside any claim.
