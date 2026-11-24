"""Host-side persistence checks: saved files are real Unreal packages.

A ``.umap`` existing on disk proves nothing (anyone can write one). These
checks confirm each saved package maps to a file that (a) exists, (b) was
written by this save, and (c) starts with the Unreal package file tag. The
in-editor reload check (``ued verify --reload``) then confirms the content.
"""

import os

PACKAGE_FILE_TAG = b"\xc1\x83\x2a\x9e"          # 0x9E2A83C1, little-endian
PACKAGE_FILE_TAG_SWAPPED = b"\x9e\x2a\x83\xc1"


def is_ue_package(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return False
    return head in (PACKAGE_FILE_TAG, PACKAGE_FILE_TAG_SWAPPED)


def verify_saved_files(project, packages, since, level=None, created_assets=()):
    """Check saved package files. External-actor packages may legitimately be
    gone (the actor was deleted); levels and created assets must exist."""
    must_exist = set(created_assets)
    if level:
        must_exist.add(level)
    ok, problems, deleted, unmapped = 0, [], 0, 0
    for pkg in packages:
        f = project.package_to_file(pkg)
        if f is None:
            unmapped += 1
            continue
        if not os.path.isfile(f):
            if "/__ExternalActors__/" in pkg or "/__ExternalObjects__/" in pkg:
                if pkg not in must_exist:
                    deleted += 1
                    continue
            problems.append("missing file for %s" % pkg)
            continue
        if not is_ue_package(f):
            problems.append("not an Unreal package (bad file tag): %s" % f)
            continue
        if os.path.getmtime(f) + 2.0 < since:
            problems.append("file not rewritten by this save: %s" % f)
            continue
        ok += 1
    return {"ok": not problems, "files_ok": ok, "deleted": deleted, "unmapped": unmapped, "problems": problems[:10]}


def scan_fake_assets(project, limit=20):
    """Find .umap/.uasset files that are not Unreal packages (e.g. JSON written by an agent)."""
    roots = [project.content_dir] + list(project.plugin_content_dirs().values())
    bad, checked = [], 0
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith((".umap", ".uasset")):
                    checked += 1
                    p = os.path.join(dirpath, f)
                    if not is_ue_package(p):
                        bad.append(os.path.relpath(p, project.dir))
    return {"checked": checked, "fake": len(bad), "examples": bad[:limit]}
