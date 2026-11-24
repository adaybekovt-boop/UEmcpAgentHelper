"""Batch planning on the host: validation, generator expansion, ids, chunking.

Generators (scatter/grid/ring/line) are expanded locally with a seeded RNG, so
the agent writes one short op instead of hundreds of coordinates, and the
same plan always produces the same actors (and the same ids).
"""

import hashlib
import json
import math
import random

from .util import UedHostError

ACTOR_OPS = {"spawn", "transform", "set", "material", "mesh", "delete", "attach"}
ASSET_OPS = {"mi", "bp", "import", "duplicate_asset"}
GENERATORS = {"scatter", "grid", "ring", "line"}
REQUIRED = {
    "spawn": (), "transform": ("target",), "set": ("target",), "material": ("target",), "mesh": ("target", "mesh"),
    "delete": ("target",), "attach": ("target", "parent"), "mi": ("path",), "bp": ("path",),
    "import": ("file", "dest"), "duplicate_asset": ("source", "dest"),
    "scatter": ("id", "count"), "grid": ("id", "count", "step"), "ring": ("id", "count", "radius"),
    "line": ("id", "count", "start", "end"),
}
_SPAWN_KEYS = ("asset", "class", "folder", "tags", "material", "materials", "props", "comp_props", "mesh")


def load_plan(text):
    try:
        plan = json.loads(text)
    except ValueError as exc:
        raise UedHostError("BAD_PLAN", "plan is not valid JSON: %s" % exc)
    if isinstance(plan, list):
        plan = {"ops": plan}
    if not isinstance(plan, dict) or not isinstance(plan.get("ops"), list):
        raise UedHostError("BAD_PLAN", "plan must be a list of ops or an object with 'ops'")
    return plan


def auto_id(op):
    blob = json.dumps(op, sort_keys=True, separators=(",", ":"))
    return "a" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def _num(v, name):
    if not isinstance(v, (int, float)):
        raise UedHostError("BAD_PLAN", "%s must be a number" % name)
    return float(v)


def _range(v, rng, default):
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return rng.uniform(float(v[0]), float(v[1]))


def _spawn_from(gen, i, loc, yaw, scale, rng):
    op = {"op": "spawn", "id": "%s#%d" % (gen["id"], i), "loc": [round(c, 2) for c in loc]}
    for k in _SPAWN_KEYS:
        if k in gen:
            op[k] = gen[k]
    if gen.get("assets"):
        op["asset"] = gen["assets"][rng.randrange(len(gen["assets"]))]
    if yaw is not None:
        op["rot"] = round(yaw, 2)
    if scale is not None:
        op["scale"] = round(scale, 4)
    if gen.get("label"):
        op["label"] = "%s_%03d" % (gen["label"], i)
    return op


