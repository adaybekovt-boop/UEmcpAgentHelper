"""Editor Gate + bootstrap.

A process named UnrealEditor is NOT proof of readiness. The gate is "ready"
only when the editor answered a command through a channel, reported the
expected project, has an editor world, finished the initial asset-registry
scan, runs a compatible engine and exposes every required Python API.
"""

import os
import time

from . import logs, procs
from .project import compat, editor_binary, parse_version, read_build_version, resolve_engine
from .transports.base import TransportError
from .transports.mcp_http import discover_urls
from .util import UedHostError, now, read_json, write_json

PY_REMOTE_INI = "-ini:Engine:[/Script/PythonScriptPlugin.PythonScriptPluginSettings]:bRemoteExecution=True"


def _norm(p):
    p = os.path.abspath(p or "").replace("\\", "/")
    return p.lower() if os.name == "nt" else p


def engine_info(project):
    try:
        root, src = resolve_engine(project)
    except UedHostError as exc:
        return {"root": None, "error": exc.code, "association": project.association}
    ver = read_build_version(root)
    return {"root": root, "source": src, "version": ".".join(map(str, ver)) if ver else None,
            "compat": compat(ver), "association": project.association}


def _state(session):
    resp = session.request("state", timeout=30.0, retries=0)
    return resp["result"], (resp.get("state") or {}).get("session")


def _selftest(session, editor_session):
    cache = read_json(session.path("selftest.json"), {}) or {}
    if cache.get("session") == editor_session:
        return cache["result"]
    res = session.request("selftest", timeout=30.0, retries=0)["result"]
    write_json(session.path("selftest.json"), {"session": editor_session, "result": res})
    return res


def evaluate(session, state, expect_level=None, sess_id=None):
    """Turn an editor state report into checks + blockers."""
    project = session.project
    checks, blockers, warnings = {}, [], []
    checks["project_match"] = _norm(state.get("project_file")) == _norm(project.uproject)
    if not checks["project_match"]:
        blockers.append({"code": "WRONG_PROJECT", "detail": "editor has %s open, expected %s"
                         % (state.get("project_file"), project.uproject)})
    ver = parse_version(state.get("engine_version"))
    c = compat(ver)
    checks["engine_compat"] = c
    if c == "incompatible" or (c == "degraded" and not session.cfg.get("allow_degraded_engine", True)):
        blockers.append({"code": "ENGINE_INCOMPATIBLE", "detail": "engine %s is not supported (need 5.8+%s)"
                         % (state.get("engine_version"), "" if c == "incompatible" else ", degraded mode disabled")})
    elif c == "degraded":
        warnings.append("engine %s predates 5.8: untested, Python remote execution only" % state.get("engine_version"))
    checks["world_ready"] = bool(state.get("world_ready"))
    if not checks["world_ready"]:
        blockers.append({"code": "EDITOR_NOT_READY", "detail": "no editor world yet"})
    checks["assets_ready"] = not state.get("asset_registry_loading")
    if not checks["assets_ready"]:
        blockers.append({"code": "EDITOR_NOT_READY", "detail": "asset registry is still scanning"})
    checks["pie_off"] = not state.get("pie")
    if not checks["pie_off"]:
        blockers.append({"code": "PIE_RUNNING", "detail": "Play-In-Editor is running; edits are refused until it stops"})
    if expect_level:
        checks["level_match"] = state.get("level") == expect_level
        if not checks["level_match"]:
            warnings.append("current level is %s, expected %s (use `ued level open`)" % (state.get("level"),
                                                                                          expect_level))
    if sess_id:
        st = _selftest(session, sess_id)
        checks["api_ok"] = st.get("ok", False)
        if not st.get("ok"):
            blockers.append({"code": "API_MISSING", "detail": "missing engine APIs: %s" % st.get("missing_required")})
        if st.get("missing_optional"):
            warnings.append("optional APIs missing (some ops disabled): %s" % st["missing_optional"][:6])
    return checks, blockers, warnings


