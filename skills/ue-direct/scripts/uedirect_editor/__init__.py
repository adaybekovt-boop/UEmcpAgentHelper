"""UE-Direct editor-side runtime.

This package runs INSIDE Unreal Editor's embedded Python (PythonScriptPlugin).
It is imported by a tiny bootstrap snippet sent over whichever transport is
available (Epic's Unreal MCP ``execute_python``, Python remote execution, or a
third-party MCP python tool relayed by the agent).

Contract: the host writes a request JSON file, the snippet calls
``run_request(path)``, and this module writes ``<path>.out.json`` next to it.
Results travel through files on the local disk, so transport output size and
log spam stay minimal, and a dropped connection after execution does not lose
the result.

Only the Python standard library and the ``unreal`` module are used.
"""

import importlib
import json
import os
import sys
import time
import traceback

__version__ = "0.1.0"

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_SUBMODULES = ("common", "inspect_scene", "batch", "persist", "capture", "assets", "selftest")


def _signature():
    sig = []
    for name in sorted(os.listdir(_PKG_DIR)):
        if name.endswith(".py"):
            try:
                st = os.stat(os.path.join(_PKG_DIR, name))
                sig.append((name, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return tuple(sig)


_LOADED_SIG = _signature()


def ensure_fresh():
    """Reload the package if its files changed since import. Returns the live module."""
    if _signature() == _LOADED_SIG:
        return sys.modules[__name__]
    for sub in _SUBMODULES:
        full = __name__ + "." + sub
        if full in sys.modules:
            importlib.reload(sys.modules[full])
    return importlib.reload(sys.modules[__name__])


def _write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def _dispatch(req):
    from . import assets, batch, capture, common, inspect_scene, persist, selftest

    table = {
        "state": (common.cmd_state, False),
        "inspect": (inspect_scene.cmd_inspect, False),
        "actors": (inspect_scene.cmd_actors, False),
        "batch": (batch.cmd_batch, True),
        "level_new": (persist.cmd_level_new, True),
        "level_open": (persist.cmd_level_open, True),
        "save": (persist.cmd_save, True),
        "verify": (persist.cmd_verify, True),  # may reload the level: never during PIE
        "shot": (capture.cmd_shot, False),
        "camera_restore": (capture.cmd_camera_restore, False),
        "pie_start": (capture.cmd_pie_start, True),
        "pie_status": (capture.cmd_pie_status, False),
        "pie_stop": (capture.cmd_pie_stop, False),
        "assets_scan": (assets.cmd_assets_scan, False),
        "assets_exist": (assets.cmd_assets_exist, False),
        "selftest": (selftest.cmd_selftest, False),
    }
    cmd = req.get("cmd")
    if cmd not in table:
        raise common.UedError("UNKNOWN_CMD", "unknown command: %r" % (cmd,))
    fn, mutating = table[cmd]
    ctx = common.Context(req)
    common.guard(ctx, mutating=mutating, cmd=cmd)
    result = fn(ctx, req.get("args") or {})
    return ctx, result


def run_request(path):
    """Execute the request stored at ``path``; write ``path + '.out.json'``."""
    out_path = path + ".out.json"
    started = time.time()
    req = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            req = json.load(fh)
        from . import common

        ctx, result = _dispatch(req)
        payload = {
            "id": req.get("id"),
            "ok": True,
            "result": result,
            "state": common.compact_state(ctx),
            "ms": int((time.time() - started) * 1000),
            "editor_version": __version__,
        }
    except Exception as exc:  # noqa: BLE001 - report every failure as data
        code = getattr(exc, "code", "EXCEPTION")
        payload = {
            "id": req.get("id"),
            "ok": False,
            "error": {
                "code": code,
                "message": str(exc)[:2000],
                "trace": traceback.format_exc()[-4000:] if code == "EXCEPTION" else None,
            },
            "ms": int((time.time() - started) * 1000),
            "editor_version": __version__,
        }
        try:
            from . import common

            payload["state"] = common.compact_state(None)
        except Exception:  # noqa: BLE001
            pass
    _write_json_atomic(out_path, payload)
    return "UED_DONE %s ok=%s" % (req.get("id"), payload["ok"])
