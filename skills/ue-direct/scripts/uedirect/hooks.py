"""Agent hooks (Claude Code and Codex share the same JSON shapes).

* pre-tool : deny tool calls that write fake native assets (.umap/.uasset via
             Write/Edit/apply_patch/shell redirection) or tamper with UE-Direct
             evidence/task files.
* post-tool: record editor mutations made through other Unreal MCP tools so
             the completion gate still demands save + verification.
* stop     : block the agent from ending its turn while an active UE-Direct
             task has unmet completion requirements.

Limits: hooks only see tools the client routes through them. A shell command
that writes files indirectly (a script, a compiled program) cannot be fully
detected. Hooks are a guard rail, not a sandbox.
"""

import json
import os
import re
import sys

from . import completion
from .project import find_project
from .util import UedHostError, append_jsonl, now, read_json, write_json

NATIVE_EXT = re.compile(r"\.(umap|uasset)\b", re.I)
PROTECTED = re.compile(r"Saved[\\/]+UEDirect[\\/]+(evidence\.jsonl|task\.json|tasks\.jsonl)", re.I)
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "write_file", "edit_file",
               "create_file", "str_replace_editor"}
SHELL_TOOLS = {"Bash", "PowerShell", "shell", "local_shell", "exec_command", "container.exec"}
# shell constructs that write to a path: redirection, tee, cp/mv/dd/install, PowerShell writers
SHELL_WRITE = re.compile(
    r"(>{1,2}\s*['\"]?(?P<redir>[^\s'\";|&]+))|"
    r"(\b(?:tee|cp|mv|install|dd|rsync|Copy-Item|Move-Item|Set-Content|Out-File|Add-Content|New-Item)\b"
    r"(?P<args>[^;|&]*))", re.I)
READ_ONLY_MCP = re.compile(r"(list_|describe_|get_|search_|read_me|grep_|find_|query|inspect)", re.I)


def _paths_from_patch(text):
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", text or "", re.M)


def written_paths(tool, tool_input):
    """Best-effort list of file paths a tool call will write."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    paths = []
    if tool in WRITE_TOOLS:
        for k in ("file_path", "path", "notebook_path", "filename"):
            if isinstance(ti.get(k), str):
                paths.append(ti[k])
        patch = ti.get("command") if tool == "apply_patch" else ti.get("patch") or ti.get("input")
        if isinstance(patch, list):
            patch = "\n".join(map(str, patch))
        for a, b in _paths_from_patch(patch if isinstance(patch, str) else ""):
            paths.append(a or b)
    return [p.strip() for p in paths if p]


def shell_violations(command):
    if not isinstance(command, str):
        command = " ".join(map(str, command or []))
    bad = []
    for m in SHELL_WRITE.finditer(command):
        target = m.group("redir") or m.group("args") or ""
        if NATIVE_EXT.search(target):
            bad.append("shell write to a native asset path: %s" % target.strip()[:120])
        if PROTECTED.search(target):
            bad.append("shell write to UE-Direct evidence: %s" % target.strip()[:120])
    return bad


def check_pre_tool(payload):
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    reasons = []
    for p in written_paths(tool, ti):
        if NATIVE_EXT.search(p):
            reasons.append("%s would write %s directly. .umap/.uasset files must be created by Unreal Editor "
                           "(ued batch / ued level new / editor tools), never written as files." % (tool, p))
        if PROTECTED.search(p):
            reasons.append("%s would modify UE-Direct evidence (%s); evidence is written only by the editor "
                           "and the ued CLI." % (tool, p))
    if tool in SHELL_TOOLS or (tool not in WRITE_TOOLS and isinstance(ti, dict) and "command" in ti):
        cmd = ti.get("command") if isinstance(ti, dict) else None
        if isinstance(cmd, (str, list)) and not _is_ued(cmd):
            reasons.extend(shell_violations(cmd))
    return reasons


def _is_ued(cmd):
    text = cmd if isinstance(cmd, str) else " ".join(map(str, cmd))
    return bool(re.search(r"ued\.py\b|\bued\s", text)) and not re.search(r"[>|]", text.split("ued", 1)[-1])


def _project_from(payload):
    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        return find_project(start=cwd)
    except UedHostError:
        return None


def handle(event, payload):
    """Return (exit_code, stdout_json_or_None, stderr_text)."""
    if event == "pre-tool":
        reasons = check_pre_tool(payload)
        if not reasons:
            return 0, None, ""
        msg = "UE-Direct: " + " | ".join(reasons[:3])
        return 0, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                          "permissionDecisionReason": msg}}, msg
    project = _project_from(payload)
    if project is None:
        return 0, None, ""
    sdir = os.path.join(project.dir, "Saved", "UEDirect")
    if event == "post-tool":
        tool = payload.get("tool_name") or ""
        low = tool.lower()
        if low.startswith("mcp__") and ("unreal" in low or "ue" in low.split("__")[1:2]):
            args = json.dumps(payload.get("tool_input") or {})[:400]
            relayed = "uedirect_editor" in args  # a relayed ued request records its own evidence
            if not relayed and not READ_ONLY_MCP.search(tool.split("__")[-1] + " " + args[:80]):
                os.makedirs(sdir, exist_ok=True)
                completion.ensure_task(sdir)
                append_jsonl(os.path.join(sdir, "evidence.jsonl"),
                             {"t": now(), "src": "host", "kind": "external_mutation", "tool": tool,
                              "task": (completion.load_task(sdir) or {}).get("id"), "level_affecting": True})
        return 0, None, ""
    if event == "stop":
        task = completion.load_task(sdir)
        if not task or task.get("status") != "active":
            return 0, None, ""
        verdict = completion.evaluate(sdir, task)
        if verdict["status"] == "success":
            return 0, None, ""
        state = read_json(os.path.join(sdir, "stop_guard.json"), {}) or {}
        sig = json.dumps(verdict["missing"], sort_keys=True)
        count = state.get("count", 0) + 1 if state.get("sig") == sig else 1
        write_json(os.path.join(sdir, "stop_guard.json"), {"sig": sig, "count": count, "t": now()})
        if count > 3:
            # no progress after repeated blocks: let the turn end, but leave an honest record
            append_jsonl(os.path.join(sdir, "evidence.jsonl"), {"t": now(), "src": "host", "kind": "stopped_incomplete",
                                                               "task": task["id"], "missing": verdict["missing"]})
            return 0, {"systemMessage": "UE-Direct: task %s ended INCOMPLETE: %s" % (task["id"], verdict["missing"])}, ""
        reason = ("UE-Direct completion gate: task %s (%s) is not verified. Missing: %s. Do the missing steps, "
                  "then `ued complete`. If you are genuinely blocked, run `ued task block --reason \"...\"` "
                  "and report the blocker to the user." % (task["id"], task["kind"], "; ".join(verdict["missing"])))
        return 0, {"decision": "block", "reason": reason}, ""
    return 0, None, ""


def main(event):
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    try:
        code, out, err = handle(event, payload)
    except Exception as exc:  # noqa: BLE001 - a broken hook must never wedge the agent
        sys.stderr.write("ue-direct hook error: %s\n" % exc)
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out))
    if err and code:
        sys.stderr.write(err)
    return code
