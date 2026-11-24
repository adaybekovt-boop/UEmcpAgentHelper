"""`ued` command line. Every command prints ONE compact JSON object.

Exit codes: 0 ok, 1 failed/incomplete/blocked, 2 usage error, 3 relay pending.
Full details of the last run of each command: Saved/UEDirect/last/<cmd>.json
"""

import argparse
import os
import sys
import time

from . import __version__, completion, gate, hooks, logs
from .assets import AssetCache
from .batch import chunks, load_plan, prepare, referenced_assets
from .persist import is_ue_package, scan_fake_assets, verify_saved_files
from .project import find_project
from .session import RelayPending, Session
from .transports.base import TransportError
from .util import UedHostError, emit, new_id, now, read_json, read_jsonl, write_json

MUTATING = {"batch", "level", "save"}


# ------------------------------------------------------------------ helpers

def _session(args):
    project = find_project(args.project)
    return Session(project, transport_override=args.transport)


def _keep(sess, name, payload):
    write_json(sess.path("last", name + ".json"), payload, pretty=True)


def _require_gate(sess, level=None):
    """Execution Guard (host half): mutating commands need a ready editor.
    In relay mode the host cannot call the editor synchronously; the in-editor
    guard (run before every command) enforces project/world/PIE checks instead."""
    if sess.cfg["transport"] == "relay":
        return {"status": "ready", "channel": "relay"}
    res = gate.run_gate(sess, expect_level=level)
    if res["status"] != "ready":
        raise UedHostError("GATE_BLOCKED", "editor gate is not ready", data={"gate": {
            k: res.get(k) for k in ("blockers", "hint", "editor") if res.get(k)}})
    return res


def _gate_cached(sess, max_age=120.0):
    """Reuse a recent successful gate (persistent session); otherwise run it."""
    if sess.cfg["transport"] == "relay":
        return
    last = [e for e in read_jsonl(sess.path("evidence.jsonl"))[-200:] if e.get("kind") == "gate"]
    if last and last[-1].get("status") == "ready" and now() - last[-1]["t"] < max_age:
        return
    _require_gate(sess)


# ----------------------------------------------------------------- commands

def cmd_doctor(args):
    sess = _session(args)
    p = sess.project
    res = gate.run_gate(sess)
    res["plugins"] = {"PythonScriptPlugin": p.plugin_enabled("PythonScriptPlugin"),
                      "ModelContextProtocol": p.plugin_enabled("ModelContextProtocol", default=True),
                      "python_remote_execution_ini": p.python_remote_settings()["enabled"]}
    res["config"] = {k: sess.cfg[k] for k in ("transport", "mcp_url", "chunk_size")}
    res["asset_cache"] = AssetCache(sess).status(check_fs=False)
    fakes = scan_fake_assets(p, limit=5)
    if fakes["fake"]:
        res["fake_assets"] = fakes
    return res, 0 if res["status"] == "ready" else 1


def cmd_gate(args):
    sess = _session(args)
    res = gate.run_gate(sess, expect_level=args.level)
    return res, 0 if res["status"] == "ready" else 1


def cmd_up(args):
    sess = _session(args)
    res = gate.run_gate(sess, expect_level=args.level, do_launch=True, wait=args.wait)
    if res["status"] == "ready" and args.level and res.get("level") != args.level:
        r = sess.request("level_open", {"path": args.level})["result"]
        res["level"] = r.get("level")
    return res, 0 if res["status"] == "ready" else 1


def cmd_status(args):
    sess = _session(args)
    task = completion.load_task(sess.dir)
    ch = read_json(sess.path("channel.json"), {}) or {}
    out = {"project": sess.project.name, "channel": ch.get("kind"),
           "channel_age_s": int(now() - ch["t"]) if ch.get("t") else None,
           "task": {k: task.get(k) for k in ("id", "kind", "status")} if task else None,
           "touched_packages": len(sess.touched()), "asset_cache": AssetCache(sess).status(check_fs=False)}
    if task and task.get("status") == "active":
        v = completion.evaluate(sess.dir, task)
        out["completion"] = {"status": v["status"], "missing": v["missing"]}
    return out, 0


