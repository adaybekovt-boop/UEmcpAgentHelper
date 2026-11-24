"""Configuration: defaults < <Project>/.uedirect.json < environment (UED_*)."""

import os

from .util import read_json

DEFAULTS = {
    "transport": "auto",          # auto | mcp | remote | relay
    "mcp_url": None,              # e.g. http://127.0.0.1:8000/mcp (auto-discovered when null)
    "chunk_size": 250,            # ops per editor transaction
    "call_timeout": 100.0,        # seconds per editor call (Claude Code's MCP timeout is ~120 s)
    "launch_timeout": 900.0,      # first launch may compile shaders for a long time
    "channel_ttl": 600.0,         # reuse a discovered channel for this many seconds
    "shot_res": [960, 540],       # default screenshot size (token-friendly)
    "shot_timeout": 45.0,
    "reload_min_actors": 1,       # completion gate requires a reload check for level work
    "max_delete": 50,
    "allow_degraded_engine": True,
}

ENV = {"UED_TRANSPORT": "transport", "UED_MCP_URL": "mcp_url", "UED_CHUNK_SIZE": "chunk_size",
       "UED_CALL_TIMEOUT": "call_timeout"}


def load(project_dir):
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in (read_json(os.path.join(project_dir, ".uedirect.json"), {}) or {}).items()
                if k in DEFAULTS})
    for env, key in ENV.items():
        if os.environ.get(env):
            val = os.environ[env]
            cfg[key] = type(DEFAULTS[key])(val) if isinstance(DEFAULTS[key], (int, float)) else val
    return cfg
