"""A small, deliberately strict fake of the ``unreal`` Python module.

Purpose: run UE-Direct's editor-side code outside Unreal Engine in tests.
It models only the API surface UE-Direct uses (signatures follow the UE 5.8
Python API reference) plus the behaviours that matter for correctness:

* dirty-package tracking, map vs. content packages, One-File-Per-Actor
  external actor packages under /Game/__ExternalActors__/...;
* saving writes real files that start with the Unreal package file tag;
* load_level reloads from those files and DISCARDS unsaved in-memory state
  (exactly why UE-Direct refuses to switch levels while maps are dirty);
* asynchronous screenshots (the PNG appears shortly after the call);
* fault injection (save failures, PIE, registry still loading, no world).

Passing tests against this fake is NOT evidence that UE-Direct works in a
real editor. See tests/integration for real-engine tests.
"""

import json
import math
import os
import threading

PACKAGE_TAG = b"\xc1\x83\x2a\x9e"
FAKE_MARK = b"FAKEUE1\n"


# --------------------------------------------------------------- values

class Name(str):
    pass


class Text(str):
    pass


class Vector(object):
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __eq__(self, o):
        return isinstance(o, Vector) and (self.x, self.y, self.z) == (o.x, o.y, o.z)

    def __repr__(self):
        return "Vector(%g, %g, %g)" % (self.x, self.y, self.z)


class Rotator(object):
    def __init__(self, roll=0.0, pitch=0.0, yaw=0.0):  # UE Python order: roll, pitch, yaw
        self.roll, self.pitch, self.yaw = float(roll), float(pitch), float(yaw)

    def __eq__(self, o):
        return isinstance(o, Rotator) and (self.roll, self.pitch, self.yaw) == (o.roll, o.pitch, o.yaw)


class LinearColor(object):
    def __init__(self, r=0.0, g=0.0, b=0.0, a=1.0):
        self.r, self.g, self.b, self.a = r, g, b, a

    def __eq__(self, o):
        return isinstance(o, LinearColor) and (self.r, self.g, self.b, self.a) == (o.r, o.g, o.b, o.a)


class Color(object):
    def __init__(self, r=0, g=0, b=0, a=255):
        self.r, self.g, self.b, self.a = r, g, b, a


class EnumBase(object):
    pass


class ComponentMobility(EnumBase):
    def __init__(self, v):
        self.v = v

    def __eq__(self, o):
        return isinstance(o, ComponentMobility) and o.v == self.v


ComponentMobility.STATIC = ComponentMobility("STATIC")
ComponentMobility.MOVABLE = ComponentMobility("MOVABLE")


class AttachmentRule(object):
    KEEP_WORLD = "KEEP_WORLD"
    KEEP_RELATIVE = "KEEP_RELATIVE"


# --------------------------------------------------------------- engine state

class _Engine(object):
    def __init__(self):
        self.reset()

    def reset(self, project_dir=None):
        self.project_dir = project_dir
        self.world = None
        self.assets = {}          # package path -> asset object (loaded)
        self.dirty = set()        # dirty package names
        self.pending_delete = set()
        self.fail_save = set()
        self.pie = False
        self.ar_loading = False
        self.counter = 0
        self.transactions = 0
        self.screenshot_delay = 0.2
        self.screenshots = []
        self.engine_version = "5.8.1-0+++UE5+Release-5.8"
        self.subobject_support = True

    # paths
    def content_file(self, package, ext):
        root, _, rel = package.strip("/").partition("/")
        assert root == "Game", package
        return os.path.join(self.project_dir, "Content", *rel.split("/")) + ext

    def file_for(self, package):
        for ext in (".umap", ".uasset"):
            p = self.content_file(package, ext)
            if os.path.isfile(p):
                return p
        return None

    def write_pkg(self, package, ext, payload):
        if package in self.fail_save:
            return False
        p = self.content_file(package, ext)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(PACKAGE_TAG + FAKE_MARK + json.dumps(payload).encode("utf-8"))
        return True

    @staticmethod
    def read_pkg(path):
        with open(path, "rb") as fh:
            raw = fh.read()
        if not raw.startswith(PACKAGE_TAG + FAKE_MARK):
            raise ValueError("not a fake package: %s" % path)
        return json.loads(raw[len(PACKAGE_TAG + FAKE_MARK):].decode("utf-8"))

    def next_name(self, base):
        self.counter += 1
        return "%s_%d" % (base, self.counter)

    def mark_dirty(self, pkg):
        self.dirty.add(pkg)


