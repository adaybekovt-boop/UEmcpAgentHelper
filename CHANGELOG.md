# Changelog

## 0.1.0 (unreleased)

First public version.

- **Editor Gate**:
  - project and engine discovery: registry, launcher, Install.ini, source builds;
  - process detection and launching with channels enabled by command-line overrides;
  - readiness (world, asset registry, PIE), project match, version policy (5.8+ supported, 5.3–5.7 degraded);
  - in-editor API self-test.
- **Channels**: Epic Unreal MCP (Streamable HTTP, tool-search aware, schema-driven), Python remote execution (UDP multicast + TCP), and agent relay.
- **Batch Execution Engine**:
  - spawn, transform, set, material, mesh, attach and delete (permission-gated, capped);
  - scatter, grid, ring and line generators; material instances, Blueprints with components, import, duplicate;
  - stable ids for idempotent re-runs, atomic ops, one transaction per chunk, continue/stop/rollback, retry-safe after dropped connections.
- **Compact Scene Inspector** with revision-based `unchanged` replies and defect heuristics.
- **Asset Cache** with content-fingerprint invalidation, in-place merge of created assets, and fuzzy "did you mean" suggestions.
- **Native save and persistence verification**:
  - task-scoped saves, including World Partition external actors;
  - package-tag and mtime file checks;
  - reload-from-disk comparison;
  - missing-reference detection from the editor log.
- **Visual and functional verification**: framed screenshots with async capture polling and camera restore; PIE runs with log error scan.
- **Completion Gate**: evidence-based, with task kinds, auto-started tasks, waivers and blocked tasks.
- **Hooks** for Claude Code and Codex (pre-tool, post-tool, stop) and a non-destructive installer/uninstaller.
- Unit, mock (20 required scenarios) and real-editor integration tests. Offline harness benchmark and end-to-end agent benchmark runner.

### Fixes from the independent review (before release)
- The editor never executes a request id twice, so a retry after a timeout cannot double-apply relative ops.
- The completion gate:
  - edits under `inspect`/`code` tasks now require save and verification;
  - changes made by other editor tools are checked live against the dirty-package baseline taken at task start;
  - an `auto` task with no changes no longer blocks;
  - `level open` no longer starts a task.
- `verify --reload` is blocked only by the level's own dirty packages, not by unrelated user edits.
- Rollback now also removes created assets and restores material-instance parameters. Rollback is refused for multi-chunk plans and for component additions to existing Blueprints.
- Hooks:
  - quote-aware shell parsing; only the destination of `cp`/`mv` counts as a write;
  - the `ued` exemption covers only a single plain `ued` invocation;
  - the post-tool hook inspects the inner tool of Epic's `call_tool`;
  - a launcher that exits 0 when the skill is missing.
- The asset pre-check accepts object paths (`.Obj`, `_C`) and only checks scanned roots.
- A non-MCP service on the MCP port no longer crashes the CLI. Truncated replies are retried safely.
- The CLI always prints JSON: empty plans, bad args and internal errors included.
- Smaller fixes:
  - project process matching uses whole path components;
  - the delete cap applies per plan, not per chunk;
  - PIE is always stopped;
  - expected-actor checks are per level.