def expand(gen):
    """Expand one generator op into spawn ops. Returns (ops, warnings)."""
    kind = gen["op"]
    rng = random.Random(gen.get("seed", 0))
    count = int(gen["count"]) if not isinstance(gen["count"], list) else gen["count"]
    if "asset" not in gen and "assets" not in gen and "class" not in gen:
        raise UedHostError("BAD_PLAN", "%s '%s' needs asset, assets or class" % (kind, gen["id"]))
    out, warnings = [], []
    z = float(gen.get("z", 0.0))
    if kind == "scatter":
        area = gen.get("area") or {}
        if "radius" in area:
            cx, cy = area.get("center", [0, 0])[:2]
            r = _num(area["radius"], "area.radius")

            def sample_pt():
                a, d = rng.uniform(0, 2 * math.pi), r * math.sqrt(rng.random())
                return cx + d * math.cos(a), cy + d * math.sin(a)
        else:
            (x0, y0), (x1, y1) = area.get("min", [-1000, -1000])[:2], area.get("max", [1000, 1000])[:2]

            def sample_pt():
                return rng.uniform(x0, x1), rng.uniform(y0, y1)
        min_dist = float(gen.get("min_dist", 0.0))
        cell = min_dist / math.sqrt(2) if min_dist > 0 else 0
        grid = {}
        attempts = int(gen.get("max_attempts", 30)) * count
        placed = 0
        while placed < count and attempts > 0:
            attempts -= 1
            x, y = sample_pt()
            if min_dist > 0:
                gx, gy = int(math.floor(x / cell)), int(math.floor(y / cell))
                ok = True
                for dx in range(-2, 3):
                    for dy in range(-2, 3):
                        p = grid.get((gx + dx, gy + dy))
                        if p and (p[0] - x) ** 2 + (p[1] - y) ** 2 < min_dist ** 2:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    continue
                grid[(gx, gy)] = (x, y)
            out.append(_spawn_from(gen, placed, (x, y, z), _range(gen.get("yaw"), rng, None),
                                   _range(gen.get("scale"), rng, None), rng))
            placed += 1
        if placed < count:
            warnings.append("scatter '%s': placed %d of %d (min_dist %.0f too large for the area)"
                            % (gen["id"], placed, count, min_dist))
    elif kind == "grid":
        n = list(count) + [1] * (3 - len(count)) if isinstance(count, list) else [count, 1, 1]
        step = list(gen["step"]) + [0.0] * (3 - len(gen["step"]))
        ox, oy, oz = (list(gen.get("origin", [0, 0, 0])) + [0, 0, 0])[:3]
        jitter = float(gen.get("jitter", 0.0))
        i = 0
        for k in range(int(n[2])):
            for jy in range(int(n[1])):
                for jx in range(int(n[0])):
                    loc = (ox + jx * step[0] + rng.uniform(-jitter, jitter), oy + jy * step[1] +
                           rng.uniform(-jitter, jitter), oz + k * step[2])
                    out.append(_spawn_from(gen, i, loc, _range(gen.get("yaw"), rng, None),
                                           _range(gen.get("scale"), rng, None), rng))
                    i += 1
    elif kind == "ring":
        cx, cy, cz = (list(gen.get("center", [0, 0, 0])) + [0, 0, 0])[:3]
        r = _num(gen["radius"], "radius")
        for i in range(count):
            a = 2 * math.pi * i / count
            yaw = math.degrees(a) + 180.0 + float(gen.get("yaw_offset", 0.0)) if gen.get("face_center", True) \
                else _range(gen.get("yaw"), rng, None)
            out.append(_spawn_from(gen, i, (cx + r * math.cos(a), cy + r * math.sin(a), cz), yaw,
                                   _range(gen.get("scale"), rng, None), rng))
    elif kind == "line":
        s, e = (list(gen["start"]) + [0, 0, 0])[:3], (list(gen["end"]) + [0, 0, 0])[:3]
        for i in range(count):
            f = i / (count - 1) if count > 1 else 0.0
            loc = tuple(s[d] + (e[d] - s[d]) * f for d in range(3))
            yaw = gen.get("yaw")
            if yaw is None and gen.get("align", True):
                yaw = math.degrees(math.atan2(e[1] - s[1], e[0] - s[0]))
            out.append(_spawn_from(gen, i, loc, _range(yaw, rng, None), _range(gen.get("scale"), rng, None), rng))
    return out, warnings


def prepare(plan, max_ops=20000):
    """Validate + expand + assign ids. Returns (ops, warnings, expected_spawn_ids)."""
    ops, warnings = [], []
    for n, op in enumerate(plan["ops"]):
        if not isinstance(op, dict):
            raise UedHostError("BAD_PLAN", "op #%d is not an object" % n)
        kind = op.setdefault("op", "spawn")
        if kind not in ACTOR_OPS | ASSET_OPS | GENERATORS:
            raise UedHostError("BAD_PLAN", "op #%d: unknown op '%s'" % (n, kind))
        missing = [k for k in REQUIRED.get(kind, ()) if k not in op]
        if missing:
            raise UedHostError("BAD_PLAN", "op #%d (%s) is missing %s" % (n, kind, ", ".join(missing)))
        if kind in GENERATORS:
            expanded, w = expand(op)
            ops.extend(expanded)
            warnings.extend(w)
            continue
        if kind == "spawn" and "asset" not in op and "class" not in op:
            raise UedHostError("BAD_PLAN", "op #%d (spawn) needs asset or class" % n)
        # every op gets a stable id: spawn ids become actor tags (idempotent re-runs);
        # other ids let the completion gate match a failure with its later fix.
        op.setdefault("id", auto_id(op))
        ops.append(op)
        if len(ops) > max_ops:
            raise UedHostError("PLAN_TOO_LARGE", "plan expands to more than %d ops; split it" % max_ops)
    ids = [o["id"] for o in ops if o.get("op") == "spawn"]
    dupes = sorted({i for i in ids if ids.count(i) > 1}) if len(ids) < 5000 else []
    if dupes:
        raise UedHostError("BAD_PLAN", "duplicate op ids: %s" % dupes[:5])
    return ops, warnings, ids


def referenced_assets(ops):
    refs = set()
    created = {o["path"] for o in ops if o.get("op") in ("mi", "bp")}
    created |= {o["dest"] for o in ops if o.get("op") == "duplicate_asset"}
    for o in ops:
        for k in ("asset", "mesh", "material", "parent"):
            v = o.get(k)
            if isinstance(v, str) and v.startswith("/") and not v.startswith("/Script/"):
                refs.add(v)
        for v in (o.get("materials") or []):
            if v:
                refs.add(v)
        for v in (o.get("texture") or {}).values():
            refs.add(v)
    return sorted(refs - created)


def chunks(ops, size):
    size = max(1, int(size))
    for i in range(0, len(ops), size):
        yield ops[i:i + size]