ENGINE = _Engine()


def _reset(project_dir):
    ENGINE.reset(project_dir)


# --------------------------------------------------------------- objects

class _Class(object):
    def __init__(self, name):
        self._n = name

    def get_name(self):
        return self._n


class _ObjectBase(object):
    _props = ()

    def __init__(self, name=None, outer_path=""):
        self._name = name or ENGINE.next_name(type(self).__name__)
        self._outer_path = outer_path

    def get_name(self):
        return self._name

    def get_class(self):
        return _Class(getattr(self, "_ue_class_name", type(self).__name__))

    def get_path_name(self):
        return "%s.%s" % (self._outer_path, self._name) if self._outer_path else self._name

    def get_outermost(self):
        return Package(self._outer_path.split(".")[0] if self._outer_path else self._name)

    def get_package(self):
        return self.get_outermost()

    def get_editor_property(self, name):
        if name not in self._props:
            raise Exception("%s has no editor property %s" % (type(self).__name__, name))
        return getattr(self, "_p_" + name)

    def set_editor_property(self, name, value):
        if name not in self._props:
            raise Exception("%s has no editor property %s" % (type(self).__name__, name))
        setattr(self, "_p_" + name, value)
        self._on_change()

    def _on_change(self):
        pass

    def __eq__(self, o):
        return self is o

    def __hash__(self):
        return id(self)


Object = _ObjectBase


class Package(_ObjectBase):
    def __init__(self, name):
        self._name = name
        self._outer_path = ""

    def __eq__(self, o):
        return isinstance(o, Package) and o._name == self._name

    def __hash__(self):
        return hash(self._name)


class Factory(_ObjectBase):
    _props = ("parent_class",)
    _p_parent_class = None


class MaterialInstanceConstantFactoryNew(Factory):
    pass


class BlueprintFactory(Factory):
    pass


# ----- assets

class _Asset(_ObjectBase):
    def __init__(self, path):
        _ObjectBase.__init__(self, path.rsplit("/", 1)[-1], "")
        self._path = path

    def get_path_name(self):
        return "%s.%s" % (self._path, self._name)

    def get_outermost(self):
        return Package(self._path)

    def _on_change(self):
        ENGINE.mark_dirty(self._path)

    def _payload(self):
        return {"class": type(self).__name__}


class StaticMesh(_Asset):
    pass


class MaterialInterface(_Asset):
    pass


class Material(MaterialInterface):
    pass


class Texture(_Asset):
    pass


class Texture2D(Texture):
    pass


class MaterialInstanceConstant(MaterialInterface):
    _props = ("parent",)

    def __init__(self, path):
        MaterialInterface.__init__(self, path)
        self._p_parent = None
        self.params = {}

    def _payload(self):
        return {"class": "MaterialInstanceConstant",
                "parent": self._p_parent._path if self._p_parent else None, "params": self.params}


class Blueprint(_Asset):
    _props = ("parent_class",)

    def __init__(self, path, parent_class=None):
        _Asset.__init__(self, path)
        self._p_parent_class = parent_class
        self.components = []  # (name, class name, mesh path)

    def generated_class(self):
        parent = self._p_parent_class or Actor
        cls = type(self._name + "_C", (parent,), {"_ue_class_name": self._name + "_C", "_bp": self})
        return cls

    def _payload(self):
        return {"class": "Blueprint", "parent": (self._p_parent_class or Actor).__name__,
                "components": self.components}


class World(_Asset):
    pass


_ASSET_CLASSES = {c.__name__: c for c in (StaticMesh, Material, MaterialInstanceConstant, Blueprint, Texture2D,
                                           World)}


# ----- components and actors

class ActorComponent(_ObjectBase):
    _props = ("mobility",)

    def __init__(self, owner, name):
        _ObjectBase.__init__(self, name, owner.get_path_name())
        self.owner = owner
        self._p_mobility = ComponentMobility.STATIC

    def _on_change(self):
        self.owner._dirty()


class SceneComponent(ActorComponent):
    pass