def cmd_inspect(args):
    sess = _session(args)
    _gate_cached(sess)
    a = {"top": args.top}
    if args.since:
        a["since_revision"] = args.since
    resp = sess.request("inspect", a)
    return resp["result"], 0


def cmd_actors(args):
    sess = _session(args)
    _gate_cached(sess)
    fields = [f for f in (args.fields or "loc").split(",") if f]
    return sess.request("actors", {"select": args.select, "fields": fields, "limit": args.limit})["result"], 0


def cmd_level(args):
    sess = _session(args)
    _require_gate(sess)
    completion.ensure_task(sess.dir)
    cmd = "level_new" if args.action == "new" else "level_open"
    payload = {"path": args.path, "save_dirty": args.save_dirty}
    if args.action == "new":
        payload.update(partitioned=args.partitioned, template=args.template)
    resp = sess.request(cmd, payload)
    return resp["result"], 0


def cmd_batch(args):
    sess = _session(args)
    text = sys.stdin.read() if args.plan == "-" else open(args.plan, "r", encoding="utf-8").read()
    plan = load_plan(text)
    on_error = args.on_error or plan.get("on_error", "continue")
    ops, warnings, spawn_ids = prepare(plan)
    cache = AssetCache(sess)
    if cache.status(check_fs=True) == "fresh":
        known = cache.paths()
        missing = [p for p in referenced_assets(ops) if p not in known]
        if missing:
            return {"status": "failed", "code": "MISSING_ASSETS", "missing": missing[:10],
                    "suggestions": {m: cache.suggest(m) for m in missing[:5]},
                    "hint": "fix asset paths (`ued assets find`) - nothing was sent to the editor"}, 1
    if args.dry_run:
        return {"status": "dry_run", "ops": len(ops), "spawn": len(spawn_ids),
                "chunks": (len(ops) + sess.cfg["chunk_size"] - 1) // sess.cfg["chunk_size"],
                "warnings": warnings, "sample": ops[:3]}, 0
    _gate_cached(sess)
    completion.ensure_task(sess.dir)
    t0 = time.time()
    totals = {"created": 0, "modified": 0, "unchanged": 0, "deleted": 0, "failed": 0, "rolled_back": 0}
    failures, assets_new, n_chunks, stopped = [], [], 0, False
    for chunk in chunks(ops, sess.cfg["chunk_size"]):
        n_chunks += 1
        resp = sess.request("batch", {"ops": chunk, "on_error": on_error, "allow_delete": args.allow_delete,
                                      "max_delete": args.max_delete or sess.cfg["max_delete"],
                                      "label": plan.get("label")}, expect_level=plan.get("level"))
        r = resp["result"]
        for k in totals:
            totals[k] += int(r["counts"].get(k, 0))
        failures.extend(r.get("failed") or [])
        assets_new.extend(r.get("assets_created") or [])
        if r.get("stopped_early"):
            stopped = True
            break
    cache.merge(assets_new)
    for f in failures[:10]:
        if f.get("code") == "MISSING_ASSET" and cache.data:
            f["did_you_mean"] = cache.suggest(f["msg"].rsplit(" ", 1)[-1])
    status = "success" if not totals["failed"] else ("failed" if not (totals["created"] + totals["modified"])
                                                     else "partial")
    out = {"status": status, "level": resp["state"].get("level"), "ops": len(ops), "chunks": n_chunks}
    out.update({k: v for k, v in totals.items() if v or k in ("created", "modified", "failed")})
    if failures:
        out["failures"] = failures[:10]
    if stopped:
        out["stopped_early"] = True
    if warnings:
        out["warnings"] = warnings[:5]
    if args.save or args.verify:
        s, _ = _do_save(sess, scope="task")
        out["saved"] = s["status"] == "success"
        if not out["saved"]:
            out["save_problems"] = s.get("problems") or s.get("failed")
        if args.verify:
            v, _ = _do_verify(sess, reload=args.reload)
            out["verified"] = bool(v.get("ok"))
            if v.get("problems"):
                out["verify_problems"] = v["problems"][:5]
    out["ms"] = int((time.time() - t0) * 1000)
    _keep(sess, "batch", {"summary": out, "failures": failures, "warnings": warnings})
    return out, 0 if status == "success" and out.get("saved", True) and out.get("verified", True) else 1


