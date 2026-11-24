"""Task tracking + Completion Gate.

Success is computed only from evidence records (``evidence.jsonl``). Records
with ``src=editor`` are written by the editor process itself; records with
``src=host`` are written by the ued CLI after it checked something (files on
disk, screenshot files, logs). The agent's own claims are never evidence,
with one documented exception: the visual review verdict, which only the
model can produce (UE-Direct can prove a screenshot was captured after the
last change, not that it was looked at carefully).
"""

import os

from .util import UedHostError, new_id, now, read_json, read_jsonl, write_json

KINDS = {
    # kind: (needs_editor_changes, save, reload, visual, functional)
    "level": (True, True, True, True, False),
    "visual": (True, True, False, True, False),
    "content": (True, True, False, False, False),
    "functional": (True, True, False, False, True),
    "inspect": (False, False, False, False, False),
    "code": (False, False, False, False, False),
    "auto": (True, True, None, None, False),  # None = derived from what actually changed
}


def task_path(sdir):
    return os.path.join(sdir, "task.json")


def load_task(sdir):
    return read_json(task_path(sdir), None)


def start_task(sdir, kind, goal=None, level=None, min_actors=None, visual=None, functional=None, reload=None):
    if kind not in KINDS:
        raise UedHostError("BAD_KIND", "kind must be one of %s" % ", ".join(sorted(KINDS)))
    cur = load_task(sdir)
    if cur and cur.get("status") == "active":
        if cur.get("kind") == "auto":
            cur["status"] = "superseded"
            _archive(sdir, cur)
        else:
            raise UedHostError("TASK_ACTIVE", "task %s (%s) is still active" % (cur["id"], cur["kind"]),
                               hint="run `ued complete`, or `ued task block --reason ...`")
    needs, save, rel, vis, func = KINDS[kind]
    task = {"id": new_id("t"), "kind": kind, "goal": goal, "started": now(), "status": "active",
            "expect": {"level": level, "min_actors": min_actors},
            "requires": {"changes": needs, "save": save, "reload": rel if reload is None else reload,
                         "visual": vis if visual is None else visual,
                         "functional": func if functional is None else functional}}
    write_json(task_path(sdir), task)
    return task


def ensure_task(sdir):
    """Auto-start a task when a mutating command runs without one (enforcement fallback)."""
    t = load_task(sdir)
    if t and t.get("status") == "active":
        return t
    return start_task(sdir, "auto")


def block_task(sdir, reason):
    t = load_task(sdir)
    if not t or t.get("status") != "active":
        raise UedHostError("NO_TASK", "no active task")
    t.update(status="blocked", reason=reason, ended=now())
    write_json(task_path(sdir), t)
    _archive(sdir, t)
    return t


def _archive(sdir, task):
    with open(os.path.join(sdir, "tasks.jsonl"), "a", encoding="utf-8") as fh:
        import json
        fh.write(json.dumps(task, separators=(",", ":")) + "\n")