class MeshComponent(SceneComponent):
    def __init__(self, owner, name):
        SceneComponent.__init__(self, owner, name)
        self.materials = [None]

    def get_num_materials(self):
        return len(self.materials)

    def get_material(self, i):
        return self.materials[i] if i < len(self.materials) else None

    def set_material(self, i, m):
        while len(self.materials) <= i:
            self.materials.append(None)
        self.materials[i] = m
        self.owner._dirty()


class StaticMeshComponent(MeshComponent):
    _props = ("mobility", "static_mesh", "cast_shadow")

    def __init__(self, owner, name="StaticMeshComponent0"):
        MeshComponent.__init__(self, owner, name)
        self._p_static_mesh = None
        self._p_cast_shadow = True

    def set_static_mesh(self, mesh):
        self._p_static_mesh = mesh
        self.owner._dirty()
        return True


class LightComponent(SceneComponent):
    _props = ("mobility", "intensity", "light_color")

    def __init__(self, owner, name="LightComponent0"):
        SceneComponent.__init__(self, owner, name)
        self._p_intensity = 5000.0
        self._p_light_color = Color(255, 255, 255, 255)


class Actor(_ObjectBase):
    _props = ("tags", "hidden")

    def __init__(self, world=None, name=None):
        world = world or ENGINE.world
        _ObjectBase.__init__(self, name or ENGINE.next_name(getattr(self, "_ue_class_name", type(self).__name__)),
                             "%s.%s:PersistentLevel" % (world.path, world.path.rsplit("/", 1)[-1]))
        self.world = world
        self.label = self._name
        self.folder = ""
        self._p_tags = []
        self._p_hidden = False
        self.loc, self.rot, self.scale = Vector(), Rotator(), Vector(1, 1, 1)
        self.components = []
        self.parent = None
        self.external_package = None
        if world.partitioned:
            self.external_package = "/Game/__ExternalActors__/%s/%s" % (world.path[len("/Game/"):], self._name)
        bp = getattr(type(self), "_bp", None)
        if bp is not None:
            for cname, ccls, mesh in bp.components:
                comp = globals()[ccls](self, cname)
                if mesh and isinstance(comp, StaticMeshComponent):
                    comp._p_static_mesh = EditorAssetLibrary.load_asset(mesh)
                self.components.append(comp)

    @property
    def tags(self):
        return list(self._p_tags)

    def get_package(self):
        return Package(self.external_package or self.world.path)

    def get_outermost(self):
        return Package(self.world.path)

    def _dirty(self):
        ENGINE.mark_dirty(self.external_package or self.world.path)

    def _on_change(self):
        self._dirty()

    def get_actor_label(self, create_if_none=True):
        return self.label

    def set_actor_label(self, label, mark_dirty=True):
        self.label = label
        self._dirty()

    def get_folder_path(self):
        return Name(self.folder)

    def set_folder_path(self, p):
        self.folder = str(p)
        self._dirty()

    def get_actor_location(self):
        return Vector(self.loc.x, self.loc.y, self.loc.z)

    def get_actor_rotation(self):
        return Rotator(self.rot.roll, self.rot.pitch, self.rot.yaw)

    def get_actor_scale3d(self):
        return Vector(self.scale.x, self.scale.y, self.scale.z)

    def set_actor_location(self, loc, sweep, teleport):
        self.loc = Vector(loc.x, loc.y, loc.z)
        self._dirty()
        return None

    def set_actor_location_and_rotation(self, loc, rot, sweep, teleport):
        self.loc = Vector(loc.x, loc.y, loc.z)
        self.rot = Rotator(rot.roll, rot.pitch, rot.yaw)
        self._dirty()
        return None

    def set_actor_scale3d(self, s):
        self.scale = Vector(s.x, s.y, s.z)
        self._dirty()

    def get_actor_bounds(self, only_colliding_components, include_from_child_actors=False):
        return Vector(self.loc.x, self.loc.y, self.loc.z + 50 * self.scale.z), \
            Vector(50 * self.scale.x, 50 * self.scale.y, 50 * self.scale.z)

    def get_components_by_class(self, component_class=None):
        cls = component_class or ActorComponent
        return [c for c in self.components if isinstance(c, cls)]

    def attach_to_actor(self, parent, socket_name, location_rule, rotation_rule, scale_rule, weld):
        self.parent = parent
        self._dirty()

    def _payload(self):
        d = {"name": self._name, "class": getattr(self, "_ue_class_name", type(self).__name__),
             "label": self.label, "folder": self.folder, "tags": [str(t) for t in self._p_tags],
             "loc": [self.loc.x, self.loc.y, self.loc.z], "rot": [self.rot.roll, self.rot.pitch, self.rot.yaw],
             "scale": [self.scale.x, self.scale.y, self.scale.z]}
        smc = [c for c in self.components if isinstance(c, StaticMeshComponent)]
        if smc:
            d["mesh"] = smc[0]._p_static_mesh._path if smc[0]._p_static_mesh else None
            d["materials"] = [m._path if m else None for m in smc[0].materials]
        bp = getattr(type(self), "_bp", None)
        if bp is not None:
            d["bp"] = bp._path
        return d


