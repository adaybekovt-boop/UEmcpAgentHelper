"""Shared test scaffolding: temp Unreal project + in-process fake editor channel."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "ue-direct", "scripts")
FAKES = os.path.join(ROOT, "tests", "fakes")
for p in (FAKES, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import unreal  # noqa: E402  (the fake)
from uedirect import cli, gate, procs  # noqa: E402
from uedirect import session as session_mod  # noqa: E402
from uedirect.transports.base import Transport, TransportError  # noqa: E402


class InProcessTransport(Transport):
    """Executes the bootstrap snippet in this interpreter against the fake engine."""

    name = "inproc"

    def __init__(self):
        self.calls = 0
        self.fail_next = 0          # simulate N dropped connections BEFORE execution
        self.drop_after = 0         # simulate N dropped replies AFTER execution
        self.down = False

    def probe(self, timeout=2.0):
        return None if self.down else {"transport": "inproc"}

    def execute(self, code, timeout=110.0):
        if self.down:
            raise TransportError("editor channel down")
        if self.fail_next:
            self.fail_next -= 1
            raise TransportError("connection reset (simulated)")
        self.calls += 1
        exec(compile(code, "<ued-snippet>", "exec"), {"__name__": "__ued__"})
        if self.drop_after:
            self.drop_after -= 1
            raise TransportError("reply lost (simulated)")
        return ""


def write_fake_asset(project_dir, package, cls, ext=".uasset", extra=None):
    root, _, rel = package.strip("/").partition("/")
    path = os.path.join(project_dir, "Content", *rel.split("/")) + ext
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"class": cls}
    payload.update(extra or {})
    with open(path, "wb") as fh:
        fh.write(unreal.PACKAGE_TAG + unreal.FAKE_MARK + json.dumps(payload).encode("utf-8"))
    return path


class ProjectTestCase(unittest.TestCase):
    """Creates <tmp>/Demo/Demo.uproject, a fake engine root, and wires the fake editor."""

    engine_version = (5, 8, 1)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ued-test-")
        self.project_dir = os.path.join(self.tmp, "Demo")
        os.makedirs(os.path.join(self.project_dir, "Content"))
        with open(os.path.join(self.project_dir, "Demo.uproject"), "w") as fh:
            json.dump({"FileVersion": 3, "EngineAssociation": "%d.%d" % self.engine_version[:2],
                       "Plugins": [{"Name": "PythonScriptPlugin", "Enabled": True}]}, fh)
        self.engine_root = os.path.join(self.tmp, "UE")
        os.makedirs(os.path.join(self.engine_root, "Engine", "Build"))
        with open(os.path.join(self.engine_root, "Engine", "Build", "Build.version"), "w") as fh:
            json.dump({"MajorVersion": self.engine_version[0], "MinorVersion": self.engine_version[1],
                       "PatchVersion": self.engine_version[2]}, fh)
        self._env = dict(os.environ)
        os.environ["UE_ENGINE_ROOT"] = self.engine_root
        os.environ["UED_PROJECT"] = self.project_dir
        for k in ("UED_TRANSPORT", "UED_MCP_URL"):
            os.environ.pop(k, None)
        unreal._reset(self.project_dir)
        unreal.ENGINE.engine_version = "%d.%d.%d-0+++UE5+Release-%d.%d" % (self.engine_version + self.engine_version[:2])
        unreal.ENGINE.screenshot_delay = 0.05
        for pkg in ("/Game/Props/SM_Rock", "/Game/Props/SM_Tree", "/Game/Props/SM_Crate"):
            write_fake_asset(self.project_dir, pkg, "StaticMesh")
        write_fake_asset(self.project_dir, "/Game/Materials/M_Base", "Material")
        write_fake_asset(self.project_dir, "/Game/Materials/M_Red", "Material")
        # start the editor on an existing saved level
        write_fake_asset(self.project_dir, "/Game/Maps/Start", "World", ".umap", {"partitioned": False, "actors": []})
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level("/Game/Maps/Start")
        self.transport = InProcessTransport()
        self._orig_select = session_mod.Session.select_transport
        self._orig_procs = procs.list_editor_processes
        transport = self.transport

        def select(sess, force_probe=False):
            if transport.down:
                raise TransportError("no editor channel answered (simulated)", code="NO_EDITOR_CHANNEL")
            return transport
        session_mod.Session.select_transport = select
        session_mod.Session.probe_info = lambda sess: {"transport": "inproc"}
        procs.list_editor_processes = lambda: [{"pid": 4242, "cmd": "UnrealEditor %s" %
                                                os.path.join(self.project_dir, "Demo.uproject")}]
        import uedirect.gate as g
        g.procs.list_editor_processes = procs.list_editor_processes

    def tearDown(self):
        session_mod.Session.select_transport = self._orig_select
        procs.list_editor_processes = self._orig_procs
        gate.procs.list_editor_processes = self._orig_procs
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- utils
    def ued(self, *argv, stdin=None, expect_code=None):
        out = io.StringIO()
        old_stdin = sys.stdin
        if stdin is not None:
            sys.stdin = io.StringIO(stdin if isinstance(stdin, str) else json.dumps(stdin))
        try:
            with contextlib.redirect_stdout(out):
                code = cli.main(list(argv))
        finally:
            sys.stdin = old_stdin
        text = out.getvalue().strip()
        data = json.loads(text.splitlines()[-1]) if text else None
        if expect_code is not None:
            self.assertEqual(code, expect_code, "ued %s -> %s" % (" ".join(argv), text))
        return code, data

    def plan(self, ops, **kw):
        kw["ops"] = ops
        return json.dumps(kw)

    def state_dir(self):
        return os.path.join(self.project_dir, "Saved", "UEDirect")

    def evidence(self):
        from uedirect.util import read_jsonl
        return read_jsonl(os.path.join(self.state_dir(), "evidence.jsonl"))

    def content_file(self, package, ext=".uasset"):
        return unreal.ENGINE.content_file(package, ext)
