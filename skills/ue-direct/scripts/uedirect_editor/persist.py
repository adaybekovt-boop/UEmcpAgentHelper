"""Native save, level switching and persistence verification (inside Unreal Editor).

Safety rules enforced here:
* ``load_level``/``new_level`` close the current level WITHOUT saving it, so
  they are refused while any map package is dirty, unless the caller asks to
  save first. Unsaved work is never discarded.
* Save defaults to the task scope: packages touched by UE-Direct plus the
  current level and its dirty external-actor packages. Unrelated dirty user
  packages are reported, not saved, unless ``scope='all'``.
* Persistence verification reloads the level from disk only when nothing is
  dirty, then compares fingerprints and the expected manifest.
"""

import unreal

from .common import (UedError, actor_label, actor_ued_id, current_level_path, dirty_package_names, level_ss,
                     sample, vec_list)
from .inspect_scene import scan


def _refuse_if_dirty(ctx, args, action):
    maps, content = dirty_package_names()
    if not maps:
        return
    if args.get("save_dirty"):
        ok = unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, False)
        maps_after, _ = dirty_package_names()
        if ok and not maps_after:
            ctx.evidence("save", scope="dirty_maps_before_" + action, saved=maps, failed=[], level=current_level_path())
            return
        raise UedError("SAVE_FAILED", "could not save dirty maps before %s: %s" % (action, maps_after))
    raise UedError("UNSAVED_CHANGES",
                   "%s would discard unsaved changes in %s. Ask the user, then retry with save_dirty=true."
                   % (action, sample(maps, 5)))


def cmd_level_new(ctx, args):
    path = args["path"]
    if unreal.EditorAssetLibrary.does_asset_exist(path):
        raise UedError("LEVEL_EXISTS", "%s already exists; open it instead of recreating it" % path)
    _refuse_if_dirty(ctx, args, "creating a new level")
    ls = level_ss()
    if args.get("template"):
        ok = ls.new_level_from_template(path, args["template"])
    else:
        ok = ls.new_level(path, bool(args.get("partitioned", False)))
    if not ok:
        raise UedError("LEVEL_CREATE_FAILED", "new_level returned False for %s" % path)
    if current_level_path() != path:
        raise UedError("LEVEL_CREATE_FAILED", "editor is on %s after creating %s" % (current_level_path(), path))
    if not unreal.EditorAssetLibrary.does_asset_exist(path):
        if not ls.save_current_level():
            raise UedError("SAVE_FAILED", "new level %s was created but could not be saved" % path)
    ctx.touch_package(path)
    ctx.evidence("level_new", level=path, mutated=True, partitioned=bool(args.get("partitioned", False)))
    return {"level": path, "exists_on_disk": unreal.EditorAssetLibrary.does_asset_exist(path)}


def cmd_level_open(ctx, args):
    path = args["path"]
    if current_level_path() == path and not args.get("reload"):
        return {"level": path, "already_open": True}
    if not unreal.EditorAssetLibrary.does_asset_exist(path):
        raise UedError("MISSING_LEVEL", "level not found: %s" % path)
    _refuse_if_dirty(ctx, args, "opening another level")
    if not level_ss().load_level(path):
        raise UedError("LEVEL_LOAD_FAILED", "load_level returned False for %s" % path)
    ctx._index = None
    return {"level": current_level_path(), "actors": len(ctx.index().actors)}


def _package_objects(names):
    by_name = {}
    for p in list(unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()) + \
            list(unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()):
        by_name[p.get_name()] = p
    return [by_name[n] for n in names if n in by_name]


def _ext_actor_root(level_path):
    # /Game/Maps/City -> /Game/__ExternalActors__/Maps/City/
    root, _, rest = level_path.lstrip("/").partition("/")
    return "/%s/__ExternalActors__/%s/" % (root, rest)


def _system_paths(names):
    out = {}
    for n in names:
        try:
            obj = unreal.find_package(n) if hasattr(unreal, "find_package") else None
            if obj is not None:
                out[n] = unreal.SystemLibrary.get_system_path(obj)
        except Exception:  # noqa: BLE001
            pass
    return out


def cmd_save(ctx, args):
    scope = args.get("scope", "task")
    level = current_level_path()
    if level and level.startswith("/Temp/"):
        if not args.get("save_as"):
            raise UedError("UNTITLED_LEVEL", "current level is untitled; pass save_as=/Game/Maps/Name")
        if not unreal.EditorLoadingAndSavingUtils.save_map(unreal.get_editor_subsystem(
                unreal.UnrealEditorSubsystem).get_editor_world(), args["save_as"]):
            raise UedError("SAVE_FAILED", "save_map failed for %s" % args["save_as"])
        level = current_level_path()
        ctx.touch_package(level)
    maps, content = dirty_package_names()
    dirty = set(maps) | set(content)
    baseline = args.get("baseline_dirty")  # dirty set when the task started (None = unknown)
    if scope == "all":
        targets = dirty
    else:
        targets = set(ctx.touched)
        if baseline is not None:
            targets |= dirty - set(baseline)  # everything dirtied during the task (incl. other tools)
        if level:
            targets.add(level)
            ext = _ext_actor_root(level)
            targets |= {n for n in dirty if n.startswith(ext)}
        targets &= dirty
    level_saved = None
    if level and level in targets:
        level_saved = bool(level_ss().save_current_level())
    remaining = [n for n in targets if n in set(sum(dirty_package_names(), []))]
    if remaining:
        unreal.EditorLoadingAndSavingUtils.save_packages(_package_objects(remaining), True)
    maps_after, content_after = dirty_package_names()
    dirty_after = set(maps_after) | set(content_after)
    failed = sorted(targets & dirty_after)
    saved = sorted(targets - dirty_after)
    untouched_user = sorted(dirty_after - targets)
    new_dirty_left = sorted(dirty_after - set(baseline)) if baseline is not None else None
    ctx.evidence("save", scope=scope, saved=saved, failed=failed, level=level, level_saved=level_saved,
                 user_dirty_left=len(untouched_user), new_dirty_left=new_dirty_left,
                 dirty_left=sorted(dirty_after)[:50])
    return {
        "saved": len(saved),
        "failed": failed[:20],
        "level": level,
        "level_saved": level_saved,
        "packages": saved if args.get("list_packages") else sample(saved, 10),
        "files": _system_paths(saved) if args.get("with_files") else None,
        "user_dirty_left": len(untouched_user),
        "new_dirty_left": new_dirty_left,
        "user_dirty_sample": sample(untouched_user, 5),
    }