class StaticMeshActor(Actor):
    def __init__(self, world=None, name=None):
        Actor.__init__(self, world, name)
        self.static_mesh_component = StaticMeshComponent(self)
        self.components.append(self.static_mesh_component)


class Light(Actor):
    def __init__(self, world=None, name=None):
        Actor.__init__(self, world, name)
        self.components.append(LightComponent(self))


class PointLight(Light):
    pass


class DirectionalLight(Light):
    pass


class SkyLight(Light):
    pass


class PlayerStart(Actor):
    pass


class CameraActor(Actor):
    pass


class SkyAtmosphere(Actor):
    pass


_ACTOR_CLASSES = {c.__name__: c for c in (Actor, StaticMeshActor, PointLight, DirectionalLight, SkyLight,
                                           PlayerStart, CameraActor, SkyAtmosphere)}


class _FakeWorld(object):
    def __init__(self, path, partitioned=False):
        self.path = path
        self.partitioned = partitioned
        self.actors = []

    def get_outermost(self):
        return Package(self.path)

    def get_name(self):
        return self.path.rsplit("/", 1)[-1]


def _spawn_from_payload(world, d):
    if d.get("bp"):
        bp = EditorAssetLibrary.load_asset(d["bp"])
        cls = bp.generated_class()
    else:
        cls = _ACTOR_CLASSES[d["class"]]
    a = cls(world, d["name"])
    a.label, a.folder = d["label"], d["folder"]
    a._p_tags = [Name(t) for t in d["tags"]]
    a.loc, a.rot, a.scale = Vector(*d["loc"]), Rotator(*d["rot"]), Vector(*d["scale"])
    if "mesh" in d:
        smc = a.get_components_by_class(StaticMeshComponent)[0]
        smc._p_static_mesh = EditorAssetLibrary.load_asset(d["mesh"]) if d["mesh"] else None
        smc.materials = [EditorAssetLibrary.load_asset(m) if m else None for m in d.get("materials") or [None]]
    return a


# --------------------------------------------------------------- subsystems

class EditorActorSubsystem(object):
    def get_all_level_actors(self):
        return list(ENGINE.world.actors) if ENGINE.world else []

    def _place(self, actor, loc, rot):
        actor.loc, actor.rot = Vector(loc.x, loc.y, loc.z), Rotator(rot.roll, rot.pitch, rot.yaw)
        ENGINE.world.actors.append(actor)
        actor._dirty()
        return actor

    def spawn_actor_from_object(self, object_to_use, location, rotation=None, transient=False):
        rotation = rotation or Rotator()
        if isinstance(object_to_use, StaticMesh):
            a = StaticMeshActor(ENGINE.world)
            a.static_mesh_component._p_static_mesh = object_to_use
            return self._place(a, location, rotation)
        if isinstance(object_to_use, Blueprint):
            return self._place(object_to_use.generated_class()(ENGINE.world), location, rotation)
        return None

    def spawn_actor_from_class(self, actor_class, location, rotation=None, transient=False):
        return self._place(actor_class(ENGINE.world), location, rotation or Rotator())

    def destroy_actor(self, actor):
        return self.destroy_actors([actor])

    def destroy_actors(self, actors):
        for a in actors:
            if a in ENGINE.world.actors:
                ENGINE.world.actors.remove(a)
                if a.external_package:
                    ENGINE.pending_delete.add(a.external_package)
                a._dirty()
        return True


