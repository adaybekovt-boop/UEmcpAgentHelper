"""REAL integration tests: run `ued` as a subprocess against a running Unreal Editor.

Skipped unless UED_IT_PROJECT points to a throwaway project (.uproject or its dir).
They create content under /Game/UEDirectIT/ in that project; use a scratch project.

    UED_IT_PROJECT=D:/Scratch/ITProject python -m unittest tests.integration.test_real_editor -v

Optional:
    UED_IT_LAUNCH=1     let the test launch the editor (`ued up`) if it is closed
    UED_IT_TRANSPORT=mcp|remote
"""

import json
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UED = os.path.join(ROOT, "skills", "ue-direct", "scripts", "ued.py")
PROJECT = os.environ.get("UED_IT_PROJECT")


def ued(*args, stdin=None, timeout=1200):
    cmd = [sys.executable, UED, "--project", PROJECT]
    if os.environ.get("UED_IT_TRANSPORT"):
        cmd += ["--transport", os.environ["UED_IT_TRANSPORT"]]
    p = subprocess.run(cmd + list(args), input=stdin, capture_output=True, text=True, timeout=timeout)
    line = (p.stdout.strip().splitlines() or ["{}"])[-1]
    return p.returncode, json.loads(line)


@unittest.skipUnless(PROJECT, "set UED_IT_PROJECT to run real-editor integration tests")
class RealEditor(unittest.TestCase):
    level = "/Game/UEDirectIT/IT_%d" % int(time.time())

    @classmethod
    def setUpClass(cls):
        code, res = ued("up" if os.environ.get("UED_IT_LAUNCH") else "gate")
        if res.get("status") != "ready":
            raise unittest.SkipTest("editor not ready: %s" % json.dumps(res)[:500])

    def test_00_api_selftest_and_doctor(self):
        code, res = ued("doctor")
        self.assertEqual(res["status"], "ready", res)
        self.assertNotIn("API_MISSING", json.dumps(res))

    def test_10_create_populate_save_reload_shot(self):
        code, res = ued("task", "start", "--kind", "level", "--level", self.level, "--min-actors", "100")
        self.assertEqual(code, 0, res)
        code, res = ued("level", "new", self.level)
        self.assertEqual(code, 0, res)
        plan = {"ops": [
            {"op": "grid", "id": "it_grid", "asset": "/Engine/BasicShapes/Cube", "count": [10, 10], "step": [250, 250],
             "folder": "UEDirectIT"},
            {"class": "DirectionalLight", "id": "it_sun", "rot": [0, -45, 30]},
            {"class": "SkyLight", "id": "it_sky"},
            {"class": "PlayerStart", "id": "it_start", "loc": [1100, 1100, 150]}]}
        code, res = ued("batch", "-", "--save", "--verify", "--reload", stdin=json.dumps(plan))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["created"], 103)
        self.assertTrue(res["saved"] and res["verified"], res)
        # idempotent re-run
        code, res = ued("batch", "-", stdin=json.dumps(plan))
        self.assertEqual((res["created"], res.get("unchanged")), (0, 103), res)
        code, shot = ued("shot")
        self.assertEqual(code, 0, shot)
        self.assertTrue(os.path.isfile(shot["file"]))

    def test_20_reopen_matches(self):
        code, res = ued("level", "open", self.level, "--save-dirty")
        if code != 0:
            self.skipTest("level from test_10 not available: %s" % res)
        code, res = ued("actors", "id:it_grid#*", "--limit", "1")
        self.assertEqual(res["matched"], 100, res)

    def test_30_mass_placement_1000(self):
        plan = {"ops": [{"op": "scatter", "id": "it_mass", "asset": "/Engine/BasicShapes/Sphere", "count": 1000,
                         "area": {"min": [-20000, -20000], "max": [20000, 20000]}, "min_dist": 300, "seed": 1,
                         "z": 50, "folder": "UEDirectIT/Mass"}]}
        t0 = time.time()
        code, res = ued("batch", "-", "--save", stdin=json.dumps(plan))
        self.assertEqual(code, 0, res)
        self.assertEqual(res["created"] + res.get("unchanged", 0), 1000)
        print("\n1000-actor batch: %.1fs, %d chunks" % (time.time() - t0, res["chunks"]))


if __name__ == "__main__":
    unittest.main()