def evaluate(sdir, task=None):
    """Return the completion verdict for the active (or given) task."""
    task = task or load_task(sdir)
    if not task:
        return {"status": "no_task"}
    ev = [e for e in read_jsonl(os.path.join(sdir, "evidence.jsonl")) if e.get("t", 0) >= task["started"]]
    req = dict(task["requires"])
    expect = task.get("expect") or {}

    def editor(kind):
        return [e for e in ev if e.get("src") == "editor" and e.get("kind") == kind]

    def host(kind):
        return [e for e in ev if e.get("src") == "host" and e.get("kind") == kind]

    batches = editor("batch")
    level_news = editor("level_new")
    external = host("external_mutation")
    mutations = [e for e in batches if e.get("mutated")] + level_news + external
    last_mut = max([e["t"] for e in mutations], default=0.0)
    level_mutated = bool(level_news) or any(e.get("mutated") and _level_change(e) for e in batches) or \
        any(e.get("level_affecting", True) for e in external)
    if req["reload"] is None:
        req["reload"] = level_mutated
    if req["visual"] is None:
        req["visual"] = level_mutated

    checks, missing = {}, []
    totals = {"created": 0, "modified": 0, "deleted": 0, "failed": 0}
    for b in batches:
        for k in totals:
            totals[k] += int((b.get("counts") or {}).get(k, 0))

    # 1. work actually happened in the editor
    if req["changes"]:
        checks["editor_changes"] = bool(mutations)
        if not mutations:
            missing.append("no editor changes recorded for this task (work must go through the editor)")

    # 2. no unresolved op failures (a later successful run of the same id resolves it)
    failed = set()
    for b in sorted(batches, key=lambda e: e["t"]):
        for i in b.get("failed_ids") or []:
            if i:
                failed.add(i)
            else:
                failed.add("op@%s" % b["t"])
        failed -= set(b.get("ok_ids") or [])
    waived = {w.get("op_id") for w in host("waive")}
    failed -= waived
    checks["no_failed_ops"] = not failed
    if failed:
        missing.append("%d op(s) failed and were not fixed: %s" % (len(failed), sorted(failed)[:5]))

    # 3. native save after the last change
    if req["save"] and mutations:
        saves = [e for e in editor("save") if e["t"] > last_mut]
        good = [e for e in saves if not e.get("failed")]
        checks["saved"] = bool(good)
        if not good:
            missing.append("save after the last change (`ued save`)" if not saves else
                           "last save failed for %s" % saves[-1].get("failed")[:3])
        files = [e for e in host("save_files") if e["t"] > last_mut]
        checks["files_verified"] = bool(files) and files[-1].get("ok", False)
        if not checks["files_verified"]:
            missing.append("saved files not verified as Unreal packages (`ued save` does this)"
                           if not files else "saved-file check failed: %s" % files[-1].get("problems", [])[:3])

    # 4. persistence: reload from disk and compare
    verifies = [e for e in editor("verify") if e["t"] > last_mut]
    if req["reload"] and mutations:
        rel = [e for e in verifies if e.get("reload")]
        checks["reload_verified"] = bool(rel) and rel[-1].get("ok", False)
        if not checks["reload_verified"]:
            missing.append("reload verification (`ued verify --reload`)" if not rel else
                           "reload verification failed: %s" % rel[-1].get("problems", [])[:3])
    if expect.get("level"):
        hit = [e for e in verifies if e.get("level") == expect["level"]]
        checks["expected_level"] = bool(hit)
        if not hit:
            missing.append("no verification of the expected level %s" % expect["level"])
        elif expect.get("min_actors") is not None and hit[-1].get("actors", 0) < int(expect["min_actors"]):
            checks["expected_level"] = False
            missing.append("level has %s actors, expected >= %s" % (hit[-1].get("actors"), expect["min_actors"]))
    if verifies and not verifies[-1].get("ok", False) and "reload_verified" not in checks:
        checks["verify_ok"] = False
        missing.append("last verification reported problems: %s" % verifies[-1].get("problems", [])[:3])

    # 5. visual verification (screenshot after the last change + recorded review)
    if req["visual"] and mutations:
        shots = [e for e in host("shot") if e["t"] > last_mut and e.get("ok")]
        reviews = [e for e in host("review") if shots and e.get("shot") in {s["shot"] for s in shots}]
        checks["screenshot"] = bool(shots)
        last_review = reviews[-1] if reviews else None
        checks["visual_review"] = bool(last_review) and last_review.get("verdict") == "pass"
        if not shots:
            missing.append("screenshot after the last change (`ued shot`)")
        elif not last_review:
            missing.append("visual review of the latest screenshot (`ued review <shot> --verdict pass|fail`)")
        elif last_review.get("verdict") != "pass":
            missing.append("latest visual review failed: %s (fix, re-shoot, re-review)" % last_review.get("notes"))

    # 6. functional verification
    if req["functional"] and mutations:
        pies = [e for e in host("pie") if e["t"] > last_mut]
        checks["functional"] = bool(pies) and pies[-1].get("ok", False)
        if not checks["functional"]:
            missing.append("Play-In-Editor check (`ued pie`)" if not pies else
                           "PIE check failed: %s" % pies[-1].get("errors"))

    status = "success" if not missing else "incomplete"
    last_level = next((e.get("level") for e in reversed(ev) if e.get("level")), None)
    return {
        "status": status,
        "task": task["id"],
        "kind": task["kind"],
        "level": last_level,
        "created": totals["created"],
        "modified": totals["modified"],
        "deleted": totals["deleted"],
        "failed": len(failed),
        "saved": checks.get("saved", None) if req["save"] and mutations else None,
        "verified": (checks.get("reload_verified", True) and checks.get("files_verified", True)
                     and checks.get("verify_ok", True)) if mutations else None,
        "visual": checks.get("visual_review") if req["visual"] and mutations else None,
        "checks": checks,
        "missing": missing,
        "waived": sorted(i for i in waived if i),
    }


def _level_change(batch):
    return bool(batch.get("actor_mutated"))


def complete(sdir):
    task = load_task(sdir)
    if not task or task.get("status") != "active":
        raise UedHostError("NO_TASK", "no active task", hint="`ued task start --kind ...` before editor work")
    verdict = evaluate(sdir, task)
    if verdict["status"] == "success":
        task.update(status="complete", ended=now(), verdict=verdict)
        write_json(task_path(sdir), task)
        _archive(sdir, task)
    return verdict
