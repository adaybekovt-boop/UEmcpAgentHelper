"""Compact Scene Inspector (runs inside Unreal Editor).

The default summary is a fixed-size digest of the level: counts, categories,
top assets, bounds, dirty state and cheap defect heuristics. Per-actor detail
is only produced by ``actors`` with an explicit selector and limit.
"""

import hashlib

import unreal

from .common import (actor_folder, actor_label, actor_ued_id, current_level_path, dirty_package_names,
                     mesh_component, rot_list, vec_list)

_LIGHT_CLASSES = ("DirectionalLight", "PointLight", "SpotLight", "RectLight", "SkyLight")
_EXTREME = 5.0e6  # cm; beyond this an actor is almost certainly misplaced


def _category(cls_name):
    if cls_name in _LIGHT_CLASSES:
        return "light"
    if cls_name in ("StaticMeshActor",):
        return "static_mesh"
    if cls_name.endswith("_C"):
        return "blueprint"
    if "Landscape" in cls_name:
        return "landscape"
    if cls_name in ("SkyAtmosphere", "ExponentialHeightFog", "VolumetricCloud", "PostProcessVolume"):
        return "environment"
    if cls_name in ("PlayerStart",):
        return "gameplay"
    if "Niagara" in cls_name:
        return "vfx"
    if "Camera" in cls_name:
        return "camera"
    return "other"


def _mesh_path(actor):
    comp = mesh_component(actor)
    if comp is None:
        return None, False
    if isinstance(comp, unreal.StaticMeshComponent):
        mesh = comp.get_editor_property("static_mesh")
        return (mesh.get_path_name().split(".")[0] if mesh else None), True
    return None, False


def scan(actors):
    """One pass over actors producing the digest and a revision hash."""
    h = hashlib.sha1()
    classes, cats, assets = {}, {}, {}
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    issues = {"missing_mesh": [], "extreme_location": [], "stacked_duplicates": 0}
    seen_spots = {}
    ofpa = 0
    ued = 0
    for a in actors:
        cls = a.get_class().get_name()
        classes[cls] = classes.get(cls, 0) + 1
        cat = _category(cls)
        cats[cat] = cats.get(cat, 0) + 1
        loc = a.get_actor_location()
        key = (cls, round(loc.x, 0), round(loc.y, 0), round(loc.z, 0))
        h.update(("%s|%s|%.0f|%.0f|%.0f;" % (a.get_path_name(), cls, loc.x, loc.y, loc.z)).encode("utf-8"))
        if actor_ued_id(a) is not None:
            ued += 1
        try:
            if "/__ExternalActors__/" in a.get_package().get_name():
                ofpa += 1
        except Exception:  # noqa: BLE001
            pass
        if max(abs(loc.x), abs(loc.y), abs(loc.z)) > _EXTREME:
            issues["extreme_location"].append(actor_label(a))
            continue
        mesh, has_mesh_comp = _mesh_path(a)
        if has_mesh_comp and cls == "StaticMeshActor" and mesh is None:
            issues["missing_mesh"].append(actor_label(a))
        if mesh:
            assets[mesh] = assets.get(mesh, 0) + 1
            spot = key + (mesh,)
            if spot in seen_spots:
                issues["stacked_duplicates"] += 1
            seen_spots[spot] = True
        if cat in ("landscape", "environment", "light"):
            continue  # unbounded/huge actors would swamp the content bounds
        for i, v in enumerate((loc.x, loc.y, loc.z)):
            lo[i] = min(lo[i], v)
            hi[i] = max(hi[i], v)
    bounds = None if lo[0] == float("inf") else {"min": [round(v) for v in lo], "max": [round(v) for v in hi]}
    return {
        "revision": h.hexdigest()[:12],
        "classes": classes,
        "categories": cats,
        "assets": assets,
        "bounds": bounds,
        "issues": issues,
        "ofpa_actors": ofpa,
        "ued_actors": ued,
    }


def _top(d, n):
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def cmd_inspect(ctx, args):
    actors = ctx.index().actors
    digest = scan(actors)
    if args.get("since_revision") and args["since_revision"] == digest["revision"]:
        return {"unchanged": True, "revision": digest["revision"], "actors": len(actors)}
    maps, content = dirty_package_names()
    classes = digest["classes"]
    has = lambda c: classes.get(c, 0) > 0  # noqa: E731
    issues = digest["issues"]
    out = {
        "level": current_level_path(),
        "revision": digest["revision"],
        "actors": len(actors),
        "categories": digest["categories"],
        "top_classes": _top(classes, int(args.get("top", 8))),
        "top_assets": _top(digest["assets"], int(args.get("top", 8))),
        "bounds": digest["bounds"],
        "world_partition": digest["ofpa_actors"] > 0,
        "ued_actors": digest["ued_actors"],
        "lighting": {"directional": has("DirectionalLight"), "sky": has("SkyLight"),
                     "atmosphere": has("SkyAtmosphere"), "fog": has("ExponentialHeightFog")},
        "player_start": has("PlayerStart"),
        "dirty": {"maps": len(maps), "content": len(content)},
        "issues": {
            "missing_mesh": len(issues["missing_mesh"]),
            "extreme_location": len(issues["extreme_location"]),
            "stacked_duplicates": issues["stacked_duplicates"],
        },
    }
    examples = {k: v[:5] for k, v in issues.items() if isinstance(v, list) and v}
    if examples:
        out["issue_examples"] = examples
    return out


def describe_actor(a, fields):
    d = {"label": actor_label(a), "class": a.get_class().get_name()}
    uid = actor_ued_id(a)
    if uid is not None:
        d["id"] = uid
    if "transform" in fields or "loc" in fields:
        d["loc"] = vec_list(a.get_actor_location())
    if "transform" in fields:
        d["rot"] = rot_list(a.get_actor_rotation())
        s = a.get_actor_scale3d()
        d["scale"] = vec_list(s, 3)
    if "mesh" in fields:
        d["mesh"] = _mesh_path(a)[0]
    if "materials" in fields:
        comp = mesh_component(a)
        if comp is not None:
            mats = []
            for i in range(comp.get_num_materials()):
                m = comp.get_material(i)
                mats.append(m.get_path_name().split(".")[0] if m else None)
            d["materials"] = mats
    if "folder" in fields:
        d["folder"] = actor_folder(a)
    if "tags" in fields:
        d["tags"] = [str(t) for t in a.tags]
    if "bounds" in fields:
        origin, extent = a.get_actor_bounds(False)
        d["bounds"] = {"origin": vec_list(origin, 0), "extent": vec_list(extent, 0)}
    if "path" in fields:
        d["path"] = a.get_path_name()
    return d


def cmd_actors(ctx, args):
    sel = args.get("select") or "label:*"
    limit = int(args.get("limit", 25))
    fields = set(args.get("fields") or ["loc"])
    found = ctx.index().select(sel)
    return {
        "select": sel,
        "matched": len(found),
        "actors": [describe_actor(a, fields) for a in found[:limit]],
        "truncated": len(found) > limit,
    }
