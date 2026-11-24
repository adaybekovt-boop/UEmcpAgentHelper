"""Regression tests for the findings of the independent code review (one test per finding)."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: E402
from helpers import unreal  # noqa: E402

ROCK = "/Game/Props/SM_Rock"


class ReviewRegressions(helpers.ProjectTestCase):
    def _session(self):
        from uedirect.project import find_project
        from uedirect.session import Session
        return Session(find_project(self.project_dir))

    def test_r01_duplicate_delivery_runs_once(self):
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "a"}]), expect_code=0)
        sess = self._session()
        rid, path = sess._write_request("batch", {"ops": [{"op": "transform", "id": "t", "target": "id:a",
                                                           "offset": [100, 0, 0]}]})
        code = __import__("uedirect.session", fromlist=["x"]).bootstrap_snippet(path)
        exec(compile(code, "<1>", "exec"), {})
        exec(compile(code, "<2>", "exec"), {})       # retried delivery of the same request
        a = [x for x in unreal.ENGINE.world.actors if "ued:a" in x.tags][0]
        self.assertEqual(a.loc.x, 100.0)

    def test_r02_inspect_task_with_edits_needs_save(self):
        self.ued("task", "start", "--kind", "inspect", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "a"}]), expect_code=0)
        code, res = self.ued("complete")
        self.assertEqual(res["status"], "incomplete")

    def test_r03_other_tool_changes_must_be_saved(self):
        self.ued("task", "start", "--kind", "content", expect_code=0)   # baseline: nothing dirty
        from uedirect import hooks
        hooks.handle("post-tool", {"cwd": self.project_dir, "tool_name": "mcp__unreal-mcp__call_tool",
                                   "tool_input": {"tool_name": "EditorToolset.PythonScriptingTools.execute_python",
                                                  "arguments": {"code": "unreal.get_editor_subsystem(...)"}}})
        red = unreal.EditorAssetLibrary.load_asset("/Game/Materials/M_Red")
        unreal.ENGINE.mark_dirty(red._path)                 # what the other tool changed
        code, res = self.ued("complete")
        self.assertEqual(res["status"], "incomplete")
        self.assertIn("M_Red", json.dumps(res["missing"]))
        self.ued("save", expect_code=0)                     # task-dirtied packages are saved
        self.assertNotIn("/Game/Materials/M_Red", unreal.ENGINE.dirty)
        code, res = self.ued("complete")
        self.assertEqual(res["status"], "success", res)

    def test_r03b_user_dirty_before_task_is_not_saved(self):
        unreal.ENGINE.mark_dirty("/Game/Materials/M_Base")  # user's unsaved edit before the task
        unreal.EditorAssetLibrary.load_asset("/Game/Materials/M_Base")
        self.ued("task", "start", "--kind", "content", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "a"}]), expect_code=0)
        code, res = self.ued("save")
        self.assertEqual(code, 0, res)
        self.assertIn("/Game/Materials/M_Base", unreal.ENGINE.dirty)
        self.assertEqual(self.ued("complete")[1]["status"], "success")

    def test_r04_unrelated_dirty_content_does_not_block_reload(self):
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "a"}]), expect_code=0)
        self.ued("save", expect_code=0)
        unreal.EditorAssetLibrary.load_asset("/Game/Materials/M_Red")
        unreal.ENGINE.mark_dirty("/Game/Materials/M_Red")
        code, res = self.ued("verify", "--reload")
        self.assertTrue(res["reloaded"] and res["ok"], res)

    def test_r05_level_open_is_not_a_task(self):
        from uedirect import hooks
        self.ued("level", "open", "/Game/Maps/Start", expect_code=0)
        self.assertIsNone(hooks.handle("stop", {"cwd": self.project_dir})[1])

    def test_r06_read_only_tools_ignored_python_counted(self):
        from uedirect import hooks
        hooks.handle("post-tool", {"cwd": self.project_dir, "tool_name": "mcp__unreal-mcp__take_screenshot"})
        self.assertFalse([e for e in self.evidence() if e["kind"] == "external_call"])
        hooks.handle("post-tool", {"cwd": self.project_dir, "tool_name": "mcp__unreal-mcp__call_tool",
                                   "tool_input": {"t": "EditorToolset.PythonScriptingTools.execute_python",
                                                  "a": {"code": "x = unreal.get_editor_subsystem(A).spawn()"}}})
        self.assertEqual(len([e for e in self.evidence() if e["kind"] == "external_call"]), 1)

    def test_r07_rollback_removes_created_assets(self):
        code, res = self.ued("batch", "-", "--on-error", "rollback", stdin=self.plan([
            {"op": "mi", "path": "/Game/Materials/MI_New", "parent": "/Game/Materials/M_Base"},
            {"asset": "/Game/Nope/SM_X", "id": "bad"}]))
        self.assertEqual(code, 1)
        self.assertFalse(unreal.EditorAssetLibrary.does_asset_exist("/Game/Materials/MI_New"))
        big = self.plan([{"asset": ROCK, "id": "r%d" % i} for i in range(300)])
        code, res = self.ued("batch", "-", "--on-error", "rollback", stdin=big)
        self.assertEqual(res["code"], "ROLLBACK_TOO_LARGE")

    def test_r09_precheck_accepts_object_paths(self):
        self.ued("assets", "refresh", expect_code=0)
        code, res = self.ued("batch", "-", stdin=self.plan([{"asset": ROCK + ".SM_Rock", "id": "a"}]))
        self.assertEqual(code, 0, res)

    def test_r11_empty_plan_and_bad_args_return_json(self):
        code, res = self.ued("batch", "-", stdin=json.dumps({"ops": []}))
        self.assertEqual((code, res["code"]), (1, "EMPTY_PLAN"))
        code, res = self.ued("shot", "--res", "960")
        self.assertEqual(res["code"], "BAD_ARGS")

    def test_r13_delete_cap_is_per_plan(self):
        self.ued("batch", "-", stdin=self.plan([{"op": "grid", "id": "g", "asset": ROCK, "count": [30, 1],
                                                 "step": [100, 100]}]), expect_code=0)
        os.environ["UED_CHUNK_SIZE"] = "10"
        ops = [{"op": "delete", "target": "id:g#%d" % i} for i in range(30)]
        code, res = self.ued("batch", "-", "--allow-delete", "--max-delete", "15", stdin=self.plan(ops))
        self.assertEqual(res["deleted"], 15)
        self.assertEqual(len(unreal.ENGINE.world.actors), 15)

    def test_r14_pie_stopped_even_if_status_fails(self):
        from uedirect.session import Session
        orig = Session.request

        def flaky(sess, cmd, args=None, **kw):
            if cmd == "pie_status":
                raise helpers.TransportError("lost", code="TRANSPORT_UNAVAILABLE")
            return orig(sess, cmd, args, **kw)
        Session.request = flaky
        try:
            self.ued("pie", "--seconds", "1.2")
        finally:
            Session.request = orig
        self.assertFalse(unreal.ENGINE.pie)


class NonMcpPort(unittest.TestCase):
    def test_r10_non_mcp_service_does_not_crash_probe(self):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = b"<html>hello</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            from uedirect.transports.mcp_http import McpHttpTransport
            self.assertIsNone(McpHttpTransport("http://127.0.0.1:%d/mcp" % srv.server_address[1]).probe())
        finally:
            srv.shutdown()
            srv.server_close()


class ProcessMatching(unittest.TestCase):
    def test_r12_name_must_be_whole_component(self):
        from uedirect import procs

        class P(object):
            uproject = "/p/Game/Game.uproject"
            name = "Game"
        mine, others = procs.processes_for_project([{"pid": 1, "cmd": "UnrealEditor /x/MyGame/MyGame.uproject"},
                                                    {"pid": 2, "cmd": 'UnrealEditor "D:/w/Game.uproject" -log'}], P)
        self.assertEqual([p["pid"] for p in mine], [2])


class HookLauncher(unittest.TestCase):
    def test_r15_missing_skill_never_blocks(self):
        import install
        tmp = tempfile.mkdtemp()
        cmd = install.hook_command(sys.executable, "pre-tool", project_env="CLAUDE_PROJECT_DIR")
        env = dict(os.environ, CLAUDE_PROJECT_DIR=tmp)
        p = subprocess.run(cmd, shell=True, input='{"tool_name":"Write","tool_input":{"file_path":"a.umap"}}',
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, "")
        # with the skill present the same launcher runs the hook and denies the write
        install.install(tmp, {"claude"}, hooks=False, instructions=False)
        p = subprocess.run(cmd, shell=True, input='{"tool_name":"Write","tool_input":{"file_path":"a.umap"}}',
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn('"deny"', p.stdout)


if __name__ == "__main__":
    unittest.main()