class LevelEditorSubsystem(object):
    def new_level(self, asset_path, is_partitioned_world=False):
        ENGINE.world = _FakeWorld(asset_path, is_partitioned_world)
        ENGINE.dirty = {p for p in ENGINE.dirty if not p.startswith("/Game/__ExternalActors__")
                        and p != asset_path}
        ENGINE.assets[asset_path] = World(asset_path)
        return _save_world()

    def new_level_from_template(self, asset_path, template_asset_path):
        return self.new_level(asset_path)

    def load_level(self, asset_path):
        f = ENGINE.file_for(asset_path)
        if f is None:
            return False
        data = ENGINE.read_pkg(f)
        w = _FakeWorld(asset_path, data.get("partitioned", False))
        ENGINE.world = w
        # unsaved changes of the previous level are DISCARDED (real-engine behaviour)
        ENGINE.dirty = {p for p in ENGINE.dirty if not (p.startswith("/Game/__ExternalActors__") or
                                                        isinstance(ENGINE.assets.get(p), World) or
                                                        p == asset_path)}
        ENGINE.pending_delete = set()
        if w.partitioned:
            root = ENGINE.content_file("/Game/__ExternalActors__/" + asset_path[len("/Game/"):], "")
            if os.path.isdir(root):
                for fn in sorted(os.listdir(root)):
                    w.actors.append(_spawn_from_payload(w, ENGINE.read_pkg(os.path.join(root, fn))))
        else:
            for d in data.get("actors", []):
                w.actors.append(_spawn_from_payload(w, d))
        return True

    def save_current_level(self):
        # Deliberately saves only the map package (not external actors), so UE-Direct must
        # save the dirty external actor packages itself.
        return _save_world()

    def save_all_dirty_levels(self):
        return _save_world()

    def is_in_play_in_editor(self):
        return ENGINE.pie

    def editor_request_begin_play(self):
        ENGINE.pie = True

    def editor_play_simulate(self):
        ENGINE.pie = True

    def editor_request_end_play(self):
        ENGINE.pie = False

    def get_current_level(self):
        return ENGINE.world


def _save_world():
    w = ENGINE.world
    payload = {"class": "World", "partitioned": w.partitioned}
    if not w.partitioned:
        payload["actors"] = [a._payload() for a in w.actors]
    if not ENGINE.write_pkg(w.path, ".umap", payload):
        return False
    ENGINE.dirty.discard(w.path)
    return True


def _save_package(name):
    if name in ENGINE.fail_save:
        return False
    w = ENGINE.world
    if w and name == w.path:
        return _save_world()
    if name.startswith("/Game/__ExternalActors__/"):
        if name in ENGINE.pending_delete:
            f = ENGINE.content_file(name, ".uasset")
            if os.path.isfile(f):
                os.remove(f)
            ENGINE.pending_delete.discard(name)
            ENGINE.dirty.discard(name)
            return True
        actor = next((a for a in (w.actors if w else []) if a.external_package == name), None)
        if actor is None:
            return False
        ok = ENGINE.write_pkg(name, ".uasset", actor._payload())
    else:
        asset = ENGINE.assets.get(name)
        if asset is None:
            return False
        ok = ENGINE.write_pkg(name, ".uasset", asset._payload())
    if ok:
        ENGINE.dirty.discard(name)
    return ok


class UnrealEditorSubsystem(object):
    camera = (Vector(0, 0, 500), Rotator(0, -30, 0))

    def get_editor_world(self):
        return ENGINE.world

    def get_level_viewport_camera_info(self):
        return UnrealEditorSubsystem.camera

    def set_level_viewport_camera_info(self, camera_location, camera_rotation):
        UnrealEditorSubsystem.camera = (camera_location, camera_rotation)


_SUBSYSTEMS = {}


def get_editor_subsystem(cls):
    if cls not in _SUBSYSTEMS:
        _SUBSYSTEMS[cls] = cls()
    return _SUBSYSTEMS[cls]


class SubobjectDataHandle(object):
    def __init__(self, obj):
        self.obj = obj


class AddNewSubobjectParams(object):
    def __init__(self, parent_handle=None, new_class=None, blueprint_context=None,
                 skip_mark_blueprint_modified=False, conform_transform_to_parent=False):
        self.parent_handle, self.new_class, self.blueprint_context = parent_handle, new_class, blueprint_context