def _do_save(sess, scope="task", save_as=None):
    t0 = now()
    payload = {"scope": scope, "list_packages": True}
    if save_as:
        payload["save_as"] = save_as
    r = sess.request("save", payload)["result"]
    created = [e for b in read_jsonl(sess.path("evidence.jsonl"))[-500:] if b.get("kind") == "batch"
               for e in (b.get("assets_created") or [])]
    files = verify_saved_files(sess.project, r["packages"], t0, level=r.get("level"),
                               created_assets=[c for c in created if c in r["packages"]])
    sess.journal("save_files", ok=files["ok"] and not r["failed"], problems=files["problems"] + r["failed"][:5],
                 files_ok=files["files_ok"])
    sess.set_touched(set(sess.touched()) - set(r["packages"]))
    ok = files["ok"] and not r["failed"] and (r.get("level_saved") is not False)
    out = {"status": "success" if ok else "failed", "saved": r["saved"], "level": r.get("level"),
           "files_verified": files["files_ok"], "failed": r["failed"][:10]}
    if files["deleted"]:
        out["deleted_external_actors"] = files["deleted"]
    if files["problems"]:
        out["problems"] = files["problems"]
    if r.get("user_dirty_left"):
        out["user_dirty_left"] = r["user_dirty_left"]
        out["note"] = "unrelated unsaved user packages were left untouched"
    return out, 0 if ok else 1


def cmd_save(args):
    sess = _session(args)
    _gate_cached(sess)
    return _do_save(sess, scope="all" if args.all else "task", save_as=args.save_as)


def _expected_ids(sess):
    task = completion.load_task(sess.dir) or {}
    started = task.get("started", 0)
    alive = set()
    for e in read_jsonl(sess.path("evidence.jsonl")):
        if e.get("t", 0) < started or e.get("src") != "editor" or e.get("kind") != "batch":
            continue
        alive |= set(e.get("spawn_ids") or [])
        alive -= set(e.get("deleted_ids") or [])
    return sorted(alive)


def _do_verify(sess, reload=False, level=None, min_actors=None):
    task = completion.load_task(sess.dir) or {}
    exp = task.get("expect") or {}
    log_path = logs.project_log(sess.project)
    off = logs.offset(log_path)
    args = {"reload": reload, "expect_level": level or exp.get("level"),
            "min_actors": min_actors if min_actors is not None else exp.get("min_actors"),
            "expect_ids": _expected_ids(sess)}
    r = sess.request("verify", args, timeout=300.0)["result"]
    if reload:
        found = logs.classify(logs.read_since(log_path, off))
        if found["counts"].get("missing_reference"):
            r["ok"] = False
            r.setdefault("problems", []).append("missing references while loading: %s" % found["examples"][:3])
            sess.journal("verify_log", ok=False, counts=found["counts"])
        r["log"] = found["counts"]
    return r, 0 if r.get("ok") else 1


def cmd_verify(args):
    sess = _session(args)
    _gate_cached(sess)
    return _do_verify(sess, reload=args.reload, level=args.level, min_actors=args.min_actors)


