"""Unit tests: pure host-side logic (no editor, no fake engine needed except where noted)."""

import json
import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: E402,F401  (sets sys.path)

import install  # noqa: E402
from uedirect import batch, completion, hooks, persist, project  # noqa: E402
from uedirect.transports import mcp_http  # noqa: E402
from uedirect.util import UedHostError, append_jsonl, write_json  # noqa: E402

sys.path.insert(0, helpers.ROOT)


def _read(p):
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def _load(p):
    return json.loads(_read(p))


def _count_lines(p):
    return len(_read(p).splitlines())


def _mkproject(root, name="Demo", assoc="5.8"):
    os.makedirs(root, exist_ok=True)
    p = os.path.join(root, name + ".uproject")
    with open(p, "w") as fh:
        json.dump({"EngineAssociation": assoc}, fh)
    return p


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_find_upward_and_ambiguous(self):
        up = _mkproject(self.tmp)
        deep = os.path.join(self.tmp, "Source", "Demo", "Private")
        os.makedirs(deep)
        self.assertEqual(project.find_project(start=deep).uproject, up)
        _mkproject(self.tmp, "Other")
        with self.assertRaises(UedHostError) as ctx:
            project.find_project(start=deep)
        self.assertEqual(ctx.exception.code, "PROJECT_AMBIGUOUS")

    def test_package_to_file_and_plugins(self):
        p = project.Project(_mkproject(self.tmp))
        os.makedirs(os.path.join(self.tmp, "Content", "Maps"))
        open(os.path.join(self.tmp, "Content", "Maps", "City.umap"), "w").close()
        self.assertTrue(p.package_to_file("/Game/Maps/City").endswith(os.path.join("Maps", "City.umap")))
        self.assertTrue(p.package_to_file("/Game/Props/SM_X.SM_X").endswith("SM_X.uasset"))
        plug = os.path.join(self.tmp, "Plugins", "Kit")
        os.makedirs(os.path.join(plug, "Content"))
        open(os.path.join(plug, "Kit.uplugin"), "w").close()
        self.assertTrue(p.package_to_file("/Kit/Rocks/SM_A").startswith(os.path.join(plug, "Content")))
        self.assertIsNone(p.package_to_file("/Unknown/X"))

    def test_python_remote_ini(self):
        p = project.Project(_mkproject(self.tmp))
        os.makedirs(os.path.join(self.tmp, "Config"))
        with open(os.path.join(self.tmp, "Config", "DefaultEngine.ini"), "w") as fh:
            fh.write("[/Script/Engine.Engine]\n+ActiveGameNameRedirects=(A=B)\n+ActiveGameNameRedirects=(C=D)\n"
                     "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nbRemoteExecution=True\n"
                     "RemoteExecutionMulticastGroupEndpoint=239.0.0.2:7000\n")
        s = p.python_remote_settings()
        self.assertTrue(s["enabled"])
        self.assertEqual(s["group"], ("239.0.0.2", 7000))

    def test_versions(self):
        self.assertEqual(project.parse_version("5.8.1-123+++UE5+Release-5.8"), (5, 8, 1))
        self.assertEqual(project.compat((5, 8, 0)), "supported")
        self.assertEqual(project.compat((5, 9, 0)), "supported")
        self.assertEqual(project.compat((5, 4, 4)), "degraded")
        self.assertEqual(project.compat((4, 27, 2)), "incompatible")
        self.assertEqual(project.compat(None), "unknown")

    def test_engine_resolution(self):
        up = project.Project(_mkproject(os.path.join(self.tmp, "Proj")))
        eng = os.path.join(self.tmp, "Eng")
        os.makedirs(os.path.join(eng, "Engine", "Build"))
        with open(os.path.join(eng, "Engine", "Build", "Build.version"), "w") as fh:
            json.dump({"MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 0}, fh)
        old = os.environ.get("UE_ENGINE_ROOT")
        os.environ["UE_ENGINE_ROOT"] = eng
        try:
            self.assertEqual(project.resolve_engine(up)[0], os.path.abspath(eng))
            self.assertEqual(project.read_build_version(eng), (5, 8, 0))
            os.environ["UE_ENGINE_ROOT"] = os.path.join(self.tmp, "nope")
            with self.assertRaises(UedHostError):
                project.resolve_engine(up)
        finally:
            if old is None:
                os.environ.pop("UE_ENGINE_ROOT", None)
            else:
                os.environ["UE_ENGINE_ROOT"] = old
        # project inside a source-build tree with empty association
        inside = project.Project(_mkproject(os.path.join(eng, "Games", "G"), "G", assoc=""))
        os.environ.pop("UE_ENGINE_ROOT", None)
        self.assertEqual(project.resolve_engine(inside), (eng, "parent-of-project"))


class BatchTests(unittest.TestCase):
    def test_scatter_deterministic_and_spaced(self):
        gen = {"op": "scatter", "id": "r", "asset": "/Game/A", "count": 80, "area": {"min": [0, 0], "max": [3000, 3000]},
               "min_dist": 150, "seed": 11}
        a, _ = batch.expand(dict(gen))
        b, _ = batch.expand(dict(gen))
        self.assertEqual(a, b)
        self.assertEqual(len(a), 80)
        pts = [o["loc"] for o in a]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                self.assertGreaterEqual(math.dist(pts[i][:2], pts[j][:2]), 150 - 1e-6)

    def test_scatter_reports_shortfall(self):
        ops, warnings = batch.expand({"op": "scatter", "id": "r", "asset": "/Game/A", "count": 500,
                                      "area": {"min": [0, 0], "max": [500, 500]}, "min_dist": 200})
        self.assertLess(len(ops), 500)
        self.assertTrue(warnings)

    def test_grid_ring_line(self):
        g, _ = batch.expand({"op": "grid", "id": "g", "asset": "/Game/A", "count": [3, 2, 2], "step": [100, 100, 300]})
        self.assertEqual(len(g), 12)
        self.assertEqual(g[-1]["loc"], [200.0, 100.0, 300.0])
        r, _ = batch.expand({"op": "ring", "id": "c", "asset": "/Game/A", "count": 4, "radius": 100})
        self.assertAlmostEqual(r[1]["loc"][1], 100.0, places=3)
        ln, _ = batch.expand({"op": "line", "id": "l", "asset": "/Game/A", "count": 3, "start": [0, 0, 0],
                              "end": [0, 200, 0]})
        self.assertEqual([o["loc"][1] for o in ln], [0.0, 100.0, 200.0])
        self.assertAlmostEqual(ln[0]["rot"], 90.0)

    def test_prepare_ids_and_validation(self):
        ops, _, ids = batch.prepare({"ops": [{"asset": "/Game/A", "loc": [1, 2, 3]}, {"op": "set", "target": "id:x"}]})
        self.assertTrue(ids[0].startswith("a"))
        ops2, _, ids2 = batch.prepare({"ops": [{"asset": "/Game/A", "loc": [1, 2, 3]}]})
        self.assertEqual(ids, ids2)  # same op -> same auto id -> idempotent
        for bad in ([{"op": "explode"}], [{"op": "set"}], [{"loc": [0, 0, 0]}],
                    [{"asset": "/Game/A", "id": "x"}, {"asset": "/Game/B", "id": "x"}]):
            with self.assertRaises(UedHostError):
                batch.prepare({"ops": bad})

    def test_referenced_assets_excludes_created(self):
        ops, _, _ = batch.prepare({"ops": [{"op": "mi", "path": "/Game/M/MI_A", "parent": "/Game/M/M_Base"},
                                           {"asset": "/Game/P/SM_A", "material": "/Game/M/MI_A"}]})
        self.assertEqual(batch.referenced_assets(ops), ["/Game/M/M_Base", "/Game/P/SM_A"])


class HookTests(unittest.TestCase):
    def deny(self, tool, ti):
        return bool(hooks.check_pre_tool({"tool_name": tool, "tool_input": ti}))

    def test_native_asset_writes_denied(self):
        self.assertTrue(self.deny("Write", {"file_path": "/p/Content/Maps/City.umap", "content": "{}"}))
        self.assertTrue(self.deny("Edit", {"file_path": "C:\\P\\Content\\X.UASSET"}))
        self.assertTrue(self.deny("apply_patch", {"command": "*** Begin Patch\n*** Add File: Content/Maps/A.umap\n+{}"}))
        self.assertTrue(self.deny("Bash", {"command": "echo '{}' > Content/Maps/City.umap"}))
        self.assertTrue(self.deny("Bash", {"command": "cp layout.json Content/Maps/City.umap"}))
        self.assertTrue(self.deny("PowerShell", {"command": "Set-Content -Path Content\\A.uasset -Value x"}))
        self.assertTrue(self.deny("Write", {"file_path": "/p/Saved/UEDirect/evidence.jsonl"}))

    def test_normal_work_allowed(self):
        self.assertFalse(self.deny("Write", {"file_path": "/p/Source/Demo/Door.cpp"}))
        self.assertFalse(self.deny("Bash", {"command": "git status && ls Content/Maps/*.umap"}))
        self.assertFalse(self.deny("Bash", {"command": "python3 .claude/skills/ue-direct/scripts/ued.py batch plan.json"}))
        self.assertFalse(self.deny("Read", {"file_path": "/p/Content/Maps/City.umap"}))

    def test_stop_and_post_tool_without_project(self):
        tmp = tempfile.mkdtemp()
        self.assertEqual(hooks.handle("stop", {"cwd": tmp}), (0, None, ""))
        self.assertEqual(hooks.handle("post-tool", {"cwd": tmp, "tool_name": "mcp__unreal__call_tool"}), (0, None, ""))

    def test_post_tool_records_external_mutation(self):
        tmp = tempfile.mkdtemp()
        _mkproject(tmp)
        hooks.handle("post-tool", {"cwd": tmp, "tool_name": "mcp__unreal-mcp__call_tool",
                                   "tool_input": {"tool_name": "EditorToolset.PythonScriptingTools.execute_python"}})
        sdir = os.path.join(tmp, "Saved", "UEDirect")
        task = completion.load_task(sdir)
        self.assertEqual(task["kind"], "auto")
        v = completion.evaluate(sdir)
        self.assertIn("save after the last change (`ued save`)", v["missing"])
        # read-only tools and relayed ued snippets are not counted
        n = _count_lines(os.path.join(sdir, "evidence.jsonl"))
        hooks.handle("post-tool", {"cwd": tmp, "tool_name": "mcp__unreal-mcp__describe_tools", "tool_input": {}})
        hooks.handle("post-tool", {"cwd": tmp, "tool_name": "mcp__unreal-mcp__call_tool",
                                   "tool_input": {"code": "importlib.import_module('uedirect_editor')"}})
        self.assertEqual(_count_lines(os.path.join(sdir, "evidence.jsonl")), n)

    def test_stop_hook_gives_up_after_no_progress(self):
        tmp = tempfile.mkdtemp()
        _mkproject(tmp)
        sdir = os.path.join(tmp, "Saved", "UEDirect")
        os.makedirs(sdir)
        completion.start_task(sdir, "content")
        outs = [hooks.handle("stop", {"cwd": tmp})[1] for _ in range(5)]
        self.assertEqual([o.get("decision") for o in outs[:3]], ["block"] * 3)
        self.assertIn("INCOMPLETE", outs[3]["systemMessage"])


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.sdir = tempfile.mkdtemp()

    def ev(self, **rec):
        rec.setdefault("t", time.time())
        append_jsonl(os.path.join(self.sdir, "evidence.jsonl"), rec)

    def test_code_task_needs_nothing(self):
        completion.start_task(self.sdir, "code")
        self.assertEqual(completion.evaluate(self.sdir)["status"], "success")

    def test_auto_task_asset_only_needs_save_not_reload(self):
        completion.start_task(self.sdir, "auto")
        self.ev(src="editor", kind="batch", mutated=True, actor_mutated=False, counts={"created": 1}, ok_ids=["m"])
        v = completion.evaluate(self.sdir)
        self.assertNotIn("reload_verified", v["checks"])
        self.ev(src="editor", kind="save", failed=[])
        self.ev(src="host", kind="save_files", ok=True)
        self.assertEqual(completion.evaluate(self.sdir)["status"], "success")

    def test_change_after_save_requires_new_save(self):
        completion.start_task(self.sdir, "content")
        self.ev(src="editor", kind="batch", mutated=True, counts={"created": 1}, ok_ids=["a"])
        self.ev(src="editor", kind="save", failed=[])
        self.ev(src="host", kind="save_files", ok=True)
        self.assertEqual(completion.evaluate(self.sdir)["status"], "success")
        self.ev(src="editor", kind="batch", mutated=True, counts={"modified": 1}, ok_ids=["a"], t=time.time() + 1)
        self.assertEqual(completion.evaluate(self.sdir)["status"], "incomplete")

    def test_explicit_task_inherits_auto_task_work(self):
        completion.ensure_task(self.sdir)
        self.ev(src="editor", kind="batch", mutated=True, actor_mutated=True, counts={"created": 1}, ok_ids=["a"])
        completion.start_task(self.sdir, "content")
        self.assertTrue(completion.evaluate(self.sdir)["checks"]["editor_changes"])

    def test_failed_review_blocks(self):
        completion.start_task(self.sdir, "visual")
        t = time.time()
        self.ev(src="editor", kind="batch", mutated=True, actor_mutated=True, counts={"created": 1}, ok_ids=["a"], t=t)
        self.ev(src="editor", kind="save", failed=[], t=t + 1)
        self.ev(src="host", kind="save_files", ok=True, t=t + 1)
        self.ev(src="host", kind="shot", ok=True, shot="s1", t=t + 2)
        self.ev(src="host", kind="review", shot="s1", verdict="fail", notes="floating rocks", t=t + 3)
        v = completion.evaluate(self.sdir)
        self.assertIn("floating rocks", " ".join(v["missing"]))


class InstallerTests(unittest.TestCase):
    def test_merge_preserves_and_is_idempotent(self):
        user = {"permissions": {"allow": ["Bash(ls)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]}}
        tpl = install._render(os.path.join(helpers.ROOT, "integrations", "claude-code", "hooks.template.json"),
                              'python3 "$CLAUDE_PROJECT_DIR/.claude/skills/ue-direct/scripts/ued.py"')
        once = install.merge_hooks(user, tpl)
        twice = install.merge_hooks(once, tpl)
        self.assertEqual(once, twice)
        self.assertEqual(once["permissions"], user["permissions"])
        stop_cmds = [h["command"] for g in once["hooks"]["Stop"] for h in g["hooks"]]
        self.assertIn("notify.sh", stop_cmds)
        self.assertEqual(len(stop_cmds), 2)
        removed = install.remove_hooks(once)
        self.assertEqual(removed["hooks"], user["hooks"])

    def test_blocks_preserve_user_text(self):
        user = "# My rules\n\nUse tabs.\n"
        block = "<!-- UE-DIRECT:BEGIN x -->\nhello\n<!-- UE-DIRECT:END -->\n"
        once = install.upsert_block(user, block)
        self.assertTrue(once.startswith(user))
        self.assertEqual(install.upsert_block(once, block.replace("hello", "v2")).count("UE-DIRECT:BEGIN"), 1)
        self.assertEqual(install.remove_block(once).strip(), user.strip())

    def test_full_install_uninstall(self):
        tmp = tempfile.mkdtemp()
        _mkproject(tmp)
        with open(os.path.join(tmp, "CLAUDE.md"), "w") as fh:
            fh.write("Existing team rules.\n")
        os.makedirs(os.path.join(tmp, ".claude"))
        write_json(os.path.join(tmp, ".claude", "settings.json"), {"model": "x"})
        install.install(tmp, {"claude", "codex"})
        self.assertTrue(os.path.isfile(os.path.join(tmp, ".claude", "skills", "ue-direct", "SKILL.md")))
        self.assertTrue(os.path.isfile(os.path.join(tmp, ".agents", "skills", "ue-direct", "scripts", "ued.py")))
        settings = _load(os.path.join(tmp, ".claude", "settings.json"))
        self.assertEqual(settings["model"], "x")
        self.assertIn("Stop", settings["hooks"])
        codex = _load(os.path.join(tmp, ".codex", "hooks.json"))
        self.assertIn("PreToolUse", codex["hooks"])
        self.assertIn("UE-DIRECT:BEGIN", _read(os.path.join(tmp, "AGENTS.md")))
        install.uninstall(tmp)
        self.assertFalse(os.path.exists(os.path.join(tmp, ".claude", "skills", "ue-direct")))
        self.assertEqual(_load(os.path.join(tmp, ".claude", "settings.json")), {"model": "x"})
        self.assertEqual(_read(os.path.join(tmp, "CLAUDE.md")).strip(), "Existing team rules.")


class PersistTests(unittest.TestCase):
    def test_package_tag_and_saved_files(self):
        tmp = tempfile.mkdtemp()
        p = project.Project(_mkproject(tmp))
        real = os.path.join(tmp, "Content", "Maps", "City.umap")
        os.makedirs(os.path.dirname(real))
        with open(real, "wb") as fh:
            fh.write(persist.PACKAGE_FILE_TAG + b"rest")
        fake = os.path.join(tmp, "Content", "Maps", "Fake.umap")
        with open(fake, "w") as fh:
            fh.write('{"actors": []}')
        self.assertTrue(persist.is_ue_package(real))
        self.assertFalse(persist.is_ue_package(fake))
        self.assertEqual(persist.scan_fake_assets(p)["fake"], 1)
        res = persist.verify_saved_files(p, ["/Game/Maps/City", "/Game/__ExternalActors__/Maps/City/A/B"],
                                         time.time() - 5, level="/Game/Maps/City")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["deleted"], 1)
        res = persist.verify_saved_files(p, ["/Game/Maps/City"], time.time() + 60)
        self.assertFalse(res["ok"])  # file older than the save: not rewritten


class McpParsingTests(unittest.TestCase):
    def test_sse_and_schema_helpers(self):
        msgs = mcp_http._parse_sse('event: message\ndata: {"id": 1}\n\ndata: {"id":\ndata: 2}\n\n')
        self.assertEqual([m["id"] for m in msgs], [1, 2])
        self.assertEqual(mcp_http._code_param({"properties": {"timeout": {"type": "number"},
                                                              "src": {"type": "string"}}, "required": ["src"]}), "src")
        self.assertEqual(mcp_http._find_schema({"x": [{"name": "a.execute_python", "inputSchema": {
            "properties": {"code": {"type": "string"}}}}]}, "a.execute_python")["properties"]["code"]["type"], "string")

    def test_discover_urls_from_project_configs(self):
        tmp = tempfile.mkdtemp()
        write_json(os.path.join(tmp, ".mcp.json"), {"mcpServers": {"unreal-mcp-5-8": {"type": "http",
                                                                                     "url": "http://127.0.0.1:8001/mcp"}}})
        os.makedirs(os.path.join(tmp, ".codex"))
        with open(os.path.join(tmp, ".codex", "config.toml"), "w") as fh:
            fh.write('[mcp_servers.unreal-mcp]\nurl = "http://127.0.0.1:8002/mcp"\n')
        urls = mcp_http.discover_urls(tmp)
        self.assertEqual(urls, ["http://127.0.0.1:8001/mcp", "http://127.0.0.1:8002/mcp", "http://127.0.0.1:8000/mcp"])


class RelayTests(helpers.ProjectTestCase):
    def test_relay_roundtrip(self):
        os.environ["UED_TRANSPORT"] = "relay"
        code, res = self.ued("inspect")
        self.assertEqual(code, 3, res)
        self.assertEqual(res["code"], "RELAY_PENDING")
        self.assertLess(len(res["snippet"]), 600)
        exec(compile(res["snippet"], "<relay>", "exec"), {})   # what the agent's MCP python tool would do
        code, out = self.ued("result", res["request"])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["level"], "/Game/Maps/Start")


if __name__ == "__main__":
    unittest.main()