def run_gate(session, expect_level=None, do_launch=False, wait=None):
    """Full gate. With launch=True, starts the project editor if none is running and waits for readiness."""
    project = session.project
    out = {"project": project.name}
    eng = engine_info(project)
    t0 = time.time()
    mine, others = procs.processes_for_project(procs.list_editor_processes(), project)
    out["editor"] = {"running": bool(mine), "pids": [p["pid"] for p in mine][:4], "other_projects": len(others)}
    launched = None
    channel_err = None
    try:
        session.select_transport(force_probe=True)
    except TransportError as exc:
        channel_err = exc
    if channel_err is not None:
        if do_launch and not mine:
            launched = launch(session, eng)
            out["editor"]["launched_pid"] = launched
        if do_launch or mine:
            budget = wait or (session.cfg["launch_timeout"] if (do_launch or launched) else 60.0)
            return _wait_ready(session, out, expect_level, launched, budget, t0)
        out["engine"] = eng
        out.update(status="blocked", blockers=[{
            "code": "EDITOR_CLOSED" if not mine else "NO_CHANNEL",
            "detail": "Unreal Editor is not running for this project" if not mine else
            "editor process exists but no channel answers (still starting? remote execution / MCP disabled?)"}],
            hint="run `ued up` (launches the editor and waits for readiness)")
        return out
    return _finish(session, out, expect_level, t0)


def _finish(session, out, expect_level, t0):
    try:
        state, sess_id = _state(session)
    except (TransportError, UedHostError) as exc:
        out.update(status="blocked", blockers=[{"code": getattr(exc, "code", "ERROR"), "detail": str(exc)}])
        return out
    info = session.probe_info() or {}
    checks, blockers, warnings = evaluate(session, state, expect_level, sess_id)
    out.update({
        "status": "ready" if not blockers else "blocked",
        "channel": info.get("transport") or session.cfg["transport"],
        "engine": "%s (%s)" % ((state.get("engine_version") or "?").split("-")[0], checks["engine_compat"]),
        "level": state.get("level"),
        "dirty": {"maps": len(state.get("dirty_maps") or []), "content": state.get("dirty_content_count", 0)},
    })
    failing = {k: v for k, v in checks.items() if v is False}
    if failing:
        out["failed_checks"] = sorted(failing)
    if not blockers:
        out.pop("editor", None)
    if blockers:
        out["blockers"] = blockers
    if warnings:
        out["warnings"] = warnings
    session.journal("gate", status=out["status"], level=state.get("level"), editor_session=sess_id,
                    blockers=[b["code"] for b in blockers])
    return out


def _wait_ready(session, out, expect_level, launched_pid, timeout, t0):
    deadline = time.time() + timeout
    last_reason = "waiting for editor channel"
    while time.time() < deadline:
        if launched_pid and not procs.pid_alive(launched_pid):
            out.update(status="blocked", blockers=[{"code": "EDITOR_EXITED",
                                                    "detail": "editor process exited during startup"}],
                       log=logs.tail_errors(logs.project_log(session.project)))
            return out
        try:
            session.select_transport(force_probe=True)
            res = _finish(session, dict(out), expect_level, t0)
            codes = {b["code"] for b in res.get("blockers", [])}
            if res["status"] == "ready" or not codes <= {"EDITOR_NOT_READY", "NO_EDITOR_CHANNEL", "TRANSPORT_UNAVAILABLE"}:
                return res
            last_reason = "; ".join(b["detail"] for b in res["blockers"])
        except TransportError as exc:
            last_reason = str(exc)
        time.sleep(3.0)
    out.update(status="blocked", blockers=[{"code": "EDITOR_TIMEOUT", "detail": "not ready after %ds: %s"
                                            % (timeout, last_reason)}],
               log=logs.tail_errors(logs.project_log(session.project)))
    return out


def launch(session, engine):
    """Start the editor for this project with the needed channels enabled via
    command-line overrides (project config files are not modified)."""
    project = session.project
    if not engine.get("root"):
        raise UedHostError("ENGINE_NOT_FOUND", "cannot launch: engine root unknown (%s)" % engine.get("error"),
                           hint="set UE_ENGINE_ROOT")
    if engine.get("compat") == "incompatible":
        raise UedHostError("ENGINE_INCOMPATIBLE", "engine %s is not supported" % engine.get("version"))
    binary = editor_binary(engine["root"])
    if not os.path.isfile(binary):
        raise UedHostError("EDITOR_BINARY_MISSING", "editor binary not found: %s" % binary)
    args = [project.uproject]
    if session.cfg["transport"] in ("auto", "mcp"):
        url = session.cfg.get("mcp_url") or discover_urls(project.dir)[0]
        port = url.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[-1]
        args += ["-ModelContextProtocolStartServer", "-ModelContextProtocolPort=%s" % port]
    if session.cfg["transport"] in ("auto", "remote") and not project.python_remote_settings()["enabled"]:
        args.append(PY_REMOTE_INI)
    log_path = session.path("editor_stdout.log")
    pid = procs.launch_editor(binary, args, log_path)
    write_json(session.path("launch.json"), {"pid": pid, "t": now(), "args": args})
    session.journal("launch", pid=pid, args=args)
    return pid
