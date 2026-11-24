"""Shared helpers for the editor-side runtime (runs inside Unreal Editor)."""

import fnmatch
import json
import os
import sys
import time

import unreal

TAG_PREFIX = "ued:"


class UedError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code


# ---------------------------------------------------------------- subsystems

def actor_ss():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def level_ss():
    return unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)


def editor_ss():
    return unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)


def asset_registry():
    return unreal.AssetRegistryHelpers.get_asset_registry()


def editor_world():
    return editor_ss().get_editor_world()


def editor_session_id():
    sid = getattr(sys, "_ued_editor_session", None)
    if not sid:
        sid = "%d-%d" % (os.getpid(), int(time.time()))
        sys._ued_editor_session = sid
    return sid


def norm_path(p):
    if not p:
        return ""
    p = os.path.abspath(p).replace("\\", "/")
    return p.lower() if os.name == "nt" else p


def project_file():
    p = unreal.Paths.get_project_file_path()
    try:
        p = unreal.Paths.convert_relative_path_to_full(p)
    except Exception:  # noqa: BLE001
        pass
    return os.path.abspath(p)


def current_level_path():
    world = editor_world()
    if world is None:
        return None
    return world.get_outermost().get_name()


def dirty_package_names():
    maps = [p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_map_packages()]
    content = [p.get_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()]
    return maps, content


def in_pie():
    try:
        return bool(level_ss().is_in_play_in_editor())
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ context