class _Template(object):
    """Stand-in for a component template inside a Blueprint SCS."""

    def __init__(self, bp, name, cls):
        self.bp, self._name, self.cls = bp, name, cls
        self.mesh = None

    def get_name(self):
        return self._name + "_GEN_VARIABLE"

    def get_editor_property(self, k):
        return self.mesh if k == "static_mesh" else None

    def set_static_mesh(self, mesh):
        self.mesh = mesh
        self._sync()

    def set_editor_property(self, k, v):
        if k == "static_mesh":
            self.mesh = v
            self._sync()

    def _sync(self):
        self.bp.components = [(n, c, (m._path if m else None)) if n == self._name else (n, c, m)
                              for n, c, m in self.bp.components]


class SubobjectDataSubsystem(object):
    def k2_gather_subobject_data_for_blueprint(self, context):
        root = SubobjectDataHandle(None)
        return [root] + [SubobjectDataHandle(_Template(context, n, c)) for n, c, _ in context.components]

    def add_new_subobject(self, params):
        bp = params.blueprint_context
        name = ENGINE.next_name(params.new_class.__name__)
        bp.components.append((name, params.new_class.__name__, None))
        ENGINE.mark_dirty(bp._path)
        return SubobjectDataHandle(_Template(bp, name, params.new_class.__name__)), Text("")

    def rename_subobject(self, handle, new_name):
        t = handle.obj
        t.bp.components = [(str(new_name) if n == t._name else n, c, m) for n, c, m in t.bp.components]
        t._name = str(new_name)
        return True


class SubobjectDataBlueprintFunctionLibrary(object):
    @staticmethod
    def get_data(handle):
        return handle

    @staticmethod
    def get_associated_object(data):
        return data.obj

    @staticmethod
    def is_handle_valid(handle):
        return handle is not None and handle.obj is not None


def get_engine_subsystem(cls):
    if cls is SubobjectDataSubsystem and not ENGINE.subobject_support:
        return None
    return get_editor_subsystem(cls)


class BlueprintEditorLibrary(object):
    compiled = []

    @staticmethod
    def compile_blueprint(bp):
        BlueprintEditorLibrary.compiled.append(bp._path)


# --------------------------------------------------------------- libraries

class EditorLoadingAndSavingUtils(object):
    @staticmethod
    def get_dirty_map_packages():
        return [Package(p) for p in sorted(ENGINE.dirty) if ENGINE.world and p == ENGINE.world.path
                or isinstance(ENGINE.assets.get(p), World)]

    @staticmethod
    def get_dirty_content_packages():
        maps = {p.get_name() for p in EditorLoadingAndSavingUtils.get_dirty_map_packages()}
        return [Package(p) for p in sorted(ENGINE.dirty) if p not in maps]

    @staticmethod
    def save_dirty_packages(save_map_packages, save_content_packages):
        ok = True
        targets = []
        if save_map_packages:
            targets += EditorLoadingAndSavingUtils.get_dirty_map_packages()
        if save_content_packages:
            targets += EditorLoadingAndSavingUtils.get_dirty_content_packages()
        for p in targets:
            ok = _save_package(p.get_name()) and ok
        return ok

    @staticmethod
    def save_packages(packages_to_save, only_dirty):
        ok = True
        for p in packages_to_save:
            if only_dirty and p.get_name() not in ENGINE.dirty:
                continue
            ok = _save_package(p.get_name()) and ok
        return ok

    @staticmethod
    def save_map(world, asset_path):
        world.path = asset_path
        ENGINE.assets[asset_path] = World(asset_path)
        return _save_world()


