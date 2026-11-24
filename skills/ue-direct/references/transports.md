# Editor channels

`ued` picks a channel automatically (`transport: auto`) and caches it for 10 minutes.

| Channel | Requirements | Notes |
|---|---|---|
| `mcp` | UE 5.8+, plugin **Unreal MCP** (`ModelContextProtocol`, on by default), the **Python Editor Script Plugin**, and the toolsets plugin that provides `EditorToolset.PythonScriptingTools.execute_python` | Streamable HTTP at `http://127.0.0.1:8000/mcp`. UE-Direct reads the URL from `.uedirect.json` (`mcp_url`), the project's `.mcp.json` / `.codex/config.toml` entries named `*unreal*`, or the default. Calls run serially on the game thread. |
| `remote` | Python Editor Script Plugin with **Enable Remote Execution** (Project Settings > Plugins > Python) | UDP multicast discovery on 239.0.0.1:6766 plus a TCP command channel. Works on UE 5.x. On Linux, multicast over loopback may need a multicast-capable interface. `ued up` enables it for the session with a command-line ini override, without editing project files. |
| `relay` | Any other MCP server that can run editor Python | Set `"transport":"relay"` in `.uedirect.json`. Every editor call returns `RELAY_PENDING` with a short `snippet`. Run it with your MCP python tool, then run `ued result <request>`. |

## Forcing a channel
`--transport mcp|remote|relay`, or the env var `UED_TRANSPORT`, or `.uedirect.json`:
```json
{"transport":"mcp","mcp_url":"http://127.0.0.1:8001/mcp","chunk_size":250}
```

## Multiple editors
Each editor needs its own MCP port (`-ModelContextProtocolPort=N`). UE-Direct checks that the editor answering is the project's editor (`WRONG_PROJECT` otherwise).

## Security
UE-Direct never opens listening ports except a short-lived loopback TCP port during a remote-execution command. Epic's MCP server has no authentication and is loopback-only by default. Keep it that way.
