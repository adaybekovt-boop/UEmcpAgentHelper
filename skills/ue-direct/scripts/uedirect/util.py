"""Small shared helpers: state directory, atomic JSON, compact output."""

import json
import os
import sys
import time


class UedHostError(Exception):
    """An error with a stable machine-readable code and a short human hint."""

    def __init__(self, code, message, hint=None, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.hint = hint
        self.data = data or {}

    def as_dict(self):
        d = {"status": "error", "code": self.code, "message": str(self)}
        if self.hint:
            d["hint"] = self.hint
        if self.data:
            d.update(self.data)
        return d


def state_dir(project_dir):
    """Per-project UE-Direct state lives in <Project>/Saved/UEDirect (Saved/ is
    conventionally git-ignored and owned by the editor)."""
    d = os.path.join(project_dir, "Saved", "UEDirect")
    for sub in ("", "requests", "shots", "last"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


def write_json(path, payload, pretty=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        if pretty:
            json.dump(payload, fh, indent=1, default=str)
        else:
            json.dump(payload, fh, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def append_jsonl(path, record):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def read_jsonl(path):
    out = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def emit(payload, pretty=False, stream=None):
    """Print one compact JSON document (the only thing the agent reads)."""
    stream = stream or sys.stdout
    if pretty:
        stream.write(json.dumps(payload, indent=1, default=str) + "\n")
    else:
        stream.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    stream.flush()


def now():
    return time.time()


def new_id(prefix="r"):
    return "%s%d%03d" % (prefix, int(time.time() * 1000), int.from_bytes(os.urandom(2), "big") % 1000)