class EditorAssetLibrary(object):
    @staticmethod
    def does_asset_exist(asset_path):
        p = asset_path.split(".")[0]
        return p in ENGINE.assets or ENGINE.file_for(p) is not None

    @staticmethod
    def load_asset(asset_path):
        p = asset_path.split(".")[0]
        if p in ENGINE.assets:
            return ENGINE.assets[p]
        f = ENGINE.file_for(p)
        if f is None:
            return None
        data = ENGINE.read_pkg(f)
        cls = _ASSET_CLASSES[data["class"]]
        obj = cls(p)
        if isinstance(obj, MaterialInstanceConstant):
            obj._p_parent = EditorAssetLibrary.load_asset(data["parent"]) if data.get("parent") else None
            obj.params = data.get("params", {})
        if isinstance(obj, Blueprint):
            obj._p_parent_class = _ACTOR_CLASSES.get(data.get("parent"), Actor)
            obj.components = [tuple(c) for c in data.get("components", [])]
        ENGINE.assets[p] = obj
        return obj

    @staticmethod
    def delete_asset(asset_path_to_delete):
        p = asset_path_to_delete.split(".")[0]
        ENGINE.assets.pop(p, None)
        ENGINE.dirty.discard(p)
        f = ENGINE.file_for(p)
        if f:
            os.remove(f)
        return True

    @staticmethod
    def duplicate_asset(source_asset_path, destination_asset_path):
        src = EditorAssetLibrary.load_asset(source_asset_path)
        if src is None:
            return None
        obj = type(src)(destination_asset_path)
        ENGINE.assets[destination_asset_path] = obj
        ENGINE.mark_dirty(destination_asset_path)
        return obj


class _TopLevelAssetPath(object):
    def __init__(self, n):
        self.asset_name = Name(n)


class AssetData(object):
    def __init__(self, package, cls):
        self.package_name = Name(package)
        self.asset_name = Name(package.rsplit("/", 1)[-1])
        self.asset_class_path = _TopLevelAssetPath(cls)


class _AssetRegistry(object):
    def get_assets_by_path(self, package_path, recursive=False, include_only_on_disk_assets=False):
        out = {}
        root = str(package_path).rstrip("/")
        if root.startswith("/Engine"):
            if root == "/Engine/BasicShapes":
                return [AssetData("/Engine/BasicShapes/Cube", "StaticMesh"),
                        AssetData("/Engine/BasicShapes/Sphere", "StaticMesh")]
            return []
        base = ENGINE.content_file(root, "") if root != "/Game" else os.path.join(ENGINE.project_dir, "Content")
        for dirpath, _, files in os.walk(base):
            if "__ExternalActors__" in dirpath:
                continue
            for f in files:
                if f.endswith((".uasset", ".umap")):
                    rel = os.path.relpath(os.path.join(dirpath, f), os.path.join(ENGINE.project_dir, "Content"))
                    pkg = "/Game/" + os.path.splitext(rel)[0].replace(os.sep, "/")
                    try:
                        cls = ENGINE.read_pkg(os.path.join(dirpath, f))["class"]
                    except ValueError:
                        continue
                    out[pkg] = cls
        for p, obj in ENGINE.assets.items():
            if p.startswith(root + "/") and not include_only_on_disk_assets:
                out[p] = type(obj).__name__
        return [AssetData(p, c) for p, c in sorted(out.items())]

    def is_loading_assets(self):
        return ENGINE.ar_loading


class AssetRegistryHelpers(object):
    @staticmethod
    def get_asset_registry():
        return _AssetRegistry()


class AssetImportTask(_ObjectBase):
    _props = ("filename", "destination_path", "destination_name", "automated", "replace_existing", "save",
              "imported_object_paths")

    def __init__(self):
        _ObjectBase.__init__(self)
        for p in self._props:
            setattr(self, "_p_" + p, None)
        self._p_imported_object_paths = []


class _AssetTools(object):
    def create_asset(self, asset_name, package_path, asset_class, factory, calling_context="None",
                     overwrite_existing=False):
        path = package_path.rstrip("/") + "/" + asset_name
        if asset_class is Blueprint:
            obj = Blueprint(path, factory.get_editor_property("parent_class"))
        else:
            obj = asset_class(path)
        ENGINE.assets[path] = obj
        ENGINE.mark_dirty(path)
        return obj

    def import_asset_tasks(self, tasks):
        for t in tasks:
            name = t._p_destination_name or os.path.splitext(os.path.basename(t._p_filename))[0]
            path = t._p_destination_path.rstrip("/") + "/" + name
            ext = os.path.splitext(t._p_filename)[1].lower()
            cls = StaticMesh if ext in (".fbx", ".obj", ".gltf", ".glb") else Texture2D
            ENGINE.assets[path] = cls(path)
            ENGINE.mark_dirty(path)
            t._p_imported_object_paths = [path + "." + name]


