"""Batch Execution Engine, editor half (runs inside Unreal Editor).

One request = one chunk = one editor transaction (Ctrl+Z undoes the chunk).
Every op is applied independently; failures are collected, not raised.

Idempotency: every spawned actor carries a ``ued:<id>`` tag. Re-sending an op
with the same id updates that actor instead of creating a duplicate, so a
chunk can be safely retried after a dropped connection or partial failure.
"""

import os

import unreal

from .common import (TAG_PREFIX, UedError, actor_label, convert_value, find_component, mesh_component,
                     resolve_class, rot, vec)

_EPS = 0.01


class _Chunk(object):
    def __init__(self, ctx, args):
        self.ctx = ctx
        self.on_error = args.get("on_error", "continue")
        self.allow_delete = bool(args.get("allow_delete", False))
        self.max_delete = int(args.get("max_delete", 50))
        self.deleted_total = 0
        self.undo = []  # callables restoring prior state (used by on_error=rollback)
        self.counts = {"created": 0, "modified": 0, "unchanged": 0, "deleted": 0, "failed": 0, "skipped": 0}
        self.ok_ids, self.failed, self.created_ids, self.assets_created = [], [], [], []
        self.deleted_ids = []
        self.actor_changes = 0
        self.spawn_ids = []

    # ------------------------------------------------------------ helpers
    def _remember_transform(self, actor):
        loc, r, s = actor.get_actor_location(), actor.get_actor_rotation(), actor.get_actor_scale3d()

        def restore():
            actor.set_actor_location_and_rotation(loc, r, False, True)
            actor.set_actor_scale3d(s)
        self.undo.append(restore)

    def _remember_prop(self, obj, name):
        old = obj.get_editor_property(name)
        self.undo.append(lambda: obj.set_editor_property(name, old))

    def _apply_transform(self, actor, op, absolute=True):
        changed = False
        cur_loc, cur_rot, cur_scale = actor.get_actor_location(), actor.get_actor_rotation(), actor.get_actor_scale3d()
        new_loc, new_rot, new_scale = cur_loc, cur_rot, cur_scale
        if "loc" in op:
            new_loc = vec(op["loc"])
        if "offset" in op:
            o = vec(op["offset"])
            new_loc = unreal.Vector(new_loc.x + o.x, new_loc.y + o.y, new_loc.z + o.z)
        if "rot" in op:
            new_rot = rot(op["rot"])
        if "rotate" in op:
            d = rot(op["rotate"])
            new_rot = unreal.Rotator(roll=new_rot.roll + d.roll, pitch=new_rot.pitch + d.pitch, yaw=new_rot.yaw + d.yaw)
        if "scale" in op:
            new_scale = vec(op["scale"])
        if "scale_by" in op:
            f = vec(op["scale_by"])
            new_scale = unreal.Vector(new_scale.x * f.x, new_scale.y * f.y, new_scale.z * f.z)
        if not (_close(new_loc, cur_loc) and _close_rot(new_rot, cur_rot)):
            actor.set_actor_location_and_rotation(new_loc, new_rot, False, True)
            changed = True
        if not _close(new_scale, cur_scale):
            actor.set_actor_scale3d(new_scale)
            changed = True
        return changed

    def _apply_common(self, actor, op):
        """Label, folder, tags, mesh, materials, props. Returns True if anything changed."""
        changed = False
        if op.get("label") and actor_label(actor) != op["label"]:
            old_label = actor_label(actor)
            self.undo.append(lambda: actor.set_actor_label(old_label))
            actor.set_actor_label(op["label"])
            changed = True
        if "folder" in op and str(actor.get_folder_path()) != op["folder"]:
            old_folder = str(actor.get_folder_path())
            self.undo.append(lambda: actor.set_folder_path(old_folder))
            actor.set_folder_path(op["folder"])
            changed = True
        if op.get("tags"):
            cur = [str(t) for t in actor.tags]
            want = cur + [t for t in op["tags"] if t not in cur]
            if want != cur:
                self._remember_prop(actor, "tags")
                actor.set_editor_property("tags", [unreal.Name(t) for t in want])
                changed = True
        if op.get("mesh"):
            comp = mesh_component(actor)
            if not isinstance(comp, unreal.StaticMeshComponent):
                raise UedError("NO_MESH_COMPONENT", "%s has no StaticMeshComponent" % actor_label(actor))
            mesh = self.ctx.load_asset(op["mesh"])
            if mesh is None:
                raise UedError("MISSING_ASSET", "mesh not found: %s" % op["mesh"])
            if comp.get_editor_property("static_mesh") != mesh:
                self._remember_prop(comp, "static_mesh")
                comp.set_static_mesh(mesh)
                changed = True
        mats = op.get("materials")
        if op.get("material"):
            mats = [op["material"]]
        if mats:
            comp = mesh_component(actor)
            if comp is None:
                raise UedError("NO_MESH_COMPONENT", "%s has no mesh component" % actor_label(actor))
            for slot, path in enumerate(mats):
                if path is None:
                    continue
                mat = self.ctx.load_asset(path)
                if mat is None:
                    raise UedError("MISSING_ASSET", "material not found: %s" % path)
                old = comp.get_material(slot)
                if old != mat:
                    self.undo.append(lambda c=comp, s=slot, m=old: c.set_material(s, m))
                    comp.set_material(slot, mat)
                    changed = True
        for name, value in (op.get("props") or {}).items():
            changed |= self._set_prop(actor, name, value)
        for comp_name, props in (op.get("comp_props") or {}).items():
            comp = find_component(actor, comp_name)
            for name, value in props.items():
                changed |= self._set_prop(comp, name, value)
        return changed

    def _set_prop(self, obj, name, value):
        cur = obj.get_editor_property(name)
        new = convert_value(cur, value, self.ctx)
        if _same(cur, new):
            return False
        self._remember_prop(obj, name)
        obj.set_editor_property(name, new)
        return True

    def _targets(self, op):
        sel = op.get("target")
        if not sel:
            raise UedError("BAD_OP", "op %s needs 'target'" % op.get("op"))
        found = self.ctx.index().select(sel)
        if not found and not op.get("optional"):
            raise UedError("NO_MATCH", "selector matched nothing: %s" % sel)
        return found

    # ---------------------------------------------------------------- ops
    def op_spawn(self, op):
        uid = op.get("id")
        if not uid:
            raise UedError("BAD_OP", "spawn needs an 'id' (the host CLI assigns one automatically)")
        existing = self.ctx.index().by_id.get(uid)
        if existing is not None:
            self._remember_transform(existing)
            changed = self._apply_transform(existing, op)
            changed |= self._apply_common(existing, _without(op, ("tags",)))
            self.ctx.touch_actor(existing)
            return "modified" if changed else "unchanged"
        loc, r = vec(op.get("loc")), rot(op.get("rot"))
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        if op.get("asset"):
            obj = self.ctx.load_asset(op["asset"])
            if obj is None:
                raise UedError("MISSING_ASSET", "asset not found: %s" % op["asset"])
            actor = ss.spawn_actor_from_object(obj, loc, r)
        elif op.get("class"):
            actor = ss.spawn_actor_from_class(resolve_class(op["class"]), loc, r)
        else:
            raise UedError("BAD_OP", "spawn needs 'asset' or 'class'")
        if actor is None:
            raise UedError("SPAWN_FAILED", "spawn returned None for %s" % (op.get("asset") or op.get("class")))
        self.undo.append(lambda a=actor: unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actor(a))
        tags = [str(t) for t in actor.tags] + [TAG_PREFIX + uid] + list(op.get("tags") or [])
        actor.set_editor_property("tags", [unreal.Name(t) for t in tags])
        if "scale" in op:
            actor.set_actor_scale3d(vec(op["scale"]))
        self._apply_common(actor, _without(op, ("tags",)))
        self.ctx.index().add(uid, actor)
        self.ctx.touch_actor(actor)
        self.created_ids.append(uid)
        return "created"

    def op_transform(self, op):
        changed = False
        for a in self._targets(op):
            self._remember_transform(a)
            if self._apply_transform(a, op):
                self.ctx.touch_actor(a)
                changed = True
        return "modified" if changed else "unchanged"

    def op_set(self, op):
        changed = False
        for a in self._targets(op):
            if self._apply_common(a, op):
                self.ctx.touch_actor(a)
                changed = True
        return "modified" if changed else "unchanged"

    op_material = op_set
    op_mesh = op_set

    def op_delete(self, op):
        if not self.allow_delete:
            raise UedError("DELETE_NOT_ALLOWED", "delete ops require allow_delete=true in the batch (user permission)")
        targets = self._targets(op)
        if self.deleted_total + len(targets) > self.max_delete:
            raise UedError("DELETE_LIMIT", "would delete %d actors (limit %d); raise max_delete only with user approval"
                           % (self.deleted_total + len(targets), self.max_delete))
        ss = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        from .common import actor_ued_id
        for a in targets:
            self.ctx.touch_actor(a)
            uid = actor_ued_id(a)
            if uid is not None:
                self.deleted_ids.append(uid)
            self.ctx.index().remove(a)
        if targets and not ss.destroy_actors(targets):
            raise UedError("DELETE_FAILED", "destroy_actors returned False")
        self.deleted_total += len(targets)
        self.counts["deleted"] += len(targets) - 1 if targets else 0
        return "deleted" if targets else "unchanged"

    def op_attach(self, op):
        parents = self.ctx.index().select(op["parent"])
        if len(parents) != 1:
            raise UedError("NO_MATCH", "parent selector must match exactly one actor: %s" % op["parent"])
        rule = unreal.AttachmentRule.KEEP_WORLD
        for a in self._targets(op):
            a.attach_to_actor(parents[0], unreal.Name(op.get("socket", "None")), rule, rule, rule, False)
            self.ctx.touch_actor(a)
        return "modified"

    # ---------------------------------------------------------- asset ops
    def _new_asset(self, path, asset_class, factory):
        pkg, name = path.rsplit("/", 1)
        tools = unreal.AssetToolsHelpers.get_asset_tools()
        obj = tools.create_asset(name, pkg, asset_class, factory)
        if obj is None:
            raise UedError("CREATE_FAILED", "create_asset failed for %s" % path)
        self.assets_created.append([path, asset_class.__name__])
        self.ctx.touch_package(path)
        self._undo_create(path)
        return obj

    def _undo_create(self, path):
        def undo():
            unreal.EditorAssetLibrary.delete_asset(path)
            self.assets_created[:] = [a for a in self.assets_created if a[0] != path]
        self.undo.append(undo)

    def op_mi(self, op):
        """Create or update a Material Instance Constant (reuses an existing asset)."""
        path = op["path"]
        mel = unreal.MaterialEditingLibrary
        status = "unchanged"
        if unreal.EditorAssetLibrary.does_asset_exist(path):
            mi = unreal.EditorAssetLibrary.load_asset(path)
            if not isinstance(mi, unreal.MaterialInstanceConstant):
                raise UedError("TYPE_MISMATCH", "%s exists and is not a MaterialInstanceConstant" % path)
        else:
            mi = self._new_asset(path, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
            status = "created"
        if status != "created":
            self._remember_mi(mi, op)
        if op.get("parent"):
            parent = self.ctx.load_asset(op["parent"])
            if parent is None:
                raise UedError("MISSING_ASSET", "parent material not found: %s" % op["parent"])
            if mi.get_editor_property("parent") != parent:
                mel.set_material_instance_parent(mi, parent)
                status = status if status == "created" else "modified"
        for k, v in (op.get("scalar") or {}).items():
            mel.set_material_instance_scalar_parameter_value(mi, k, float(v))
        for k, v in (op.get("vector") or {}).items():
            c = list(v) + [1.0] * (4 - len(v))
            mel.set_material_instance_vector_parameter_value(mi, k, unreal.LinearColor(*[float(x) for x in c[:4]]))
        for k, v in (op.get("texture") or {}).items():
            tex = self.ctx.load_asset(v)
            if tex is None:
                raise UedError("MISSING_ASSET", "texture not found: %s" % v)
            mel.set_material_instance_texture_parameter_value(mi, k, tex)
        if op.get("scalar") or op.get("vector") or op.get("texture"):
            status = status if status == "created" else "modified"
        mel.update_material_instance(mi)
        self.ctx.touch_package(path)
        return status

    def _remember_mi(self, mi, op):
        mel = unreal.MaterialEditingLibrary
        old_parent = mi.get_editor_property("parent")
        restores = [lambda: mel.set_material_instance_parent(mi, old_parent)]
        for kind in ("scalar", "vector", "texture"):
            getter = getattr(mel, "get_material_instance_%s_parameter_value" % kind, None)
            setter = getattr(mel, "set_material_instance_%s_parameter_value" % kind)
            for name in (op.get(kind) or {}):
                if getter is None:
                    raise UedError("NO_UNDO", "cannot snapshot %s parameter %s for rollback" % (kind, name))
                old = getter(mi, name)
                restores.append(lambda s=setter, n=name, v=old: s(mi, n, v))

        def undo():
            for r in restores:
                r()
            mel.update_material_instance(mi)
        self.undo.append(undo)

    def op_bp(self, op):
        """Create (or reuse) an Actor Blueprint and add missing components."""
        path = op["path"]
        status = "unchanged"
        if unreal.EditorAssetLibrary.does_asset_exist(path):
            bp = unreal.EditorAssetLibrary.load_asset(path)
            if not isinstance(bp, unreal.Blueprint):
                raise UedError("TYPE_MISMATCH", "%s exists and is not a Blueprint" % path)
        else:
            factory = unreal.BlueprintFactory()
            factory.set_editor_property("parent_class", resolve_class(op.get("parent", "Actor")))
            bp = self._new_asset(path, unreal.Blueprint, factory)
            status = "created"
        comps = op.get("components") or []
        if comps and status != "created" and self.on_error == "rollback":
            raise UedError("NO_UNDO", "adding components to an existing Blueprint cannot be rolled back; "
                           "use on_error=stop or continue")
        if comps:
            sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
            lib = unreal.SubobjectDataBlueprintFunctionLibrary
            get_obj = getattr(lib, "get_associated_object", None) or getattr(lib, "get_object")
            handles = sds.k2_gather_subobject_data_for_blueprint(bp)
            existing = set()
            for h in handles:
                o = get_obj(lib.get_data(h))
                if o is not None:
                    existing.add(o.get_name().replace("_GEN_VARIABLE", ""))
            for spec in comps:
                name = spec.get("name") or spec["class"]
                if name in existing:
                    continue
                params = unreal.AddNewSubobjectParams(parent_handle=handles[0], new_class=resolve_class(spec["class"]),
                                                      blueprint_context=bp)
                handle, fail = sds.add_new_subobject(params)
                valid = getattr(lib, "is_handle_valid", None)
                if handle is None or (valid is not None and not valid(handle)):
                    raise UedError("COMPONENT_FAILED", "add_new_subobject failed: %s" % fail)
                if hasattr(sds, "rename_subobject"):
                    sds.rename_subobject(handle, unreal.Text(name))
                comp = get_obj(lib.get_data(handle))
                for k, v in (spec.get("props") or {}).items():
                    comp.set_editor_property(k, convert_value(comp.get_editor_property(k), v, self.ctx))
                if spec.get("mesh") and isinstance(comp, unreal.StaticMeshComponent):
                    mesh = self.ctx.load_asset(spec["mesh"])
                    if mesh is None:
                        raise UedError("MISSING_ASSET", "mesh not found: %s" % spec["mesh"])
                    comp.set_static_mesh(mesh)
                status = status if status == "created" else "modified"
            if status != "unchanged" and hasattr(unreal, "BlueprintEditorLibrary"):
                unreal.BlueprintEditorLibrary.compile_blueprint(bp)
        self.ctx.touch_package(path)
        return status

    def op_import(self, op):
        src = op["file"]
        if not os.path.isfile(src):
            raise UedError("MISSING_FILE", "source file not found: %s" % src)
        dest = op["dest"]
        name = op.get("name") or os.path.splitext(os.path.basename(src))[0]
        target = dest.rstrip("/") + "/" + name
        if unreal.EditorAssetLibrary.does_asset_exist(target) and not op.get("replace"):
            return "unchanged"  # reuse first: never re-import an existing asset silently
        task = unreal.AssetImportTask()
        task.set_editor_property("filename", src)
        task.set_editor_property("destination_path", dest)
        task.set_editor_property("destination_name", name)
        task.set_editor_property("automated", True)
        task.set_editor_property("replace_existing", bool(op.get("replace")))
        task.set_editor_property("save", False)
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        paths = list(task.get_editor_property("imported_object_paths") or [])
        if not paths:
            raise UedError("IMPORT_FAILED", "nothing imported from %s" % src)
        for p in paths:
            pkg = p.split(".")[0]
            self.ctx.touch_package(pkg)
            if not op.get("replace"):
                self._undo_create(pkg)
            obj = unreal.EditorAssetLibrary.load_asset(pkg)
            self.assets_created.append([pkg, obj.get_class().get_name() if obj else "Unknown"])
        return "created"

    def op_duplicate_asset(self, op):
        if unreal.EditorAssetLibrary.does_asset_exist(op["dest"]):
            return "unchanged"
        obj = unreal.EditorAssetLibrary.duplicate_asset(op["source"], op["dest"])
        if obj is None:
            raise UedError("DUPLICATE_FAILED", "duplicate_asset failed: %s -> %s" % (op["source"], op["dest"]))
        self.assets_created.append([op["dest"], obj.get_class().get_name()])
        self.ctx.touch_package(op["dest"])
        self._undo_create(op["dest"])
        return "created"


def _without(op, keys):
    return {k: v for k, v in op.items() if k not in keys}


def _close(a, b):
    return abs(a.x - b.x) < _EPS and abs(a.y - b.y) < _EPS and abs(a.z - b.z) < _EPS


def _close_rot(a, b):
    def d(x, y):
        v = (x - y) % 360.0
        return min(v, 360.0 - v)
    return d(a.roll, b.roll) < _EPS and d(a.pitch, b.pitch) < _EPS and d(a.yaw, b.yaw) < _EPS


def _same(a, b):
    try:
        if isinstance(a, unreal.Vector) and isinstance(b, unreal.Vector):
            return _close(a, b)
        return a == b
    except Exception:  # noqa: BLE001
        return False


def cmd_batch(ctx, args):
    ops = args.get("ops") or []
    chunk = _Chunk(ctx, args)
    deferred_deletes = []
    desc = "UE-Direct: %s" % (args.get("label") or "batch %d ops" % len(ops))
    with unreal.ScopedEditorTransaction(desc):
        for i, op in enumerate(ops):
            kind = op.get("op", "spawn")
            if kind == "delete" and chunk.on_error == "rollback":
                deferred_deletes.append((i, op))
                continue
            _run_op(chunk, i, op, kind)
            if chunk.failed and chunk.on_error in ("stop", "rollback"):
                break
        if chunk.failed and chunk.on_error == "rollback":
            for fn in reversed(chunk.undo):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
            ctx._index = None
            rolled = len(chunk.ok_ids)
            chunk.counts = dict.fromkeys(chunk.counts, 0)
            chunk.counts["failed"] = len(chunk.failed)
            chunk.counts["rolled_back"] = rolled
            chunk.ok_ids, chunk.created_ids, chunk.spawn_ids = [], [], []
        else:
            for i, op in deferred_deletes:
                _run_op(chunk, i, op, "delete")
    result = {
        "counts": chunk.counts,
        "created_ids": chunk.created_ids[:5] + (["...+%d" % (len(chunk.created_ids) - 5)] if len(chunk.created_ids) > 5 else []),
        "failed": chunk.failed[:20],
        "assets_created": chunk.assets_created,
        "stopped_early": bool(chunk.failed) and chunk.on_error in ("stop", "rollback"),
    }
    mutated = sum(chunk.counts.get(k, 0) for k in ("created", "modified", "deleted")) > 0
    ctx.evidence("batch", counts=chunk.counts, ok_ids=chunk.ok_ids, failed_ids=[f.get("id") for f in chunk.failed],
                 deleted_ids=chunk.deleted_ids, spawn_ids=chunk.spawn_ids, assets_created=[a[0] for a in chunk.assets_created],
                 mutated=mutated, actor_mutated=chunk.actor_changes > 0 and not result["counts"].get("rolled_back"),
                 level=_level(), on_error=chunk.on_error)
    return result


def _level():
    from .common import current_level_path
    return current_level_path()


def _run_op(chunk, i, op, kind):
    """Run one op atomically: if it raises, undo whatever it changed before failing."""
    fn = getattr(chunk, "op_" + kind, None)
    mark = len(chunk.undo)
    try:
        if fn is None:
            raise UedError("BAD_OP", "unknown op %r" % kind)
        status = fn(op)
        chunk.counts[status] = chunk.counts.get(status, 0) + 1
        if kind not in ("mi", "bp", "import", "duplicate_asset") and status in ("created", "modified", "deleted"):
            chunk.actor_changes += 1
        if op.get("id"):
            chunk.ok_ids.append(op["id"])
            if kind == "spawn":
                chunk.spawn_ids.append(op["id"])
    except Exception as exc:  # noqa: BLE001
        for undo in reversed(chunk.undo[mark:]):
            try:
                undo()
            except Exception:  # noqa: BLE001
                pass
        del chunk.undo[mark:]
        chunk.ctx._index = None
        chunk.counts["failed"] += 1
        chunk.failed.append({"i": i, "id": op.get("id"), "op": kind, "code": getattr(exc, "code", "EXCEPTION"),
                             "msg": str(exc)[:300]})
