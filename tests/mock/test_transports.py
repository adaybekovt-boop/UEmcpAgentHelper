"""Transport tests against local mock servers.

* MockUnrealMcp: a Streamable-HTTP JSON-RPC server shaped like Epic's Unreal
  MCP with tool search ON (meta-tools only) or OFF (python tool listed). The
  argument names are intentionally NOT the ones UE-Direct guesses first, to
  prove the client reads live schemas. Replies use SSE.
* MockRemoteNode: a UDP-multicast + TCP node speaking the Python remote
  execution message format.

These validate UE-Direct's protocol handling against our understanding of the
protocols. They are not a substitute for running against a real editor.
"""

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import helpers  # noqa: E402

PY_TOOL = "EditorToolset.PythonScriptingTools.execute_python"


def _exec(code):
    exec(compile(code, "<snippet>", "exec"), {"__name__": "__ued__"})
    return "ok"


class MockUnrealMcp(object):
    def __init__(self, project_file, tool_search=True, sse=True):
        self.project_file = project_file
        self.tool_search = tool_search
        self.sse = sse
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_DELETE(self):
                self.send_response(200)
                self.end_headers()

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = outer.handle(body["method"], body.get("params") or {})
                msg = {"jsonrpc": "2.0", "id": body["id"], "result": result}
                self.send_response(200)
                self.send_header("Mcp-Session-Id", "sess-1")
                if outer.sse:
                    data = ("event: message\ndata: %s\n\n" % json.dumps(msg)).encode()
                    self.send_header("Content-Type", "text/event-stream")
                else:
                    data = json.dumps(msg).encode()
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def handle(self, method, params):
        self.calls.append(method if method != "tools/call" else "call:" + params["name"])
        if method == "initialize":
            return {"protocolVersion": params["protocolVersion"], "serverInfo": {"name": "unreal-mcp"},
                    "capabilities": {"tools": {}},
                    "_meta": {"com.epicgames.unrealengine/instance": {
                        "projectFilePath": self.project_file, "engineVersion": "5.8.1", "pid": 4242}}}
        if method == "tools/list":
            if not self.tool_search:
                return {"tools": [{"name": PY_TOOL, "inputSchema": {
                    "type": "object", "properties": {"python_source": {"type": "string"}},
                    "required": ["python_source"]}}]}
            return {"tools": [
                {"name": "list_toolsets", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "describe_toolsets", "inputSchema": {"type": "object", "properties": {
                    "toolsets": {"type": "array"}, "include_schemas": {"type": "boolean"}}}},
                {"name": "describe_tools", "inputSchema": {"type": "object", "properties": {
                    "names": {"type": "array"}, "include_schemas": {"type": "boolean"}}}},
                {"name": "call_tool", "inputSchema": {"type": "object", "properties": {
                    "tool_name": {"type": "string"}, "tool_arguments": {"type": "object"}}}}]}
        if method == "tools/call":
            name, args = params["name"], params.get("arguments") or {}
            if name == "describe_tools":
                tools = [{"name": n, "inputSchema": {"type": "object", "properties": {"script": {"type": "string"}},
                                                     "required": ["script"]}}
                         for n in args.get("names", []) if n == PY_TOOL]
                if not tools:
                    return {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}
                return {"content": [{"type": "text", "text": json.dumps({"tools": tools})}]}
            if name == "call_tool":
                assert args["tool_name"] == PY_TOOL, args
                return {"content": [{"type": "text", "text": _exec(args["tool_arguments"]["script"])}]}
            if name == PY_TOOL:
                return {"content": [{"type": "text", "text": _exec(args["python_source"])}]}
        raise AssertionError("unexpected %s %s" % (method, params))


