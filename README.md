# UE-Direct

**Agent execution harness for Unreal Engine 5.8+** by T&T Labs. MIT licensed.

> Editor First. Native Assets. Verified Results. Minimal Tokens.

UE-Direct is an [Agent Skill](https://agentskills.io) plus a small stdlib-only Python runtime. It makes coding agents (Claude Code, Codex and other Agent Skills clients) build game content **inside a running Unreal Editor**. Results are saved as native assets, the saves are verified by reloading from disk and taking screenshots, and the agent spends a fraction of the usual tool calls and tokens.

It exists because agents left alone with Unreal tend to:
- write JSON "maps" or generator scripts instead of levels;
- skip opening the editor;
- make one MCP call per actor;
- never save;
- claim success they never checked.

## How it works

```
agent ──(1 shell call)──▶ ued CLI ──request file──▶ Unreal Editor (Python)
  ▲                         │  ▲                      │ uedirect_editor: gate, batch,
  │   one compact JSON line │  └──response file──────┤ save, verify, inspect, shots
  └─────────────────────────┘                         └─▶ evidence.jsonl (written by the editor)
                 hooks (pre-tool / post-tool / stop) ──▶ completion gate reads evidence
```

- **Channels** (auto-detected): Epic's **Unreal MCP** plugin in UE 5.8 (`execute_python` over Streamable HTTP), **Python remote execution** (UE 5.x), or **relay** through any third-party MCP server that can run editor Python. Nothing is tied to one MCP server.
- **Fixed-size messages**: the agent writes a plan, UE-Direct writes it to a request file, and a 6-line bootstrap snippet makes the editor execute it. Results come back through a response file. Placing 1,000 actors costs the agent about the same tokens as placing 10.
- **Evidence from the editor**: saves, reloads and batch results are recorded by the editor process itself. Success is computed from that evidence, never from the agent's claims.

## Install

Requirements:
- Python 3.9+ on the agent's machine.
- In the project, the **Python Editor Script Plugin**.
- For UE 5.8, the **Unreal MCP** plugin with its toolsets (on by default), or Python Remote Execution.

```bash
git clone <this repo> UE-Direct
```

```bash
python UE-Direct/install.py --project "D:/Projects/MyGame" --agents claude,codex
```

The install:
- copies the skill to `.claude/skills/ue-direct` and `.agents/skills/ue-direct`;
- merges hooks into `.claude/settings.json` and `.codex/hooks.json`, keeping existing settings;
- adds a marked block to `CLAUDE.md` / `AGENTS.md`.

Pass `--dry-run` to preview and `--uninstall` to revert. Codex asks you to trust new project hooks (`/hooks`).

Check the setup:

```bash
python .claude/skills/ue-direct/scripts/ued.py doctor
```

## Using it (what the agent does)

```bash
ued task start --kind level --level /Game/Maps/Harbor
```

```bash
ued up
```

```bash
ued assets find crate --cat mesh
```

```bash
ued level new /Game/Maps/Harbor
```

```bash
ued batch plan.json --save --verify --reload
```

```bash
ued shot
```

```bash
ued review s123 --verdict pass --notes "docks aligned, lit, no overlaps"
```

```bash
ued complete
```

- `ued up` launches or attaches to the editor and waits until it is genuinely ready.
- `ued batch` handles one to thousands of ops in transactional chunks.
- After `ued shot`, the agent looks at the PNG before `ued review`.

`ued complete` returns `{"status":"success","level":"/Game/Maps/Harbor","created":512,"modified":3,"failed":0,"saved":true,"verified":true,"visual":true,...}` only when every required check passed.

See [`skills/ue-direct/SKILL.md`](skills/ue-direct/SKILL.md) for the plan format. Topic guides live in [`skills/ue-direct/references/`](skills/ue-direct/references/).

## What is enforced, and how

| Layer | Mechanism | Guarantees | Does not guarantee |
|---|---|---|---|
| 1. Skill | Compact SKILL.md (~1.1k tokens, estimated) + on-demand references | The agent knows the workflow | Obedience |
| 2. Project integration | CLAUDE.md / AGENTS.md block, hooks | Rules load every session | — |
| 3. Execution Guard | Host gate before mutating commands; in-editor guard before every command (project, world, PIE, level) | UE-Direct never edits the wrong project or a not-ready editor, and never discards unsaved work | Protection against other tools the agent may call |
| 4. Completion Gate | Evidence-based `ued complete` + Stop hook | `success` only after editor-recorded changes, a native save, file checks (package tag, mtime), reload verification and a screenshot after the last change | That the model *looked carefully* at the screenshot: its review verdict is self-reported (and labelled so) |

The hooks deny writing `.umap`/`.uasset` files through Write/Edit/apply_patch and obvious shell redirections, and protect the evidence files. They record changes made through other Unreal MCP tools, so those still require save and verification. They also stop the agent from ending its turn with an unverified task. **Limits:** a client that offers tools not routed through hooks, or a script that writes files indirectly, can bypass them. The hooks are guard rails, not a sandbox. After three Stop blocks with no progress, the hook lets the turn end and records `stopped_incomplete`, so the agent can't loop forever.

## Tests

```bash
python -m unittest discover -s tests -t .
```

| Suite | What it proves |
|---|---|
| `tests/unit` | Project and engine discovery, plan expansion, hooks, installer merge and uninstall, completion rules, MCP parsing, relay |
| `tests/mock` | The 20 required scenarios end-to-end through the real CLI and the real editor-side code, against a strict **fake `unreal` module**, plus mock Unreal-MCP and remote-execution servers |
| `tests/integration` | **Real editor** tests: create, populate (100 and 1,000 actors), save, reload, re-run idempotently, screenshot. Needs `UED_IT_PROJECT=<throwaway project>` and is skipped otherwise |

Mock tests passing is **not** evidence that UE-Direct works inside Unreal Editor. As of this release the integration suite has not been run against a real UE 5.8 editor. See [Limitations](#limitations).

## Benchmarks

`benchmarks/harness_bench.py` gives offline, real measurements of UE-Direct's own overhead. For a full place → save → reload flow, the agent reads back about 540 bytes in 4 tool calls, the same for 10, 100 and 1,000 objects.

`benchmarks/run_agent_benchmark.py` is the end-to-end comparison: the same 10 scenarios, model and project, with and without UE-Direct. It records tokens (including cached), cost, tool/MCP calls, tool-output bytes, errors and time, then runs independent acceptance checks. **No end-to-end results are published yet.** The 60–80% token and 80–95% call reduction figures are targets, not measurements. See [benchmarks/README.md](benchmarks/README.md).

## Limitations

- **Not yet validated in a real editor.** The engine API calls were checked against Epic's UE 5.8 Python API reference, and `ued doctor` runs an in-editor API self-test. The integration suite still needs a real run. Expect first-contact fixes.
- **Epic's Unreal MCP is experimental.** UE-Direct finds `execute_python` and its argument names from the live schemas. If Epic changes the toolset, the `remote` and `relay` channels remain.
- **Screenshots** need a rendering editor viewport. A minimized editor, or *Use Less CPU when in Background*, can prevent capture.
- **Batch ops** cover actors, transforms, properties, materials, MI parameters, Blueprint creation with components, import and duplication. Blueprint graphs, material graphs, PCG, Sequencer, Niagara and animation authoring go through the editor's own tools. UE-Direct still saves and verifies afterwards.
- **World Partition**: after a reload, actors outside loaded regions may not be loaded. Verification then flags them, which can be a false positive (documented in references/world-partition.md).
- Windows is the primary target. macOS and Linux are supported by the code paths (engine discovery, process listing, launching). Python remote execution on Linux may need a multicast-capable loopback.

## License

MIT © T&T Labs. See [LICENSE](LICENSE).