def cmd_shot(args):
    sess = _session(args)
    _gate_cached(sess)
    w, h = (int(x) for x in (args.res or "x".join(map(str, sess.cfg["shot_res"]))).lower().split("x"))
    shot_id = new_id("s")
    path = sess.path("shots", shot_id + ".png")
    t0 = now()
    payload = {"mode": args.mode, "res": [w, h], "file": path}
    if args.select:
        payload["select"] = args.select
    r = sess.request("shot", payload)["result"]
    found = _wait_png(path, sess.project, t0, sess.cfg["shot_timeout"])
    if not args.keep_camera and r.get("previous_camera") and args.mode != "current":
        try:
            sess.request("camera_restore", r["previous_camera"], retries=0)
        except (UedHostError, TransportError):
            pass
    ok = found is not None
    sess.journal("shot", ok=ok, shot=shot_id, file=found, mode=args.mode)
    if not ok:
        return {"status": "failed", "code": "SCREENSHOT_MISSING", "shot": shot_id,
                "hint": "viewport may not be rendering (editor minimized / 'Use Less CPU when in Background')"}, 1
    return {"status": "success", "shot": shot_id, "file": found, "res": [w, h], "mode": args.mode,
            "next": "view the image, then `ued review %s --verdict pass|fail --notes ...`" % shot_id}, 0


def _wait_png(path, project, since, timeout):
    """The capture happens on a later editor frame; poll for the file."""
    shots_dir = os.path.join(project.dir, "Saved", "Screenshots")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for cand in [path, path[:-4] + "_00000.png"]:
            if os.path.isfile(cand) and _png_complete(cand):
                return cand
        newest = None
        for dirpath, _, files in os.walk(shots_dir):
            for f in files:
                if f.lower().endswith(".png"):
                    p = os.path.join(dirpath, f)
                    if os.path.getmtime(p) >= since and (newest is None or os.path.getmtime(p) > os.path.getmtime(newest)):
                        newest = p
        if newest and _png_complete(newest):
            return newest
        time.sleep(0.5)
    return None


def _png_complete(p):
    try:
        with open(p, "rb") as fh:
            head = fh.read(8)
            fh.seek(-12, 2)
            tail = fh.read(12)
        return head == b"\x89PNG\r\n\x1a\n" and b"IEND" in tail
    except OSError:
        return False


def cmd_review(args):
    sess = _session(args)
    shots = [e for e in read_jsonl(sess.path("evidence.jsonl")) if e.get("kind") == "shot" and e.get("ok")]
    if not any(s["shot"] == args.shot for s in shots):
        raise UedHostError("UNKNOWN_SHOT", "no captured screenshot %s" % args.shot)
    if args.verdict == "pass" and not (args.notes or "").strip():
        raise UedHostError("NOTES_REQUIRED", "a pass verdict needs --notes describing what was checked "
                           "(composition, scale, lighting, materials, intersections)")
    sess.journal("review", shot=args.shot, verdict=args.verdict, notes=(args.notes or "")[:500])
    return {"status": "recorded", "shot": args.shot, "verdict": args.verdict}, 0


def cmd_pie(args):
    sess = _session(args)
    _require_gate(sess)
    log_path = logs.project_log(sess.project)
    off = logs.offset(log_path)
    sess.request("pie_start", {"simulate": args.simulate})
    t_end = time.time() + args.seconds
    started = False
    while time.time() < t_end:
        time.sleep(1.0)
        started = started or sess.request("pie_status", retries=0)["result"]["pie"]
    sess.request("pie_stop", retries=0)
    found = logs.classify(logs.read_since(log_path, off))
    errors = {k: v for k, v in found["counts"].items() if k in ("blueprint_runtime", "error", "python")}
    ok = started and not errors
    sess.journal("pie", ok=ok, started=started, errors=errors, seconds=args.seconds)
    out = {"status": "success" if ok else "failed", "pie_started": started, "errors": errors}
    if found["examples"]:
        out["examples"] = found["examples"][:5]
    return out, 0 if ok else 1


def cmd_assets(args):
    sess = _session(args)
    cache = AssetCache(sess)
    if args.action == "status":
        return {"cache": cache.status(), "assets": len(cache.paths())}, 0
    if args.action == "refresh":
        _gate_cached(sess)
        return cache.refresh(), 0
    info = None
    if cache.status() != "fresh":
        _gate_cached(sess)
        info = cache.ensure()
    res = cache.find(" ".join(args.query or []), cls=args.cls, cat=args.cat, under=args.under, limit=args.limit)
    if info:
        res["cache_refreshed"] = info
    return res, 0


