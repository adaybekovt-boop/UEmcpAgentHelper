# Troubleshooting (error code → what to do)

| Code | Meaning | Action |
|---|---|---|
| `NO_PROJECT` / `PROJECT_AMBIGUOUS` | No single `.uproject` found | Run inside the project, or pass `--project path.uproject` |
| `ENGINE_NOT_FOUND` | EngineAssociation could not be resolved | Set `UE_ENGINE_ROOT` to the folder that contains `Engine/` |
| `EDITOR_CLOSED` | No editor process for this project | `ued up` |
| `NO_CHANNEL` / `NO_EDITOR_CHANNEL` | Editor runs, but no channel answers | Still starting: wait for `ued up`. Otherwise enable the Unreal MCP plugin or Python remote execution (see transports.md) |
| `EDITOR_NOT_READY` | No world yet, or the asset registry is still scanning | `ued up` waits for this. On first launch, shader compilation can take many minutes |
| `EDITOR_TIMEOUT` / `EDITOR_EXITED` | Startup failed | Read `log` in the result. The full log is `Saved/Logs/<Project>.log` |
| `WRONG_PROJECT` / `GUARD_WRONG_PROJECT` | The editor answering has another project open | Use the right editor/port. Never edit another project |
| `ENGINE_INCOMPATIBLE` | Engine < 5.3 | Not supported. 5.3–5.7 run in degraded mode (warning) |
| `API_MISSING` | A required Python API is absent | Enable the **Python Editor Script Plugin** and **Editor Scripting Utilities**, or `ued setup --enable-python` (edits the .uproject, then the user restarts) |
| `PIE_RUNNING` / `GUARD_PIE` | Play-In-Editor is active | Stop PIE (`ued pie` stops its own session) |
| `UNSAVED_CHANGES` | Switching level would discard unsaved work | Ask the user, then `--save-dirty` |
| `UNTITLED_LEVEL` | The current level was never saved | `ued save --save-as /Game/Maps/Name` |
| `MISSING_ASSET(S)` | Asset path not found | Use the `did_you_mean` / `suggestions` paths or `ued assets find` |
| `DELETE_NOT_ALLOWED` / `DELETE_LIMIT` | Deletion needs explicit permission | Ask the user; pass `--allow-delete [--max-delete N]` |
| `SAVE_FAILED` / save `failed` list | Packages could not be saved | Check that files are not read-only or locked by source control (checkout) and that the disk is not full |
| `SCREENSHOT_MISSING` | No PNG appeared | The editor viewport must be visible. Disable *Editor Preferences > Use Less CPU when in Background*, and don't minimize the editor |
| `RELAY_PENDING` | Relay mode | Run the snippet with your MCP python tool, then `ued result <request>` |
| `TRANSPORT_NO_PYTHON_TOOL` | Unreal MCP has no python tool | Enable the toolsets plugin (*All Toolsets*) and the Python plugin |
| `EDITOR_EXEC_ERROR` | Python error inside the editor | See the `trace`. If it's in `uedirect_editor`, report a bug with the trace |
| `GATE_BLOCKED` | A mutating command ran without a ready editor | Run `ued gate` and fix the blockers |

## Completion gate says `incomplete`
The `missing` list names each step still owed, for example a save after the last change, `ued verify --reload`, or a screenshot plus review. Do those steps. A failed op must be fixed by re-running it successfully, or waived with `ued task waive --op-id <id> --reason "..."`. Waivers appear in the final report.

## Hooks
A hook denial (`UE-Direct: ... would write X.umap directly`) means you tried to fake a native asset. Create it through the editor instead. Hooks are a guard rail and don't run every possible write path. Following the rules is still required.