class Context(object):
    """Per-request context: expectations, evidence sink, caches."""

    def __init__(self, req):
        self.req = req or {}
        self.expect = self.req.get("expect") or {}
        self.state_dir = self.req.get("state_dir")
        self.task_id = self.req.get("task_id")
        self.touched = set(self.req.get("touched") or [])
        self._index = None
        self._assets = {}

    # evidence is written by the editor process itself, so the completion gate
    # does not depend on what the agent claims.
    def evidence(self, kind, **data):
        if not self.state_dir:
            return
        rec = {"t": time.time(), "src": "editor", "kind": kind, "session": editor_session_id(),
               "req": self.req.get("id"), "task": self.task_id}
        rec.update(data)
        try:
            with open(os.path.join(self.state_dir, "evidence.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        except OSError:
            pass

    def touch_actor(self, actor):
        try:
            self.touched.add(actor.get_package().get_name())
        except Exception:  # noqa: BLE001
            pass
        lvl = current_level_path()
        if lvl:
            self.touched.add(lvl)

    def touch_package(self, name):
        if name:
            self.touched.add(name)

    # -------------------------------------------------------- actor index
    def index(self, refresh=False):
        if self._index is None or refresh:
            self._index = ActorIndex(actor_ss().get_all_level_actors())
        return self._index

    def load_asset(self, path):
        if path in self._assets:
            return self._assets[path]
        obj = None
        if unreal.EditorAssetLibrary.does_asset_exist(path):
            obj = unreal.EditorAssetLibrary.load_asset(path)
        self._assets[path] = obj
        return obj


def compact_state(ctx):
    try:
        maps, content = dirty_package_names()
    except Exception:  # noqa: BLE001
        maps, content = [], []
    st = {
        "session": editor_session_id(),
        "level": current_level_path(),
        "pie": in_pie(),
        "dirty_maps": len(maps),
        "dirty_content": len(content),
    }
    if ctx is not None:
        st["touched"] = sorted(ctx.touched)
    return st


def cmd_state(ctx, args):
    ar = asset_registry()
    try:
        loading = bool(ar.is_loading_assets())
    except Exception:  # noqa: BLE001
        loading = False
    maps, content = dirty_package_names()
    return {
        "project_file": project_file(),
        "engine_version": unreal.SystemLibrary.get_engine_version(),
        "level": current_level_path(),
        "world_ready": editor_world() is not None,
        "asset_registry_loading": loading,
        "pie": in_pie(),
        "dirty_maps": maps[:20],
        "dirty_content": content[:20],
        "dirty_content_count": len(content),
        "python": sys.version.split()[0],
    }


def guard(ctx, mutating, cmd):
    """Execution Guard (in-editor half). Runs before every command."""
    if cmd in ("state", "selftest"):
        return
    if editor_world() is None:
        raise UedError("GUARD_NO_WORLD", "editor world is not ready")
    want = ctx.expect.get("project")
    if want and norm_path(want) != norm_path(project_file()):
        raise UedError("GUARD_WRONG_PROJECT", "editor has %s open, expected %s" % (project_file(), want))
    if mutating and in_pie():
        raise UedError("GUARD_PIE", "Play-In-Editor is running; stop PIE before editing")
    lvl = ctx.expect.get("level")
    if lvl and mutating and cmd not in ("level_new", "level_open") and lvl != current_level_path():
        raise UedError("GUARD_WRONG_LEVEL", "current level is %s, expected %s" % (current_level_path(), lvl))


# -------------------------------------------------------------- actor index

def actor_ued_id(actor):
    try:
        for tag in actor.tags:
            s = str(tag)
            if s.startswith(TAG_PREFIX):
                return s[len(TAG_PREFIX):]
    except Exception:  # noqa: BLE001
        pass
    return None


def actor_label(actor):
    try:
        return actor.get_actor_label()
    except Exception:  # noqa: BLE001
        return actor.get_name()


def actor_folder(actor):
    try:
        return str(actor.get_folder_path())
    except Exception:  # noqa: BLE001
        return ""


class ActorIndex(object):
    def __init__(self, actors):
        self.actors = [a for a in actors if a is not None]
        self.by_id = {}
        for a in self.actors:
            uid = actor_ued_id(a)
            if uid is not None:
                self.by_id[uid] = a

    def add(self, uid, actor):
        self.actors.append(actor)
        if uid is not None:
            self.by_id[uid] = actor

    def remove(self, actor):
        self.actors = [a for a in self.actors if a != actor]
        for k, v in list(self.by_id.items()):
            if v == actor:
                del self.by_id[k]

    def select(self, selector):
        """Resolve a selector string to a list of actors.

        Forms: ``id:<ued id or glob>``, ``label:<glob>``, ``tag:<tag>``,
        ``class:<ClassName>``, ``path:<object path>``, ``folder:<glob>``.
        A bare string is treated as ``label:``.
        """
        if not selector:
            raise UedError("BAD_SELECTOR", "empty selector")
        kind, _, pat = selector.partition(":")
        if not _:
            kind, pat = "label", selector
        kind = kind.lower()
        has_glob = any(c in pat for c in "*?[")
        if kind == "id":
            if not has_glob:
                a = self.by_id.get(pat)
                return [a] if a is not None else []
            return [a for k, a in self.by_id.items() if fnmatch.fnmatchcase(k, pat)]
        if kind == "label":
            return [a for a in self.actors if fnmatch.fnmatchcase(actor_label(a), pat)]
        if kind == "tag":
            return [a for a in self.actors if any(fnmatch.fnmatchcase(str(t), pat) for t in a.tags)]
        if kind == "class":
            return [a for a in self.actors if fnmatch.fnmatchcase(a.get_class().get_name(), pat)]
        if kind == "path":
            return [a for a in self.actors if fnmatch.fnmatchcase(a.get_path_name(), pat)]
        if kind == "folder":
            return [a for a in self.actors if fnmatch.fnmatchcase(actor_folder(a), pat)]
        raise UedError("BAD_SELECTOR", "unknown selector kind %r" % kind)


# ---------------------------------------------------------- value helpers

def vec(v, default=(0.0, 0.0, 0.0)):
    if v is None:
        v = default
    if isinstance(v, (int, float)):
        v = (v, v, v)
    if isinstance(v, dict):
        v = (v.get("x", 0.0), v.get("y", 0.0), v.get("z", 0.0))
    return unreal.Vector(float(v[0]), float(v[1]), float(v[2]))


def rot(v):
    """Rotation: number = yaw; [x, y, z] = [roll, pitch, yaw] as in the Details panel;
    or a dict with roll/pitch/yaw keys."""
    if v is None:
        return unreal.Rotator(roll=0.0, pitch=0.0, yaw=0.0)
    if isinstance(v, (int, float)):
        return unreal.Rotator(roll=0.0, pitch=0.0, yaw=float(v))
    if isinstance(v, dict):
        return unreal.Rotator(roll=float(v.get("roll", 0.0)), pitch=float(v.get("pitch", 0.0)),
                              yaw=float(v.get("yaw", 0.0)))
    return unreal.Rotator(roll=float(v[0]), pitch=float(v[1]), yaw=float(v[2]))


def vec_list(v, nd=1):
    return [round(v.x, nd), round(v.y, nd), round(v.z, nd)]


def rot_list(r, nd=1):
    return [round(r.roll, nd), round(r.pitch, nd), round(r.yaw, nd)]


def resolve_class(name):
    """Resolve an actor/component class from a short name, script path or BP class path."""
    if name.startswith("/"):
        cls = unreal.load_class(None, name)
        if cls is None and not name.endswith("_C"):
            base = name.rsplit("/", 1)[-1].split(".")[0]
            cls = unreal.load_class(None, "%s.%s_C" % (name.split(".")[0], base))
        if cls is None:
            raise UedError("MISSING_CLASS", "class not found: %s" % name)
        return cls
    cls = getattr(unreal, name, None)
    if cls is None:
        raise UedError("MISSING_CLASS", "unknown class name: %s" % name)
    return cls


def convert_value(current, value, ctx):
    """Convert a JSON value to the type of an existing editor property."""
    if value is None:
        return None
    if isinstance(current, unreal.Vector):
        return vec(value)
    if isinstance(current, unreal.Rotator):
        return rot(value)
    if isinstance(current, unreal.LinearColor):
        c = list(value) + [1.0] * (4 - len(value))
        return unreal.LinearColor(float(c[0]), float(c[1]), float(c[2]), float(c[3]))
    if isinstance(current, unreal.Color):
        c = list(value) + [255] * (4 - len(value))
        return unreal.Color(r=int(c[0]), g=int(c[1]), b=int(c[2]), a=int(c[3]))
    if isinstance(current, unreal.EnumBase) and isinstance(value, str):
        member = getattr(type(current), value.upper(), None)
        if member is None:
            raise UedError("BAD_VALUE", "enum %s has no member %s" % (type(current).__name__, value))
        return member
    if isinstance(current, bool):
        return bool(value)
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, unreal.Name):
        return unreal.Name(str(value))
    if isinstance(current, unreal.Text):
        return unreal.Text(str(value))
    if isinstance(value, str) and value.startswith("/") and (current is None or isinstance(current, unreal.Object)):
        obj = ctx.load_asset(value.split(":")[0])
        if obj is None:
            raise UedError("MISSING_ASSET", "asset not found: %s" % value)
        return obj
    return value


def find_component(actor, name):
    """Find a component by object name, or by class name (first match)."""
    comps = actor.get_components_by_class(unreal.ActorComponent)
    for c in comps:
        if c.get_name() == name:
            return c
    for c in comps:
        if c.get_class().get_name() == name:
            return c
    raise UedError("MISSING_COMPONENT", "%s has no component %s" % (actor_label(actor), name))


def mesh_component(actor):
    comps = actor.get_components_by_class(unreal.MeshComponent)
    return comps[0] if comps else None


def sample(items, n=8):
    items = list(items)
    return items[:n] + (["...+%d" % (len(items) - n)] if len(items) > n else [])
