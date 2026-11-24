"""The 20 required scenarios, run end-to-end through the real `ued` CLI, the real
host runtime, the real editor-side package and a FAKE `unreal` module.

MOCK TESTS: they prove the harness logic (gating, idempotency, save/verify,
completion rules). They do not prove behaviour inside a real Unreal Editor;
see tests/integration for that.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: E402
from helpers import unreal  # noqa: E402

ROCK = "/Game/Props/SM_Rock"


class Scenarios(helpers.ProjectTestCase):

    # 1 ------------------------------------------------------------------
    def test_01_editor_closed(self):
        self.transport.down = True
        import uedirect.gate as g
        g.procs.list_editor_processes = lambda: []
        code, res = self.ued("gate")
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "blocked")
        self.assertEqual(res["blockers"][0]["code"], "EDITOR_CLOSED")
        code, res = self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r1"}]))
        self.assertEqual(code, 1)
        self.assertEqual(res["code"], "GATE_BLOCKED")
        self.assertFalse(os.path.exists(self.content_file("/Game/Maps/City", ".umap")))

    # 2 ------------------------------------------------------------------
    def test_02_wrong_project(self):
        other = os.path.join(self.tmp, "Other")
        os.makedirs(other)
        with open(os.path.join(other, "Other.uproject"), "w") as fh:
            fh.write("{}")
        orig = unreal.Paths.get_project_file_path
        unreal.Paths.get_project_file_path = staticmethod(lambda: os.path.join(other, "Other.uproject"))
        try:
            code, res = self.ued("gate")
            self.assertEqual(res["status"], "blocked")
            self.assertIn("WRONG_PROJECT", [b["code"] for b in res["blockers"]])
            # even if the host gate were bypassed, the in-editor guard refuses the batch
            from uedirect.project import find_project
            from uedirect.session import Session
            from uedirect.util import UedHostError
            sess = Session(find_project(self.project_dir))
            with self.assertRaises(UedHostError) as ctx:
                sess.request("batch", {"ops": [{"op": "spawn", "id": "x", "asset": ROCK}]})
            self.assertEqual(ctx.exception.code, "GUARD_WRONG_PROJECT")
            self.assertEqual(len(unreal.ENGINE.world.actors), 0)
        finally:
            unreal.Paths.get_project_file_path = orig

    # 3 ------------------------------------------------------------------
    def test_03_mcp_unavailable(self):
        from uedirect.project import find_project
        from uedirect.session import Session
        from uedirect.transports.base import TransportError
        helpers.session_mod.Session.select_transport = self._orig_select  # real channel selection
        os.environ["UED_TRANSPORT"] = "mcp"
        os.environ["UED_MCP_URL"] = "http://127.0.0.1:9/mcp"  # nothing listens on the discard port
        sess = Session(find_project(self.project_dir))
        with self.assertRaises(TransportError) as ctx:
            sess.select_transport(force_probe=True)
        self.assertEqual(ctx.exception.code, "NO_EDITOR_CHANNEL")
        self.assertIn("ued up", ctx.exception.hint)

    # 4 ------------------------------------------------------------------
    def test_04_disconnect_during_execution(self):
        self.ued("level", "new", "/Game/Maps/City", expect_code=0)
        self.transport.drop_after = 1   # command ran, reply lost
        code, res = self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r%d" % i, "loc": [i * 200, 0, 0]}
                                                          for i in range(5)]))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["created"], 5)
        self.transport.fail_next = 1    # connection reset before execution -> retried
        code, res = self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r%d" % i, "loc": [i * 200, 0, 0]}
                                                          for i in range(6)]))
        self.assertEqual(code, 0, res)
        self.assertEqual((res["created"], res.get("unchanged")), (1, 5))
        self.assertEqual(len(unreal.ENGINE.world.actors), 6)

    # 5 ------------------------------------------------------------------
    def test_05_running_but_not_ready(self):
        unreal.ENGINE.ar_loading = True
        code, res = self.ued("gate")
        self.assertEqual(res["status"], "blocked")
        self.assertIn("EDITOR_NOT_READY", [b["code"] for b in res["blockers"]])
        unreal.ENGINE.ar_loading = False
        saved_world, unreal.ENGINE.world = unreal.ENGINE.world, None
        code, res = self.ued("gate")
        self.assertEqual(res["status"], "blocked")
        unreal.ENGINE.world = saved_world
        code, res = self.ued("gate")
        self.assertEqual(res["status"], "ready")

    # 6 ------------------------------------------------------------------
    def test_06_level_not_saved(self):
        self.ued("task", "start", "--kind", "content", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r1"}]), expect_code=0)
        code, res = self.ued("complete")
        self.assertEqual(code, 1)
        self.assertFalse(res["saved"])
        self.assertTrue(any("save" in m for m in res["missing"]))

    # 7 ------------------------------------------------------------------
    def test_07_json_generator_instead_of_level(self):
        self.ued("task", "start", "--kind", "level", "--level", "/Game/Maps/City", expect_code=0)
        # what a misbehaving agent produces: a generator + a JSON "map"
        fake = self.content_file("/Game/Maps/City", ".umap")
        os.makedirs(os.path.dirname(fake), exist_ok=True)
        with open(fake, "w") as fh:
            json.dump({"actors": [{"mesh": ROCK, "loc": [0, 0, 0]}]}, fh)
        with open(os.path.join(self.project_dir, "generate_city.py"), "w") as fh:
            fh.write("# writes a JSON level description\n")
        code, res = self.ued("audit")
        self.assertEqual(code, 1)
        self.assertEqual(res["fake"], 1)
        code, res = self.ued("complete")
        self.assertEqual(code, 1)
        self.assertIn("no editor changes recorded", " ".join(res["missing"]))
        # and the hook refuses the write in the first place
        from uedirect import hooks
        _, out, _ = hooks.handle("pre-tool", {"tool_name": "Write", "tool_input": {"file_path": fake}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    # 8 ------------------------------------------------------------------
    def test_08_external_actors_saved(self):
        self.ued("level", "new", "/Game/Maps/Open", "--partitioned", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r%d" % i, "loc": [i * 300, 0, 0]}
                                                for i in range(4)]), expect_code=0)
        ext = [a.external_package for a in unreal.ENGINE.world.actors]
        self.assertTrue(all(p and "/__ExternalActors__/" in p for p in ext))
        code, res = self.ued("save")
        self.assertEqual(code, 0, res)
        for p in ext:
            self.assertTrue(os.path.isfile(self.content_file(p)), p)
        self.assertEqual(unreal.ENGINE.dirty, set())
        # an external actor whose save fails must surface as a failed save
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "late", "loc": [0, 900, 0]}]), expect_code=0)
        late = [a for a in unreal.ENGINE.world.actors if "ued:late" in a.tags][0].external_package
        unreal.ENGINE.fail_save.add(late)
        code, res = self.ued("save")
        self.assertEqual(code, 1)
        self.assertIn(late, res["failed"])

    # 9 ------------------------------------------------------------------
    def test_09_missing_asset(self):
        # without a cache the editor reports it per op
        code, res = self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "ok"},
                                                          {"asset": "/Game/Props/SM_Rok", "id": "bad"}]))
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["failures"][0]["code"], "MISSING_ASSET")
        # with a fresh cache the plan is rejected before any editor call, with suggestions
        self.ued("assets", "refresh", expect_code=0)
        calls = self.transport.calls
        code, res = self.ued("batch", "-", stdin=self.plan([{"asset": "/Game/Props/SM_Rok", "id": "bad"}]))
        self.assertEqual(res["code"], "MISSING_ASSETS")
        self.assertIn(ROCK, res["suggestions"]["/Game/Props/SM_Rok"])
        self.assertEqual(self.transport.calls, calls)

    # 10 -----------------------------------------------------------------
    def test_10_stale_cache(self):
        self.ued("assets", "refresh", expect_code=0)
        code, res = self.ued("assets", "status")
        self.assertEqual(res["cache"], "fresh")
        helpers.write_fake_asset(self.project_dir, "/Game/Props/SM_Barrel", "StaticMesh")
        code, res = self.ued("assets", "status")
        self.assertEqual(res["cache"], "stale:content")
        code, res = self.ued("assets", "find", "barrel")
        self.assertIn("cache_refreshed", res)
        self.assertEqual(res["results"][0], "/Game/Props/SM_Barrel|StaticMesh")

    # 11 -----------------------------------------------------------------
    def test_11_partial_batch_and_rollback(self):
        ops = [{"asset": ROCK, "id": "a"}, {"op": "set", "target": "id:a", "props": {"nope": 1}, "id": "badset"},
               {"asset": ROCK, "id": "b", "loc": [300, 0, 0]}]
        code, res = self.ued("batch", "-", stdin=self.plan(ops))
        self.assertEqual(res["status"], "partial")
        self.assertEqual((res["created"], res["failed"]), (2, 1))
        before = len(unreal.ENGINE.world.actors)
        code, res = self.ued("batch", "-", "--on-error", "rollback",
                             stdin=self.plan([{"asset": ROCK, "id": "c", "loc": [600, 0, 0]},
                                              {"asset": "/Game/Nope/SM_X", "id": "d"}]))
        self.assertEqual(code, 1)
        self.assertEqual(len(unreal.ENGINE.world.actors), before)  # 'c' was rolled back
        self.assertEqual(res.get("rolled_back"), 1)
        # unresolved failures block completion until fixed or explicitly waived
        code, v = self.ued("status")
        self.assertIn("failed", " ".join(v["completion"]["missing"]))
        self.ued("task", "waive", "--op-id", "badset", "--reason", "property does not exist; not needed")
        self.ued("task", "waive", "--op-id", "d", "--reason", "asset path typo, replaced by c")

    # 12 -----------------------------------------------------------------
    def test_12_repeated_operation_is_idempotent(self):
        plan = self.plan([{"op": "grid", "id": "crates", "asset": "/Game/Props/SM_Crate", "count": [5, 4],
                           "step": [200, 200]}])
        code, first = self.ued("batch", "-", stdin=plan)
        code, second = self.ued("batch", "-", stdin=plan)
        self.assertEqual(first["created"], 20)
        self.assertEqual((second["created"], second["unchanged"]), (0, 20))
        self.assertEqual(len(unreal.ENGINE.world.actors), 20)

    # 13 -----------------------------------------------------------------
    def test_13_save_failure(self):
        self.ued("task", "start", "--kind", "content", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r1"}]), expect_code=0)
        unreal.ENGINE.fail_save.add("/Game/Maps/Start")
        code, res = self.ued("save")
        self.assertEqual(code, 1)
        self.assertEqual(res["status"], "failed")
        code, res = self.ued("complete")
        self.assertEqual(res["status"], "incomplete")
        self.assertFalse(res["saved"])

    # 14 -----------------------------------------------------------------
    def test_14_user_unsaved_changes_are_protected(self):
        user_actor = unreal.get_editor_subsystem(unreal.EditorActorSubsystem).spawn_actor_from_class(
            unreal.PointLight, unreal.Vector(1, 2, 3))
        self.assertIn("/Game/Maps/Start", unreal.ENGINE.dirty)
        code, res = self.ued("level", "new", "/Game/Maps/City")
        self.assertEqual(code, 1)
        self.assertEqual(res["code"], "UNSAVED_CHANGES")
        self.assertIn(user_actor, unreal.ENGINE.world.actors)  # nothing discarded
        code, res = self.ued("verify", "--reload")
        self.assertFalse(res["reloaded"])                       # reload refused while dirty
        self.assertIn(user_actor, unreal.ENGINE.world.actors)
        code, res = self.ued("level", "new", "/Game/Maps/City", "--save-dirty")
        self.assertEqual(code, 0, res)
        data = unreal.ENGINE.read_pkg(self.content_file("/Game/Maps/Start", ".umap"))
        self.assertEqual(len(data["actors"]), 1)                # the user's light was saved, not lost

    # 15 -----------------------------------------------------------------
    def test_15_premature_completion(self):
        from uedirect import hooks
        self.ued("task", "start", "--kind", "level", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r1"}]), expect_code=0)
        code, res = self.ued("complete")
        self.assertEqual(code, 1)
        self.assertGreaterEqual(len(res["missing"]), 3)
        _, out, _ = hooks.handle("stop", {"cwd": self.project_dir})
        self.assertEqual(out["decision"], "block")
        self.ued("task", "block", "--reason", "user asked to stop", expect_code=0)
        _, out, _ = hooks.handle("stop", {"cwd": self.project_dir})
        self.assertIsNone(out)

    # 16 -----------------------------------------------------------------
    def test_16_create_and_save_real_level(self):
        self.ued("task", "start", "--kind", "level", "--level", "/Game/Maps/City", "--min-actors", "10",
                 expect_code=0)
        self.ued("level", "new", "/Game/Maps/City", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([
            {"op": "scatter", "id": "rocks", "asset": ROCK, "count": 30, "area": {"min": [0, 0], "max": [4000, 4000]},
             "min_dist": 150, "seed": 7, "yaw": [0, 360], "scale": [0.8, 1.3], "folder": "Props/Rocks"},
            {"class": "DirectionalLight", "id": "sun", "rot": [0, -40, 25]},
            {"class": "SkyLight", "id": "sky"},
            {"class": "PlayerStart", "id": "start", "loc": [2000, 2000, 100]}]), expect_code=0)
        self.ued("save", expect_code=0)
        f = self.content_file("/Game/Maps/City", ".umap")
        from uedirect.persist import is_ue_package
        self.assertTrue(is_ue_package(f))
        self.ued("verify", "--reload", expect_code=0)
        code, shot = self.ued("shot", expect_code=0)
        self.ued("review", shot["shot"], "--verdict", "pass", "--notes", "rocks evenly spread, lit, no overlaps",
                 expect_code=0)
        code, res = self.ued("complete")
        self.assertEqual(code, 0, res)
        self.assertEqual((res["status"], res["saved"], res["verified"], res["visual"]), ("success", True, True, True))
        self.assertEqual(res["created"], 33)

    # 17 -----------------------------------------------------------------
    def test_17_reopen_saved_level(self):
        self.ued("level", "new", "/Game/Maps/City", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r%d" % i, "loc": [i * 100, 50, 0],
                                                 "label": "Rock_%d" % i} for i in range(3)]), expect_code=0)
        self.ued("save", expect_code=0)
        self.ued("level", "open", "/Game/Maps/Start", expect_code=0)
        self.assertEqual(len(unreal.ENGINE.world.actors), 0)
        self.ued("level", "open", "/Game/Maps/City", expect_code=0)
        code, res = self.ued("actors", "id:r*", "--fields", "loc,mesh")
        self.assertEqual(res["matched"], 3)
        self.assertEqual(sorted(a["label"] for a in res["actors"]), ["Rock_0", "Rock_1", "Rock_2"])
        self.assertTrue(all(a["mesh"] == ROCK for a in res["actors"]))

    # 18 -----------------------------------------------------------------
    def test_18_mass_placement_is_chunked(self):
        calls = self.transport.calls
        code, res = self.ued("batch", "-", stdin=self.plan([
            {"op": "grid", "id": "g", "asset": "/Game/Props/SM_Crate", "count": [40, 25], "step": [150, 150]}]))
        self.assertEqual(code, 0, res)
        self.assertEqual((res["created"], res["chunks"]), (1000, 4))
        self.assertLessEqual(self.transport.calls - calls, 4 + 3)  # 4 chunks + gate/selftest
        self.assertLess(len(json.dumps(res)), 300)                  # compact, independent of N

    # 19 -----------------------------------------------------------------
    def test_19_saved_state_matches_expected(self):
        self.ued("task", "start", "--kind", "level", "--level", "/Game/Maps/Open", expect_code=0)
        self.ued("level", "new", "/Game/Maps/Open", "--partitioned", expect_code=0)
        self.ued("batch", "-", stdin=self.plan([{"asset": ROCK, "id": "r%d" % i, "loc": [i * 100, 0, 0]}
                                                for i in range(5)]), expect_code=0)
        self.ued("save", expect_code=0)
        code, res = self.ued("verify", "--reload")
        self.assertTrue(res["ok"], res)
        # something outside the editor deletes one saved actor file (e.g. bad VCS sync)
        victim = [a for a in unreal.ENGINE.world.actors if "ued:r3" in a.tags][0].external_package
        os.remove(self.content_file(victim))
        code, res = self.ued("verify", "--reload")
        self.assertFalse(res["ok"])
        self.assertTrue(any("r3" in p for p in res["problems"]), res["problems"])

    # 20 -----------------------------------------------------------------
    def test_20_incompatible_engine(self):
        unreal.ENGINE.engine_version = "4.27.2-0+++UE4+Release-4.27"
        code, res = self.ued("gate")
        self.assertIn("ENGINE_INCOMPATIBLE", [b["code"] for b in res["blockers"]])
        unreal.ENGINE.engine_version = "5.5.4-0+++UE5+Release-5.5"
        code, res = self.ued("gate")
        self.assertEqual(res["status"], "ready")
        self.assertTrue(any("predates 5.8" in w for w in res["warnings"]))


if __name__ == "__main__":
    unittest.main()
