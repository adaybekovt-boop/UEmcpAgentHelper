"""Minimal MCP Streamable-HTTP client for Epic's Unreal MCP plugin (UE 5.8+).

Stdlib only. Speaks JSON-RPC 2.0 over POST, accepts JSON or SSE responses,
tracks ``Mcp-Session-Id``. Locates the Python execution tool at runtime:

* tool search ON (default): ``tools/list`` returns meta-tools (``list_toolsets``,
  ``describe_tools``, ``call_tool``...). The python tool is reached through
  ``call_tool``; argument names are read from the live schemas, not hardcoded.
* tool search OFF: the python tool is listed directly and called directly.

The discovered mapping is cached on disk so later invocations skip discovery.
"""

import http.client
import json
import os
import re
import urllib.error
import urllib.request

from .. import __version__
from ..util import read_json, write_json
from .base import Transport, TransportError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
PY_TOOL_CANDIDATES = ("EditorToolset.PythonScriptingTools.execute_python", "PythonScriptingTools.execute_python",
                      "execute_python")
CODE_PARAM_GUESSES = ("code", "script", "python", "source", "command", "python_code")
INSTANCE_META_KEY = "com.epicgames.unrealengine/instance"


def _opener():
    # Never route loopback editor traffic through an HTTP(S)_PROXY.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _parse_sse(text):
    msgs = []
    data = []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            try:
                msgs.append(json.loads("\n".join(data)))
            except ValueError:
                pass
            data = []
    return msgs


def _content_text(result):
    parts = []
    for c in (result or {}).get("content", []) or []:
        if c.get("type") == "text":
            parts.append(c.get("text", ""))
    if not parts and result and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"]))
    return "\n".join(parts)


def _find_schema(obj, tool_name=None):
    """Search a describe_* response for an input schema object."""
    if isinstance(obj, dict):
        for key in ("inputSchema", "input_schema", "parameters", "schema"):
            s = obj.get(key)
            if isinstance(s, dict) and isinstance(s.get("properties"), dict):
                name = obj.get("name") or obj.get("tool") or ""
                if tool_name is None or not name or name.endswith(tool_name.split(".")[-1]):
                    return s
        for v in obj.values():
            r = _find_schema(v, tool_name)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_schema(v, tool_name)
            if r:
                return r
    return None


def _code_param(schema):
    props = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    strings = [k for k, v in props.items() if isinstance(v, dict) and v.get("type") == "string"]
    for k in required:
        if k in strings:
            return k
    for g in CODE_PARAM_GUESSES:
        if g in strings:
            return g
    return strings[0] if strings else None


