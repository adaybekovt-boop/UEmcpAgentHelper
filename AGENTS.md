# Working on the UE-Direct repository

This file is for agents and contributors **developing UE-Direct itself**. It is not for using it in a game project; that is `skills/ue-direct/SKILL.md`.

## Layout
- `skills/ue-direct/` is the installable Agent Skill. Keep it self-contained.
  - `SKILL.md` must stay compact (target ≤ 1.2k tokens). Put details in `references/`.
  - `scripts/ued.py` is the CLI. `scripts/uedirect/` is the host runtime (stdlib only, Python 3.9+).
  - `scripts/uedirect_editor/` runs **inside Unreal Editor** (stdlib + `unreal` only, Python 3.11 in UE 5.8).
- `integrations/` holds the hook templates and instruction snippet used by `install.py`.
- `tests/unit`, `tests/mock` (fake `unreal` in `tests/fakes/unreal.py`) and `tests/integration` (real editor, opt-in).
- `benchmarks/` holds the offline harness measurement and the end-to-end agent benchmark.

## Rules
- Verify every engine API against Epic's Python API reference for the target version before using it. Add new required APIs to `uedirect_editor/selftest.py`. Never invent APIs.
- When you use a new engine API, extend the fake `unreal` module with the **documented** signature and behaviour, and add a test.
- Every editor-side change must keep: atomic ops, idempotent ids, no discarding of unsaved work, evidence written by the editor.
- CLI output stays one compact JSON line. Put large data in `Saved/UEDirect/last/`.
- No new dependencies. No network calls except loopback editor channels.
- Never present mock results as real-engine results, or estimates as measurements.

## Commands
```bash
python -m unittest discover -s tests -t .
```

```bash
python benchmarks/harness_bench.py
```

```bash
UED_IT_PROJECT=/path/to/ScratchProject python -m unittest tests.integration.test_real_editor -v
```
