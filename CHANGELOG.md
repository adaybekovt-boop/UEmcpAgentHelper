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