def cmd_task(args):
    sess = _session(args)
    if args.action == "start":
        t = completion.start_task(sess.dir, args.kind, goal=args.goal, level=args.level, min_actors=args.min_actors,
                                  visual=args.visual, functional=args.functional)
        return {"task": t["id"], "kind": t["kind"], "requires": t["requires"]}, 0
    if args.action == "block":
        if not args.reason:
            raise UedHostError("REASON_REQUIRED", "--reason is required")
        t = completion.block_task(sess.dir, args.reason)
        return {"task": t["id"], "status": "blocked", "reason": args.reason}, 0
    if args.action == "waive":
        if not args.reason or not args.op_id:
            raise UedHostError("REASON_REQUIRED", "waive needs --op-id and --reason")
        sess.journal("waive", op_id=args.op_id, reason=args.reason)
        return {"waived": args.op_id}, 0
    t = completion.load_task(sess.dir)
    return {"task": t}, 0


def cmd_complete(args):
    sess = _session(args)
    v = completion.complete(sess.dir)
    sess.journal("complete_attempt", status=v["status"])
    return v, 0 if v["status"] == "success" else 1


def cmd_result(args):
    sess = _session(args)
    return sess.result(args.request), 0


def cmd_audit(args):
    sess = _session(args)
    res = scan_fake_assets(sess.project)
    res["status"] = "clean" if not res["fake"] else "fake_assets_found"
    return res, 0 if not res["fake"] else 1


def cmd_setup(args):
    """Explicit, user-requested project changes (backs up the .uproject first)."""
    import json
    import shutil
    sess = _session(args)
    p = sess.project
    changes = []
    data = dict(p.data)
    plugins = list(data.get("Plugins") or [])
    for flag, name in ((args.enable_python, "PythonScriptPlugin"), (args.enable_mcp, "ModelContextProtocol")):
        if flag and not p.plugin_enabled(name):
            plugins = [x for x in plugins if x.get("Name") != name] + [{"Name": name, "Enabled": True}]
            changes.append(name)
    if not changes:
        return {"status": "unchanged"}, 0
    shutil.copy2(p.uproject, p.uproject + ".bak")
    data["Plugins"] = plugins
    with open(p.uproject, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent="\t")
    return {"status": "changed", "enabled": changes, "backup": p.uproject + ".bak",
            "note": "restart the editor yourself when convenient (unsaved work stays your call)"}, 0


def cmd_check_file(args):
    return {"file": args.file, "ue_package": is_ue_package(args.file)}, 0


# --------------------------------------------------------------------- main