class AssetToolsHelpers(object):
    @staticmethod
    def get_asset_tools():
        return _AssetTools()


class MaterialEditingLibrary(object):
    @staticmethod
    def set_material_instance_parent(instance, new_parent):
        instance._p_parent = new_parent
        ENGINE.mark_dirty(instance._path)

    @staticmethod
    def set_material_instance_scalar_parameter_value(instance, parameter_name, value, association=None):
        instance.params[str(parameter_name)] = value
        ENGINE.mark_dirty(instance._path)
        return True

    @staticmethod
    def set_material_instance_vector_parameter_value(instance, parameter_name, value, association=None):
        instance.params[str(parameter_name)] = [value.r, value.g, value.b, value.a]
        ENGINE.mark_dirty(instance._path)
        return True

    @staticmethod
    def set_material_instance_texture_parameter_value(instance, parameter_name, value, association=None):
        instance.params[str(parameter_name)] = value._path if value else None
        ENGINE.mark_dirty(instance._path)
        return True

    @staticmethod
    def get_material_instance_scalar_parameter_value(instance, parameter_name, association=None):
        return instance.params.get(str(parameter_name), 0.0)

    @staticmethod
    def get_material_instance_vector_parameter_value(instance, parameter_name, association=None):
        v = instance.params.get(str(parameter_name), [0, 0, 0, 1])
        return LinearColor(*v)

    @staticmethod
    def get_material_instance_texture_parameter_value(instance, parameter_name, association=None):
        v = instance.params.get(str(parameter_name))
        return EditorAssetLibrary.load_asset(v) if v else None

    @staticmethod
    def update_material_instance(instance):
        return None


class ScopedEditorTransaction(object):
    def __init__(self, desc):
        self.desc = desc

    def __enter__(self):
        ENGINE.transactions += 1
        return self

    def __exit__(self, *exc):
        return False

    def cancel(self):
        pass


_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"IEND\xaeB`\x82")


class AutomationLibrary(object):
    @staticmethod
    def take_high_res_screenshot(res_x, res_y, filename, camera=None, mask_enabled=False, capture_hdr=False,
                                 comparison_tolerance=None, comparison_notes="", delay=0.0, force_game_view=True):
        def write():
            with open(filename, "wb") as fh:
                fh.write(_PNG)
        ENGINE.screenshots.append(filename)
        threading.Timer(ENGINE.screenshot_delay, write).start()  # written on a later "frame"
        return object()


class SystemLibrary(object):
    @staticmethod
    def get_engine_version():
        return ENGINE.engine_version

    @staticmethod
    def execute_console_command(world_context_object, command, specific_player=None):
        return None

    @staticmethod
    def get_system_path(obj):
        return ENGINE.file_for(obj.get_name())


class MathLibrary(object):
    @staticmethod
    def find_look_at_rotation(start, target):
        dx, dy, dz = target.x - start.x, target.y - start.y, target.z - start.z
        yaw = math.degrees(math.atan2(dy, dx))
        pitch = math.degrees(math.atan2(dz, math.hypot(dx, dy)))
        return Rotator(0.0, pitch, yaw)


class Paths(object):
    @staticmethod
    def get_project_file_path():
        import glob
        found = glob.glob(os.path.join(ENGINE.project_dir, "*.uproject"))
        return found[0] if found else ""

    @staticmethod
    def convert_relative_path_to_full(p, base_path=""):
        return os.path.abspath(p)


class WorldPartitionBlueprintLibrary(object):
    @staticmethod
    def get_actor_descs():
        w = ENGINE.world
        if not w or not w.partitioned:
            return []
        root = ENGINE.content_file("/Game/__ExternalActors__/" + w.path[len("/Game/"):], "")
        return sorted(os.listdir(root)) if os.path.isdir(root) else []


def load_class(outer, path):
    name = path.rsplit(".", 1)[-1]
    if name in _ACTOR_CLASSES:
        return _ACTOR_CLASSES[name]
    if name.endswith("_C"):
        bp = EditorAssetLibrary.load_asset(path.split(".")[0])
        if bp is not None:
            return bp.generated_class()
    return None


def find_package(name):
    return Package(name) if EditorAssetLibrary.does_asset_exist(name) or name.startswith("/Game/__External") \
        else None