class McpTransportTests(helpers.ProjectTestCase):
    def _use_real_selection(self, url):
        helpers.session_mod.Session.select_transport = self._orig_select
        os.environ["UED_TRANSPORT"] = "mcp"
        os.environ["UED_MCP_URL"] = url

    def test_tool_search_mode_with_unknown_arg_names(self):
        srv = MockUnrealMcp(os.path.join(self.project_dir, "Demo.uproject"))
        try:
            self._use_real_selection(srv.url)
            code, res = self.ued("inspect")
            self.assertEqual(code, 0, res)
            self.assertEqual(res["level"], "/Game/Maps/Start")
            self.assertIn("call:describe_tools", srv.calls)
            # second invocation reuses the cached tool plan: no discovery calls
            srv.calls.clear()
            self.ued("inspect", expect_code=0)
            self.assertNotIn("call:describe_tools", srv.calls)
            self.assertNotIn("tools/list", srv.calls)
        finally:
            srv.close()

    def test_direct_mode_json_replies(self):
        srv = MockUnrealMcp(os.path.join(self.project_dir, "Demo.uproject"), tool_search=False, sse=False)
        try:
            self._use_real_selection(srv.url)
            code, res = self.ued("batch", "-", stdin=self.plan([{"asset": "/Game/Props/SM_Rock", "id": "r"}]))
            self.assertEqual(code, 0, res)
            self.assertEqual(res["created"], 1)
            self.assertIn("call:" + PY_TOOL, srv.calls)
        finally:
            srv.close()

    def test_probe_reads_instance_metadata(self):
        from uedirect.transports.mcp_http import McpHttpTransport
        srv = MockUnrealMcp("/x/Demo.uproject")
        try:
            info = McpHttpTransport(srv.url).probe()
            self.assertEqual(info["project_file"], "/x/Demo.uproject")
            self.assertEqual(info["engine_version"], "5.8.1")
            self.assertEqual(info["pid"], 4242)
        finally:
            srv.close()


# ----------------------------------------------------------------- remote exec

GROUP = ("239.0.0.1", 6799)  # non-default port so a real editor is never contacted


def multicast_loopback_available():
    try:
        from uedirect.transports.py_remote import PyRemoteTransport
        t = PyRemoteTransport(group=GROUP)
        rx = t._udp()
        rx.settimeout(0.5)
        tx = t._udp()
        tx.sendto(b"probe", GROUP)
        data, _ = rx.recvfrom(64)
        rx.close()
        tx.close()
        return data == b"probe"
    except OSError:
        return False


class MockRemoteNode(threading.Thread):
    def __init__(self, project_root, project_name="Demo"):
        threading.Thread.__init__(self, daemon=True)
        from uedirect.transports.py_remote import PyRemoteTransport
        self.sock = PyRemoteTransport(group=GROUP)._udp()
        self.sock.settimeout(0.2)
        self.node_id = "editor-node"
        self.data = {"project_root": project_root, "project_name": project_name, "engine_version": "5.8.1",
                     "machine": "test", "user": "test"}
        self.running = True
        self.commands = 0

    def send(self, mtype, dest, data=None):
        m = {"version": 1, "magic": "ue_py", "type": mtype, "source": self.node_id, "dest": dest}
        if data is not None:
            m["data"] = data
        self.sock.sendto(json.dumps(m).encode(), GROUP)

    def run(self):
        while self.running:
            try:
                raw, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            m = json.loads(raw)
            if m.get("source") == self.node_id:
                continue
            if m["type"] == "ping":
                self.send("pong", m["source"], self.data)
            elif m["type"] == "open_connection" and m.get("dest") == self.node_id:
                d = m["data"]
                c = socket.create_connection((d["command_ip"], d["command_port"]))
                buf = b""
                while True:
                    buf += c.recv(65536)
                    try:
                        cmd = json.loads(buf)
                        break
                    except ValueError:
                        continue
                self.commands += 1
                try:
                    _exec(cmd["data"]["command"])
                    res = {"success": True, "result": "None", "output": [{"type": "Info", "output": "done"}]}
                except Exception as exc:  # noqa: BLE001
                    res = {"success": False, "result": str(exc), "output": []}
                reply = {"version": 1, "magic": "ue_py", "type": "command_result", "source": self.node_id,
                         "dest": m["source"], "data": res}
                c.sendall(json.dumps(reply).encode())
                c.close()

    def stop(self):
        self.running = False
        self.sock.close()


@unittest.skipUnless(multicast_loopback_available(), "UDP multicast loopback not available in this environment")
class RemoteExecTests(helpers.ProjectTestCase):
    def test_discovery_and_command(self):
        node = MockRemoteNode(self.project_dir)
        node.start()
        try:
            helpers.session_mod.Session.select_transport = self._orig_select
            os.environ["UED_TRANSPORT"] = "remote"
            ini = os.path.join(self.project_dir, "Config")
            os.makedirs(ini)
            with open(os.path.join(ini, "DefaultEngine.ini"), "w") as fh:
                fh.write("[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nbRemoteExecution=True\n"
                         "RemoteExecutionMulticastGroupEndpoint=%s:%d\n" % GROUP)
            code, res = self.ued("inspect")
            self.assertEqual(code, 0, res)
            self.assertEqual(res["level"], "/Game/Maps/Start")
            self.assertGreaterEqual(node.commands, 1)
        finally:
            node.stop()


if __name__ == "__main__":
    unittest.main()
