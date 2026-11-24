"""Editor process discovery and launching (Windows first; macOS/Linux supported)."""

import json
import os
import subprocess
import sys


def list_editor_processes():
    """Return [{'pid': int, 'cmd': str}] for running UnrealEditor processes."""
    out = []
    try:
        if sys.platform == "win32":
            ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'UnrealEditor%'\" | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
            raw = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                                 capture_output=True, text=True, timeout=15).stdout.strip()
            if raw:
                data = json.loads(raw)
                for row in (data if isinstance(data, list) else [data]):
                    out.append({"pid": int(row.get("ProcessId", 0)), "cmd": row.get("CommandLine") or ""})
        else:
            raw = subprocess.run(["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=10).stdout
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                pid, _, args = line.partition(" ")
                exe = args.split(" ", 1)[0]
                if os.path.basename(exe).startswith("UnrealEditor") and "ShaderCompileWorker" not in exe:
                    out.append({"pid": int(pid), "cmd": args})
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [p for p in out if "-run=" not in p["cmd"].lower()]  # ignore commandlets


def processes_for_project(procs, project):
    key = project.uproject.replace("\\", "/").lower()
    name = (project.name + ".uproject").lower()
    mine, others = [], []
    for p in procs:
        cmd = p["cmd"].replace("\\", "/").lower()
        (mine if key in cmd or name in cmd else others).append(p)
    return mine, others


def pid_alive(pid):
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"], capture_output=True, text=True,
                               timeout=10)
            return str(pid) in r.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def launch_editor(binary, args, log_path):
    """Start the editor detached from this process. Returns the PID."""
    kwargs = {"stdin": subprocess.DEVNULL, "close_fds": True}
    log = open(log_path, "ab")  # noqa: SIM115 - handed to the child
    kwargs["stdout"] = log
    kwargs["stderr"] = subprocess.STDOUT
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([binary] + list(args), **kwargs)
    return proc.pid