class McpHttpTransport(Transport):
    name = "mcp"

    def __init__(self, url, cache_path=None, timeout=110.0):
        self.url = url
        self.cache_path = cache_path
        self.timeout = timeout
        self.session_id = None
        self.protocol = None
        self.instance = None
        self._rid = 0
        self._tools = None

    # ------------------------------------------------------------ jsonrpc
    def _post(self, payload, timeout, expect_reply=True):
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            resp = _opener().open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            if exc.code == 404 and self.session_id:
                self.session_id = None  # session expired; caller may retry
            raise TransportError("MCP HTTP %s: %s" % (exc.code, detail), code="TRANSPORT_HTTP_%d" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError("cannot reach Unreal MCP at %s: %s" % (self.url, getattr(exc, "reason", exc)))
        try:
            with resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except (OSError, http.client.HTTPException) as exc:  # timeout / connection lost mid-reply
            raise TransportError("reply from %s was cut off: %s" % (self.url, exc), code="TRANSPORT_BAD_REPLY")
        if not expect_reply:
            return None
        try:
            msgs = _parse_sse(raw) if "text/event-stream" in ctype else ([json.loads(raw)] if raw.strip() else [])
        except ValueError:
            raise TransportError("%s did not answer with MCP JSON (another service on this port?)" % self.url,
                                 code="TRANSPORT_NOT_MCP", retryable=False)
        msgs = [m for m in msgs if isinstance(m, dict)]
        for m in msgs:
            if m.get("id") == payload.get("id"):
                if "error" in m:
                    err = m["error"]
                    raise TransportError("MCP error %s: %s" % (err.get("code"), err.get("message")),
                                         code="TRANSPORT_RPC_ERROR", retryable=False)
                return m.get("result")
        raise TransportError("no JSON-RPC reply for id %s" % payload.get("id"), code="TRANSPORT_BAD_REPLY")

    def _call(self, method, params=None, timeout=None):
        self._rid += 1
        payload = {"jsonrpc": "2.0", "id": self._rid, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload, timeout or self.timeout)

    def _notify(self, method):
        self._post({"jsonrpc": "2.0", "method": method}, 10.0, expect_reply=False)

    def connect(self, timeout=5.0):
        if self.session_id:
            return
        last = None
        for ver in PROTOCOL_VERSIONS:
            try:
                res = self._call("initialize", {"protocolVersion": ver, "capabilities": {},
                                                "clientInfo": {"name": "ue-direct", "version": __version__}},
                                 timeout=timeout)
            except TransportError as exc:
                last = exc
                if exc.code == "TRANSPORT_RPC_ERROR":
                    continue
                raise
            self.protocol = res.get("protocolVersion", ver)
            self.instance = (res.get("_meta") or {}).get(INSTANCE_META_KEY) or {}
            self.server = res.get("serverInfo") or {}
            try:
                self._notify("notifications/initialized")
            except TransportError:
                pass
            return
        raise last or TransportError("MCP initialize failed")

    def close(self):
        if not self.session_id:
            return
        req = urllib.request.Request(self.url, method="DELETE", headers={"Mcp-Session-Id": self.session_id})
        try:
            _opener().open(req, timeout=2.0).close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        self.session_id = None

    # ---------------------------------------------------------- discovery
    def _tool_plan(self):
        if self._tools:
            return self._tools
        cache = read_json(self.cache_path, {}) if self.cache_path else {}
        if cache.get("url") == self.url and cache.get("plan"):
            self._tools = cache["plan"]
            return self._tools
        listed = self._call("tools/list", {}).get("tools", [])
        by_name = {t.get("name"): t for t in listed}
        plan = None
        for name, tool in by_name.items():
            if name and name.split(".")[-1] == "execute_python":
                plan = {"mode": "direct", "tool": name, "code_param": _code_param(tool.get("inputSchema"))}
                break
        if plan is None and "call_tool" in by_name:
            plan = self._plan_via_meta(by_name)
        if plan is None or not plan.get("code_param"):
            raise TransportError("Unreal MCP is up but no Python execution tool was found",
                                 code="TRANSPORT_NO_PYTHON_TOOL", retryable=False,
                                 hint="enable the Python Editor Script Plugin and the Unreal MCP 'All Toolsets' plugin")
        self._tools = plan
        if self.cache_path:
            write_json(self.cache_path, {"url": self.url, "plan": plan})
        return plan

    def _plan_via_meta(self, by_name):
        call_schema = by_name["call_tool"].get("inputSchema") or {}
        props = call_schema.get("properties") or {}
        name_key = next((k for k, v in props.items() if v.get("type") == "string" and
                         ("tool" in k or "name" in k)), None)
        args_key = next((k for k, v in props.items() if v.get("type") == "object"), None)
        if not name_key or not args_key:
            return None
        desc = by_name.get("describe_tools")
        for qualified in PY_TOOL_CANDIDATES:
            schema = None
            if desc:
                dprops = (desc.get("inputSchema") or {}).get("properties") or {}
                arr = next((k for k, v in dprops.items() if v.get("type") == "array"), None)
                if arr:
                    args = {arr: [qualified]}
                    for k, v in dprops.items():
                        if v.get("type") == "boolean" and "schema" in k:
                            args[k] = True
                    try:
                        res = self._call("tools/call", {"name": "describe_tools", "arguments": args})
                    except TransportError:
                        continue
                    if res.get("isError"):
                        continue
                    payload = res.get("structuredContent")
                    if payload is None:
                        try:
                            payload = json.loads(_content_text(res))
                        except ValueError:
                            payload = None
                    schema = _find_schema(payload, qualified)
            if schema is not None or not desc:
                return {"mode": "meta", "tool": qualified, "name_key": name_key, "args_key": args_key,
                        "code_param": _code_param(schema) if schema else "code"}
        return None

    # ---------------------------------------------------------- transport
    def probe(self, timeout=2.0):
        try:
            self.connect(timeout=timeout)
        except Exception:  # noqa: BLE001 - any failure means "no usable Unreal MCP here"
            return None
        info = {"transport": "mcp", "url": self.url, "server": getattr(self, "server", {})}
        inst = self.instance or {}
        info["instance"] = inst
        for k, v in _flatten(inst):
            lk = k.lower()
            if "project" in lk and isinstance(v, str) and v.lower().endswith(".uproject"):
                info["project_file"] = v
            elif lk.endswith("engineversion") or lk.endswith("engine_version") or lk == "version":
                info.setdefault("engine_version", str(v))
            elif lk in ("pid", "processid", "process_id"):
                info["pid"] = v
        return info

    def execute(self, code, timeout=110.0):
        self.connect()
        plan = self._tool_plan()
        if plan["mode"] == "direct":
            params = {"name": plan["tool"], "arguments": {plan["code_param"]: code}}
        else:
            params = {"name": "call_tool", "arguments": {plan["name_key"]: plan["tool"],
                                                          plan["args_key"]: {plan["code_param"]: code}}}
        try:
            res = self._call("tools/call", params, timeout=timeout)
        except TransportError as exc:
            if exc.code == "TRANSPORT_RPC_ERROR" and self.cache_path:
                # the cached plan may be stale (plugin updated); rediscover next time
                try:
                    os.remove(self.cache_path)
                except OSError:
                    pass
            raise
        text = _content_text(res)
        if res.get("isError"):
            raise TransportError("editor python error: %s" % text[-1500:], code="EDITOR_EXEC_ERROR", retryable=False)
        return text

    def describe(self):
        return {"transport": "mcp", "url": self.url}


def _flatten(d, prefix=""):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _flatten(v, prefix + str(k) if not prefix else prefix + "." + str(k))
    else:
        yield prefix.split(".")[-1], d


def discover_urls(project_dir, explicit=None):
    """Candidate Unreal MCP URLs: explicit, project .mcp.json / .codex/config.toml, default port."""
    urls = []
    if explicit:
        urls.append(explicit)
    mcp_json = read_json(os.path.join(project_dir, ".mcp.json"), {}) or {}
    for name, srv in (mcp_json.get("mcpServers") or {}).items():
        url = (srv or {}).get("url")
        if url and "unreal" in name.lower():
            urls.append(url)
    toml = os.path.join(project_dir, ".codex", "config.toml")
    try:
        with open(toml, "r", encoding="utf-8") as fh:
            text = fh.read()
        m = re.search(r"\[mcp_servers\.[^\]]*unreal[^\]]*\][^\[]*?url\s*=\s*\"([^\"]+)\"", text, re.I | re.S)
        if m:
            urls.append(m.group(1))
    except OSError:
        pass
    urls.append("http://127.0.0.1:8000/mcp")
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out
