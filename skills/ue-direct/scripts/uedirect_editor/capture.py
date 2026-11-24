"""Screenshots and Play-In-Editor control (inside Unreal Editor).

Screenshots are asynchronous: ``take_high_res_screenshot`` returns a task and
the image is written on a later frame. The game thread must be released for
the frame to render, so this command only frames the camera and requests the
capture; the host polls for the file and verifies it.
"""

import math
import os

import unreal

from .common import UedError, actor_ss, editor_ss, editor_world, in_pie, level_ss, vec_list
from .inspect_scene import _category


def _bounds_of(actors):
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    n = 0
    for a in actors:
        cls = a.get_class().get_name()
        if _category(cls) in ("landscape", "environment", "light"):
            continue
        origin, extent = a.get_actor_bounds(False)
        if max(abs(extent.x), abs(extent.y), abs(extent.z)) > 1.0e6:
            continue
        n += 1
        for i, (o, e) in enumerate(((origin.x, extent.x), (origin.y, extent.y), (origin.z, extent.z))):
            lo[i] = min(lo[i], o - e)
            hi[i] = max(hi[i], o + e)
    if not n:
        return None
    return lo, hi


def _frame(lo, hi, mode, fov_deg=90.0):
    cx, cy, cz = [(a + b) / 2.0 for a, b in zip(lo, hi)]
    radius = max(50.0, 0.5 * math.sqrt(sum((b - a) ** 2 for a, b in zip(lo, hi))))
    dist = radius / math.tan(math.radians(fov_deg) / 2.0) * 1.1
    if mode == "top":
        loc = unreal.Vector(cx, cy, cz + dist)
        return loc, unreal.Rotator(roll=0.0, pitch=-90.0, yaw=0.0)
    # 3/4 overview: from the south-west, 35 degrees down
    pitch = math.radians(35.0)
    yaw = math.radians(45.0)
    loc = unreal.Vector(cx - dist * math.cos(pitch) * math.cos(yaw),
                        cy - dist * math.cos(pitch) * math.sin(yaw),
                        cz + dist * math.sin(pitch))
    return loc, unreal.MathLibrary.find_look_at_rotation(loc, unreal.Vector(cx, cy, cz))


def cmd_shot(ctx, args):
    """args: mode=overview|top|current|camera, select=<selector>, res=[w,h], file=<abs path>,
    loc/rot for mode=camera."""
    w, h = [int(x) for x in (args.get("res") or [960, 540])]
    if w * h > 3840 * 2160:
        raise UedError("BAD_ARGS", "resolution too large")
    path = args.get("file")
    if not path:
        raise UedError("BAD_ARGS", "file is required")
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    mode = args.get("mode", "overview")
    prev = editor_ss().get_level_viewport_camera_info()
    previous = None
    if prev:
        previous = {"loc": vec_list(prev[0], 2), "rot": [prev[1].roll, prev[1].pitch, prev[1].yaw]}
    framed = None
    if mode in ("overview", "top"):
        actors = ctx.index().select(args["select"]) if args.get("select") else ctx.index().actors
        b = _bounds_of(actors)
        if b is None:
            raise UedError("NOTHING_TO_FRAME", "no boundable actors for screenshot")
        loc, r = _frame(b[0], b[1], mode)
        editor_ss().set_level_viewport_camera_info(loc, r)
        framed = {"loc": vec_list(loc, 0), "rot": [round(r.roll, 1), round(r.pitch, 1), round(r.yaw, 1)]}
    elif mode == "camera":
        from .common import rot, vec
        editor_ss().set_level_viewport_camera_info(vec(args["loc"]), rot(args["rot"]))
        framed = {"loc": args["loc"], "rot": args["rot"]}
    elif mode != "current":
        raise UedError("BAD_ARGS", "unknown mode %s" % mode)
    method = "automation"
    try:
        unreal.AutomationLibrary.take_high_res_screenshot(w, h, path)
    except Exception:  # noqa: BLE001 - fall back to the console command
        method = "console"
        unreal.SystemLibrary.execute_console_command(editor_world(), 'HighResShot %dx%d filename="%s"' % (w, h, path))
    return {"requested": path, "res": [w, h], "mode": mode, "framed": framed, "previous_camera": previous,
            "method": method}


def cmd_camera_restore(ctx, args):
    from .common import rot, vec
    editor_ss().set_level_viewport_camera_info(vec(args["loc"]), rot(args["rot"]))
    return {"restored": True}


def cmd_pie_start(ctx, args):
    if in_pie():
        return {"pie": True, "already_running": True}
    if args.get("simulate"):
        level_ss().editor_play_simulate()
    else:
        level_ss().editor_request_begin_play()
    ctx.evidence("pie_start", level=_lvl())
    return {"requested": True}


def cmd_pie_status(ctx, args):
    return {"pie": in_pie()}


def cmd_pie_stop(ctx, args):
    was = in_pie()
    if was:
        level_ss().editor_request_end_play()
    return {"was_running": was}


def _lvl():
    from .common import current_level_path
    return current_level_path()


__all__ = ["cmd_shot", "cmd_camera_restore", "cmd_pie_start", "cmd_pie_status", "cmd_pie_stop", "actor_ss"]
