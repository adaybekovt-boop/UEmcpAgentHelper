"""Editor session: channel selection, request/response files, retries, journal.

Every editor command is: write request JSON -> send a 6-line bootstrap snippet
through the transport -> editor writes ``<request>.out.json``. The snippet is
the same size whatever the payload, so 1 op and 1000 ops cost the agent the
same number of tokens.
"""

import os
import time

from . import config as config_mod
from .transports.base import TransportError
from .transports.mcp_http import McpHttpTransport, discover_urls
from .transports.py_remote import PyRemoteTransport
from .util import UedHostError, append_jsonl, new_id, now, read_json, state_dir, write_json

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RelayPending(UedHostError):
    def __init__(self, req_id, snippet, relay_file):
        UedHostError.__init__(self, "RELAY_PENDING", "run the snippet with your editor Python MCP tool, "
                              "then: ued result %s" % req_id,
                              data={"request": req_id, "snippet": snippet, "snippet_file": relay_file})


def bootstrap_snippet(request_path):
    # Only top-level `import` statements (Epic's execute_python rejects others);
    # the package itself is imported via importlib after sys.path is set.
    return ("import sys\n"
            "import importlib\n"
            "_ued_p = %r\n"
            "if _ued_p not in sys.path: sys.path.insert(0, _ued_p)\n"
            "_ued = importlib.import_module('uedirect_editor').ensure_fresh()\n"
            "_ued.run_request(%r)\n") % (SCRIPTS_DIR, request_path)


class Session(object):
    def __init__(self, project, cfg=None, transport_override=None):
        self.project = project
        self.cfg = cfg or config_mod.load(project.dir)
        if transport_override:
            self.cfg["transport"] = transport_override
        self.dir = state_dir(project.dir)
        self._transport = None

    # ------------------------------------------------------------- paths
    def path(self, *parts):
        return os.path.join(self.dir, *parts)

    def journal(self, kind, **data):
        rec = {"t": now(), "src": "host", "kind": kind, "task": self.task_id()}
        rec.update(data)
        append_jsonl(self.path("evidence.jsonl"), rec)

    def task_id(self):
        t = read_json(self.path("task.json"), {}) or {}
        return t.get("id") if t.get("status") == "active" else None

    def touched(self):
        return list((read_json(self.path("touched.json"), {}) or {}).get("packages", []))

    def set_touched(self, packages):
        write_json(self.path("touched.json"), {"packages": sorted(set(packages)), "t": now()})

    # --------------------------------------------------------- transports
    def _make(self, kind, info=None):
        if kind == "mcp":
            url = (info or {}).get("url") or self.cfg.get("mcp_url") or discover_urls(self.project.dir)[0]
            return McpHttpTransport(url, cache_path=self.path("mcp_plan.json"), timeout=self.cfg["call_timeout"])
        if kind == "remote":
            s = self.project.python_remote_settings()
            return PyRemoteTransport(group=s["group"], bind=s["bind"], ttl=s["ttl"],
                                     project_name=self.project.name, project_root=self.project.dir)
        raise UedHostError("BAD_TRANSPORT", "unknown transport %s" % kind)

    def select_transport(self, force_probe=False):
        """Pick a working channel. Cached for channel_ttl seconds (no re-probing per call)."""
        if self._transport is not None and not force_probe:
            return self._transport
        mode = self.cfg["transport"]
        if mode == "relay":
            return None
        cached = read_json(self.path("channel.json"), {}) or {}
        if not force_probe and cached.get("kind") and now() - cached.get("t", 0) < self.cfg["channel_ttl"] \
                and mode in ("auto", cached["kind"]):
            self._transport = self._make(cached["kind"], cached)
            return self._transport
        tried = []
        if mode in ("auto", "mcp"):
            urls = [self.cfg["mcp_url"]] if self.cfg.get("mcp_url") else discover_urls(self.project.dir)
            for url in urls:
                t = McpHttpTransport(url, cache_path=self.path("mcp_plan.json"), timeout=self.cfg["call_timeout"])
                info = t.probe(timeout=2.0)
                tried.append("mcp:" + url)
                if info:
                    write_json(self.path("channel.json"), {"kind": "mcp", "url": url, "t": now(), "info": info})
                    self._transport = t
                    return t
        if mode in ("auto", "remote"):
            t = self._make("remote")
            info = t.probe(timeout=1.5)
            tried.append("remote:%s:%s" % t.group)
            if info and t.remote:
                write_json(self.path("channel.json"), {"kind": "remote", "t": now(), "info": info})
                self._transport = t
                return t
        try:
            os.remove(self.path("channel.json"))
        except OSError:
            pass
        raise TransportError("no editor channel answered (%s)" % ", ".join(tried), code="NO_EDITOR_CHANNEL",
                             hint="run `ued up` to launch/attach the editor, or `ued doctor` for diagnosis")

    def probe_info(self):
        return (read_json(self.path("channel.json"), {}) or {}).get("info")

    # ------------------------------------------------------------ request
    def _write_request(self, cmd, args, expect_level=None):
        rid = new_id()
        req = {"id": rid, "cmd": cmd, "args": args or {}, "state_dir": self.dir, "task_id": self.task_id(),
               "touched": self.touched(), "expect": {"project": self.project.uproject}}
        if expect_level:
            req["expect"]["level"] = expect_level
        path = self.path("requests", rid + ".json")
        write_json(path, req)
        return rid, path

    def request(self, cmd, args=None, expect_level=None, timeout=None, retries=2):
        rid, path = self._write_request(cmd, args, expect_level)
        return self._run(rid, path, timeout or self.cfg["call_timeout"], retries)

    def _run(self, rid, path, timeout, retries):
        snippet = bootstrap_snippet(path)
        out_path = path + ".out.json"
        if self.cfg["transport"] == "relay":
            relay_file = self.path("requests", rid + ".py")
            with open(relay_file, "w", encoding="utf-8") as fh:
                fh.write(snippet)
            raise RelayPending(rid, snippet, relay_file)
        attempt = 0
        last = None
        while attempt <= retries:
            attempt += 1
            try:
                t = self.select_transport(force_probe=attempt > 1)
                t.execute(snippet, timeout=timeout)
                last = None
            except TransportError as exc:
                last = exc
                # The command may have finished even though the reply was lost.
                if _wait_file(out_path, 3.0):
                    break
                if not exc.retryable:
                    break
                self._transport = None
                time.sleep(min(2.0 * attempt, 5.0))
                continue
            break
        resp = read_json(out_path) if _wait_file(out_path, 5.0) else None
        if resp is None:
            if last is not None:
                raise last
            raise UedHostError("NO_RESPONSE", "editor ran the snippet but wrote no response for %s" % rid,
                               hint="check the editor Output Log (LogPython)")
        return self._finish(resp)

    def result(self, rid):
        """Collect the response for a relayed request."""
        out_path = self.path("requests", rid + ".json.out.json")
        resp = read_json(out_path)
        if resp is None:
            raise UedHostError("NO_RESPONSE", "no response file for %s yet" % rid,
                               hint="run the relay snippet in the editor first")
        return self._finish(resp)

    def _finish(self, resp):
        st = resp.get("state") or {}
        if "touched" in st:
            self.set_touched(st["touched"])
        if not resp.get("ok"):
            err = resp.get("error") or {}
            raise UedHostError(err.get("code", "EDITOR_ERROR"), err.get("message", "editor error"),
                               data={"trace": err.get("trace")} if err.get("trace") else None)
        return resp


def _wait_file(path, seconds):
    deadline = time.time() + seconds
    while True:
        if os.path.isfile(path):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.1)
