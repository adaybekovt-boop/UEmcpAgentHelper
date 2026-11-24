"""Client for PythonScriptPlugin "remote execution" (UE 4.26+ / 5.x).

Independent stdlib implementation of the documented wire behaviour:

* Discovery: JSON ``ping`` datagrams to a UDP multicast group (default
  239.0.0.1:6766, TTL 0, bound to 127.0.0.1); editors answer with ``pong``
  whose ``data`` carries engine/project info.
* Command channel: the client listens on a local TCP port and sends
  ``open_connection`` (data: command_ip, command_port); the editor connects
  back. ``command`` messages (data: command, unattended, exec_mode) are sent
  over TCP and answered with ``command_result`` (data: success, result, output).
* Messages carry ``version`` 1, ``magic`` "ue_py", ``source`` and ``dest`` node ids.

The editor must have "Enable Remote Execution" on (Project Settings > Python),
or be launched by ``ued up``, which passes it as a command-line ini override.
"""

import json
import socket
import sys
import time
import uuid

from .base import Transport, TransportError

MAGIC = "ue_py"
VERSION = 1
BUFFER = 2 * 1024 * 1024


class PyRemoteTransport(Transport):
    name = "remote"

    def __init__(self, group=("239.0.0.1", 6766), bind="127.0.0.1", ttl=0, project_name=None,
                 project_root=None):
        self.group = (group[0], int(group[1]))
        self.bind = bind
        self.ttl = int(ttl)
        self.project_name = project_name
        self.project_root = project_root
        self.node_id = str(uuid.uuid4())
        self.remote = None
        self.remote_data = {}

    def _msg(self, mtype, dest=None, data=None):
        m = {"version": VERSION, "magic": MAGIC, "source": self.node_id, "type": mtype}
        if dest:
            m["dest"] = dest
        if data is not None:
            m["data"] = data
        return json.dumps(m).encode("utf-8")

    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.bind))
        # Windows receives multicast on a unicast bind; POSIX needs the wildcard address.
        s.bind((self.bind if sys.platform == "win32" else "", self.group[1]))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(self.group[0]) + socket.inet_aton(self.bind))
        return s

    def discover(self, timeout=1.5):
        """Ping and return a list of (node_id, data) for answering editors."""
        try:
            s = self._udp()
        except OSError as exc:
            raise TransportError("cannot open multicast socket %s:%s: %s" % (self.group[0], self.group[1], exc))
        found = {}
        try:
            s.settimeout(0.25)
            deadline = time.time() + timeout
            next_ping = 0.0
            while time.time() < deadline:
                if time.time() >= next_ping:
                    s.sendto(self._msg("ping"), self.group)
                    next_ping = time.time() + 0.5
                try:
                    raw, _ = s.recvfrom(BUFFER)
                except socket.timeout:
                    if found:
                        break
                    continue
                try:
                    m = json.loads(raw.decode("utf-8"))
                except ValueError:
                    continue
                if m.get("magic") != MAGIC or m.get("source") == self.node_id or m.get("type") != "pong":
                    continue
                if m.get("dest") not in (None, self.node_id):
                    continue
                found[m["source"]] = m.get("data") or {}
        finally:
            s.close()
        return list(found.items())

    def _select(self, nodes):
        def norm(p):
            return (p or "").replace("\\", "/").rstrip("/").lower()
        for nid, data in nodes:
            if self.project_root and norm(data.get("project_root")) == norm(self.project_root):
                return nid, data
        for nid, data in nodes:
            if self.project_name and data.get("project_name") == self.project_name:
                return nid, data
        return (nodes[0] if len(nodes) == 1 else (None, None))

    def probe(self, timeout=1.5):
        try:
            nodes = self.discover(timeout)
        except TransportError:
            return None
        if not nodes:
            return None
        nid, data = self._select(nodes)
        info = {"transport": "remote", "editors": len(nodes)}
        if nid is None:
            info["ambiguous"] = [d.get("project_name") for _, d in nodes]
            return info
        self.remote, self.remote_data = nid, data
        info.update({"engine_version": data.get("engine_version"), "project_name": data.get("project_name"),
                     "project_root": data.get("project_root"), "machine": data.get("machine")})
        return info

    def execute(self, code, timeout=110.0):
        if not self.remote:
            info = self.probe()
            if not info or not self.remote:
                raise TransportError("no editor answered Python remote-execution discovery on %s:%s"
                                     % self.group, hint="enable Project Settings > Python > Enable Remote Execution")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5.0)
            port = listener.getsockname()[1]
            udp = self._udp()
            try:
                udp.sendto(self._msg("open_connection", self.remote, {"command_ip": "127.0.0.1",
                                                                     "command_port": port}), self.group)
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    raise TransportError("editor did not open the command connection")
                try:
                    conn.settimeout(timeout)
                    conn.sendall(self._msg("command", self.remote, {"command": code, "unattended": True,
                                                                    "exec_mode": "ExecuteFile"}))
                    reply = _recv_json(conn, timeout)
                finally:
                    conn.close()
                    udp.sendto(self._msg("close_connection", self.remote), self.group)
            finally:
                udp.close()
        except OSError as exc:
            raise TransportError("remote execution socket error: %s" % exc)
        finally:
            listener.close()
        data = reply.get("data") or {}
        out = "\n".join(o.get("output", "") for o in data.get("output", []) or [])
        if not data.get("success", False):
            raise TransportError("editor python error: %s %s" % (data.get("result"), out[-1500:]),
                                 code="EDITOR_EXEC_ERROR", retryable=False)
        return out or str(data.get("result", ""))

    def describe(self):
        return {"transport": "remote", "group": "%s:%s" % self.group}


def _recv_json(conn, timeout):
    """TCP messages are a single JSON document without a length prefix."""
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = conn.recv(BUFFER)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        try:
            return json.loads(buf.decode("utf-8"))
        except ValueError:
            continue
    if buf:
        try:
            return json.loads(buf.decode("utf-8"))
        except ValueError:
            pass
    raise TransportError("connection closed before a complete command_result arrived")