def _fingerprint(ctx):
    ctx._index = None
    idx = ctx.index()
    digest = scan(idx.actors)
    ued = {}
    for uid, a in idx.by_id.items():
        ued[uid] = vec_list(a.get_actor_location(), 0)
    return {"actors": len(idx.actors), "classes": digest["classes"], "ued": ued, "issues": digest["issues"]}


def _wp_desc_count():
    try:
        return len(unreal.WorldPartitionBlueprintLibrary.get_actor_descs())
    except Exception:  # noqa: BLE001
        return None


def cmd_verify(ctx, args):
    """Verify the saved state of the current level.

    args: expect_level, min_actors, expect_ids (list), reload (bool), tolerance (cm)
    """
    level = current_level_path()
    checks, problems = {}, []
    exp_level = args.get("expect_level")
    if exp_level:
        checks["level_match"] = level == exp_level
        if not checks["level_match"]:
            problems.append("current level %s != expected %s" % (level, exp_level))
    checks["level_on_disk"] = bool(level) and unreal.EditorAssetLibrary.does_asset_exist(level)
    if not checks["level_on_disk"]:
        problems.append("level %s is not a saved asset" % level)
    maps, content = dirty_package_names()
    # Reloading a level only discards map packages and that level's external actors;
    # unrelated dirty content (often the user's) survives and must not block verification.
    ext = _ext_actor_root(level) if level else None
    level_dirty = maps + [c for c in content if ext and c.startswith(ext)]
    checks["clean"] = not level_dirty
    before = _fingerprint(ctx)
    wp_before = _wp_desc_count()
    reload_done = False
    if args.get("reload"):
        if not checks["clean"]:
            problems.append("reload skipped: unsaved level packages (%s); save first" % sample(level_dirty, 5))
        elif not checks["level_on_disk"]:
            problems.append("reload skipped: level not on disk")
        else:
            if not level_ss().load_level(level):
                problems.append("reload failed: load_level returned False")
            else:
                reload_done = True
    after = _fingerprint(ctx) if reload_done else before
    if reload_done:
        checks["reload_same_count"] = after["actors"] == before["actors"]
        checks["reload_same_classes"] = after["classes"] == before["classes"]
        wp_after = _wp_desc_count()
        if wp_before is not None and wp_after is not None:
            checks["wp_descs_same"] = wp_before == wp_after
        lost = sorted(set(before["ued"]) - set(after["ued"]))
        moved = [k for k, v in before["ued"].items() if k in after["ued"] and
                 max(abs(a - b) for a, b in zip(v, after["ued"][k])) > float(args.get("tolerance", 1.0))]
        checks["reload_ued_intact"] = not lost and not moved
        if lost:
            problems.append("%d tagged actors missing after reload: %s" % (len(lost), sample(lost, 5)))
        if moved:
            problems.append("%d tagged actors moved after reload: %s" % (len(moved), sample(moved, 5)))
        if not checks["reload_same_count"]:
            problems.append("actor count %d before reload, %d after" % (before["actors"], after["actors"]))
    expect_ids = args.get("expect_ids") or []
    if isinstance(expect_ids, dict):
        expect_ids = expect_ids.get(level) or []
    if expect_ids:
        missing = [i for i in expect_ids if i not in after["ued"]]
        checks["expected_ids_present"] = not missing
        if missing:
            problems.append("%d expected actors missing: %s" % (len(missing), sample(missing, 5)))
    if args.get("min_actors") is not None:
        checks["min_actors"] = after["actors"] >= int(args["min_actors"])
        if not checks["min_actors"]:
            problems.append("only %d actors, expected >= %s" % (after["actors"], args["min_actors"]))
    crit = len(after["issues"]["missing_mesh"]) + len(after["issues"]["extreme_location"])
    checks["no_critical_issues"] = crit == 0
    if crit:
        problems.append("critical scene issues: missing_mesh=%s extreme_location=%s" % (
            sample(after["issues"]["missing_mesh"], 3), sample(after["issues"]["extreme_location"], 3)))
    ok = all(checks.values()) and not problems
    ctx.evidence("verify", ok=ok, level=level, reload=reload_done, checks=checks, actors=after["actors"],
                 problems=problems[:10])
    return {"ok": ok, "level": level, "reloaded": reload_done, "actors": after["actors"], "checks": checks,
            "problems": problems[:10]}


# re-exported for tests
__all__ = ["cmd_level_new", "cmd_level_open", "cmd_save", "cmd_verify", "actor_label", "actor_ued_id"]