def build_parser():
    ap = argparse.ArgumentParser(prog="ued", description="UE-Direct: editor-first Unreal Engine execution harness")
    ap.add_argument("--project", help=".uproject path or project dir (default: search upward from cwd)")
    ap.add_argument("--transport", choices=["auto", "mcp", "remote", "relay"])
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--version", action="version", version="ue-direct " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    g = sub.add_parser("gate")
    g.add_argument("--level")
    g.set_defaults(fn=cmd_gate)
    u = sub.add_parser("up")
    u.add_argument("--level")
    u.add_argument("--wait", type=float)
    u.set_defaults(fn=cmd_up)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    i = sub.add_parser("inspect")
    i.add_argument("--since")
    i.add_argument("--top", type=int, default=8)
    i.set_defaults(fn=cmd_inspect)
    a = sub.add_parser("actors")
    a.add_argument("select")
    a.add_argument("--fields", help="comma list: loc,transform,mesh,materials,folder,tags,bounds,path")
    a.add_argument("--limit", type=int, default=25)
    a.set_defaults(fn=cmd_actors)
    lv = sub.add_parser("level")
    lv.add_argument("action", choices=["new", "open"])
    lv.add_argument("path")
    lv.add_argument("--partitioned", action="store_true")
    lv.add_argument("--template")
    lv.add_argument("--save-dirty", action="store_true", help="save dirty maps first (only with user consent)")
    lv.set_defaults(fn=cmd_level)
    b = sub.add_parser("batch")
    b.add_argument("plan", help="plan JSON file or - for stdin")
    b.add_argument("--on-error", choices=["continue", "stop", "rollback"])
    b.add_argument("--allow-delete", action="store_true")
    b.add_argument("--max-delete", type=int)
    b.add_argument("--save", action="store_true")
    b.add_argument("--verify", action="store_true")
    b.add_argument("--reload", action="store_true")
    b.add_argument("--dry-run", action="store_true")
    b.set_defaults(fn=cmd_batch)
    s = sub.add_parser("save")
    s.add_argument("--all", action="store_true", help="also save unrelated dirty packages")
    s.add_argument("--save-as")
    s.set_defaults(fn=cmd_save)
    v = sub.add_parser("verify")
    v.add_argument("--reload", action="store_true")
    v.add_argument("--level")
    v.add_argument("--min-actors", type=int)
    v.set_defaults(fn=cmd_verify)
    sh = sub.add_parser("shot")
    sh.add_argument("--mode", default="overview", choices=["overview", "top", "current"])
    sh.add_argument("--select")
    sh.add_argument("--res", help="WxH, default 960x540")
    sh.add_argument("--keep-camera", action="store_true")
    sh.set_defaults(fn=cmd_shot)
    rv = sub.add_parser("review")
    rv.add_argument("shot")
    rv.add_argument("--verdict", required=True, choices=["pass", "fail"])
    rv.add_argument("--notes")
    rv.set_defaults(fn=cmd_review)
    pie = sub.add_parser("pie")
    pie.add_argument("--seconds", type=float, default=8.0)
    pie.add_argument("--simulate", action="store_true")
    pie.set_defaults(fn=cmd_pie)
    asx = sub.add_parser("assets")
    asx.add_argument("action", choices=["find", "refresh", "status"])
    asx.add_argument("query", nargs="*")
    asx.add_argument("--class", dest="cls")
    asx.add_argument("--cat")
    asx.add_argument("--under")
    asx.add_argument("--limit", type=int, default=15)
    asx.set_defaults(fn=cmd_assets)
    t = sub.add_parser("task")
    t.add_argument("action", choices=["start", "block", "waive", "show"])
    t.add_argument("--kind", default="level", choices=sorted(completion.KINDS))
    t.add_argument("--goal")
    t.add_argument("--level")
    t.add_argument("--min-actors", type=int)
    t.add_argument("--visual", action="store_true", default=None)
    t.add_argument("--functional", action="store_true", default=None)
    t.add_argument("--reason")
    t.add_argument("--op-id")
    t.set_defaults(fn=cmd_task)
    sub.add_parser("complete").set_defaults(fn=cmd_complete)
    r = sub.add_parser("result")
    r.add_argument("request")
    r.set_defaults(fn=cmd_result)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    su = sub.add_parser("setup")
    su.add_argument("--enable-python", action="store_true")
    su.add_argument("--enable-mcp", action="store_true")
    su.set_defaults(fn=cmd_setup)
    cf = sub.add_parser("check-file")
    cf.add_argument("file")
    cf.set_defaults(fn=cmd_check_file)
    hk = sub.add_parser("hook")
    hk.add_argument("event", choices=["pre-tool", "post-tool", "stop"])
    return ap


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["hook"] and len(argv) > 1:
        return hooks.main(argv[1])
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        payload, code = args.fn(args)
    except RelayPending as exc:
        emit(exc.as_dict(), args.pretty)
        return 3
    except UedHostError as exc:
        emit(exc.as_dict(), args.pretty)
        return 1
    except OSError as exc:
        emit({"status": "error", "code": "OS_ERROR", "message": str(exc)}, args.pretty)
        return 1
    if isinstance(payload, dict) and "result" in payload and "ok" in payload:  # raw editor response (ued result)
        payload = payload["result"]
    emit(payload, args.pretty)
    return code
