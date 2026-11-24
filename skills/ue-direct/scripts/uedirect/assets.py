"""Local asset cache: one Asset Registry scan, then cheap local queries.

Invalidation:
* content fingerprint (file count, total size, newest mtime of .uasset/.umap
  under Content/ and plugin Content/) - catches imports, deletes and edits
  made outside UE-Direct (other tools, source control syncs, the user);
* project path + engine association change;
* assets created by UE-Direct batches are merged in place (no rescan).
"""

import difflib
import os
import re
import time

from .util import UedHostError, now, read_json, write_json

CATEGORIES = [
    ("level", ("World",)),
    ("mesh", ("StaticMesh",)),
    ("skeletal", ("SkeletalMesh", "Skeleton", "PhysicsAsset")),
    ("material", ("Material", "MaterialInstanceConstant", "MaterialFunction", "MaterialParameterCollection")),
    ("texture", ("Texture2D", "TextureCube", "TextureRenderTarget2D", "VolumeTexture")),
    ("blueprint", ("Blueprint", "WidgetBlueprint", "AnimBlueprint", "BlueprintGeneratedClass")),
    ("anim", ("AnimSequence", "AnimMontage", "BlendSpace", "BlendSpace1D", "PoseAsset", "IKRig", "IKRetargeter")),
    ("vfx", ("NiagaraSystem", "NiagaraEmitter", "ParticleSystem")),
    ("audio", ("SoundWave", "SoundCue", "MetaSoundSource", "SoundAttenuation")),
    ("data", ("DataTable", "CurveTable", "DataAsset", "PrimaryDataAsset", "CurveFloat", "StringTable")),
    ("level_data", ("LevelSequence", "PCGGraph", "FoliageType_InstancedStaticMesh", "LandscapeLayerInfoObject")),
]
_CAT = {cls: cat for cat, classes in CATEGORIES for cls in classes}


def category(cls):
    return _CAT.get(cls, "other")


def content_fingerprint(project):
    roots = [project.content_dir] + list(project.plugin_content_dirs().values())
    count = size = 0
    newest = 0.0
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.endswith((".uasset", ".umap")):
                    try:
                        st = os.stat(os.path.join(dirpath, f))
                    except OSError:
                        continue
                    count += 1
                    size += st.st_size
                    newest = max(newest, st.st_mtime)
    return {"files": count, "bytes": size, "newest": round(newest, 3)}


class AssetCache(object):
    def __init__(self, session):
        self.session = session
        self.path = session.path("asset_cache.json")
        self.data = read_json(self.path, None)

    def _key(self):
        p = self.session.project
        return {"uproject": p.uproject, "association": p.association}

    def status(self, check_fs=True):
        if not self.data or self.data.get("key") != self._key():
            return "missing" if not self.data else "stale:project"
        if check_fs:
            fp = content_fingerprint(self.session.project)
            if fp != self.data.get("fingerprint"):
                return "stale:content"
        return "fresh"

    def refresh(self, roots=None):
        t0 = time.time()
        resp = self.session.request("assets_scan", {"roots": roots or ["/Game"]}, timeout=300.0)
        res = resp["result"]
        if res.get("registry_loading"):
            raise UedHostError("EDITOR_NOT_READY", "asset registry still scanning; retry shortly")
        rows = [[p, c] for p, c in res["rows"]]
        self.data = {"key": self._key(), "fingerprint": content_fingerprint(self.session.project), "built": now(),
                     "roots": res.get("roots"), "rows": rows}
        write_json(self.path, self.data)
        return {"assets": len(rows), "ms": int((time.time() - t0) * 1000)}

    def ensure(self):
        st = self.status()
        if st != "fresh":
            info = self.refresh()
            info["reason"] = st
            return info
        return None

    def merge(self, rows):
        """Add assets created by UE-Direct and re-stamp the fingerprint."""
        if not self.data or not rows:
            return
        have = {r[0] for r in self.data["rows"]}
        for path, cls in rows:
            if path not in have:
                self.data["rows"].append([path, cls])
        self.data["fingerprint"] = content_fingerprint(self.session.project)
        write_json(self.path, self.data)

    def paths(self):
        return {r[0] for r in (self.data or {}).get("rows", [])}

    def find(self, query="", cls=None, cat=None, under=None, limit=15):
        terms = [t for t in re.split(r"[\s_\-/]+", query.lower()) if t]
        scored = []
        for path, c in (self.data or {}).get("rows", []):
            if cls and c.lower() != cls.lower():
                continue
            if cat and category(c) != cat:
                continue
            if under and not path.startswith(under):
                continue
            name = path.rsplit("/", 1)[-1].lower()
            low = path.lower()
            score = 0
            for t in terms:
                if t in name:
                    score += 3 if name.startswith(t) or ("_" + t) in name else 2
                elif t in low:
                    score += 1
                else:
                    score = -1
                    break
            if score >= 0:
                scored.append((-score, len(path), path, c))
        scored.sort()
        return {"matches": len(scored), "results": ["%s|%s" % (p, c) for _, _, p, c in scored[:limit]]}

    def suggest(self, missing_path, limit=3):
        """Closest existing assets for a wrong path (typos, wrong folder, wrong prefix)."""
        name = missing_path.split(".")[0].rsplit("/", 1)[-1]
        rows = (self.data or {}).get("rows", [])
        by_name = {}
        for path, _ in rows:
            by_name.setdefault(path.rsplit("/", 1)[-1].lower(), []).append(path)
        out = []
        for n in difflib.get_close_matches(name.lower(), list(by_name), n=limit, cutoff=0.6):
            out.extend(sorted(by_name[n], key=lambda p: (p.rsplit("/", 1)[0] != missing_path.rsplit("/", 1)[0], p)))
        if len(out) < limit:
            stem = re.sub(r"^(SM|M|MI|T|BP|SK|ABP|NS)_", "", name, flags=re.I)
            out.extend(r.split("|")[0] for r in self.find(stem.replace("_", " "), limit=limit)["results"])
        seen = []
        for p in out:
            if p not in seen:
                seen.append(p)
        return seen[:limit]
