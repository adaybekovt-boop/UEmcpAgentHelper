"""Editor log reading: offsets + compact error extraction (full logs stay on disk)."""

import os
import re

ERROR_PATTERNS = [
    ("missing_reference", re.compile(r"(Failed to load|Can't find file|Missing cached shader|"
                                     r"LogLinker: Warning: .*(Failed|missing))", re.I)),
    ("blueprint_runtime", re.compile(r"(Blueprint Runtime Error|Accessed None|Script Msg:)", re.I)),
    ("python", re.compile(r"LogPython: Error", re.I)),
    ("error", re.compile(r"\bError:", re.I)),
]


def project_log(project):
    return os.path.join(project.dir, "Saved", "Logs", project.name + ".log")


def offset(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_since(path, start, max_bytes=4 * 1024 * 1024):
    try:
        size = os.path.getsize(path)
        if size < start:  # log rotated (new editor session)
            start = 0
        with open(path, "rb") as fh:
            fh.seek(max(start, size - max_bytes))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def classify(text, limit=8):
    counts, examples = {}, []
    for line in text.splitlines():
        for kind, pat in ERROR_PATTERNS:
            if pat.search(line):
                counts[kind] = counts.get(kind, 0) + 1
                if len(examples) < limit:
                    examples.append(line.strip()[-240:])
                break
    return {"counts": counts, "examples": examples}


def tail_errors(path, lines=400):
    text = read_since(path, max(0, offset(path) - 256 * 1024))
    return classify("\n".join(text.splitlines()[-lines:]))
