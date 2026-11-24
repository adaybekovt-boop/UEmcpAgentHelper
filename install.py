#!/usr/bin/env python3
"""UE-Direct installer (stdlib only). Non-destructive and reversible.

  python install.py --project <dir-with-.uproject> [--agents claude,codex] [--link] [--dry-run]
  python install.py --user [--agents claude,codex]            # skill only, for every project
  python install.py --project <dir> --uninstall

What it does (project scope):
  * copies (or symlinks) skills/ue-direct to .claude/skills/ue-direct and/or .agents/skills/ue-direct
  * merges hook entries into .claude/settings.json and/or .codex/hooks.json
    (existing settings and hooks are kept; re-running is idempotent)
  * adds a marked block to CLAUDE.md / AGENTS.md (the rest of the file is never touched)
Codex asks you to review and trust new project hooks (/hooks) before they run.
"""

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_SRC = os.path.join(HERE, "skills", "ue-direct")
BEGIN, END = "<!-- UE-DIRECT:BEGIN", "<!-- UE-DIRECT:END -->"
HOOK_MARK = "ue-direct"


def _python_cmd(explicit=None):
    if explicit:
        return explicit
    return "python" if sys.platform == "win32" else "python3"


class Plan(object):
    def __init__(self, dry):
        self.dry = dry
        self.actions = []

    def do(self, desc, fn):
        self.actions.append(desc)
        if not self.dry:
            fn()


def _copy_skill(plan, dest, link):
    def run():
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        if os.path.islink(dest):
            os.unlink(dest)
        elif os.path.isdir(dest):
            if not os.path.isfile(os.path.join(dest, "SKILL.md")):
                raise SystemExit("refusing to overwrite %s: not a UE-Direct skill folder" % dest)
            shutil.rmtree(dest)
        if link:
            os.symlink(SKILL_SRC, dest, target_is_directory=True)
        else:
            shutil.copytree(SKILL_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    plan.do("%s skill -> %s" % ("link" if link else "copy", dest), run)


def _load_json(path):
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return json.loads(text) if text.strip() else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        shutil.copy2(path, path + ".ue-direct.bak")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _is_ours(handler):
    cmd = handler.get("command") or ""
    return HOOK_MARK in cmd and "hook" in cmd and "ued.py" in cmd


def hook_command(python, event, script=None, project_env=None):
    """A launcher that exits 0 when the skill is missing (a missing script must never make every
    tool call fail: Python exits 2 on a missing file, which agents treat as 'block'). The script
    path is resolved inside Python, so no shell variable expansion is needed (works on Windows)."""
    if project_env:
        path = "os.path.join(os.environ.get('%s','.'),'.claude','skills','ue-direct','scripts','ued.py')" % project_env
    else:
        path = repr(script)
    code = ("import os,runpy,sys;p=%s;sys.argv=[p,'hook','%s'];"
            "os.path.isfile(p) and runpy.run_path(p,run_name='__main__')" % (path, event))
    return '%s -c "%s"' % (python, code)


def merge_hooks(existing, template):
    """Add UE-Direct hook groups; drop any previous UE-Direct handlers first (idempotent)."""
    out = dict(existing)
    hooks = {k: list(v) for k, v in (existing.get("hooks") or {}).items()}
    for event, groups in hooks.items():
        cleaned = []
        for g in groups:
            hs = [h for h in g.get("hooks", []) if not _is_ours(h)]
            if hs:
                cleaned.append(dict(g, hooks=hs))
        hooks[event] = cleaned
    for event, groups in template["hooks"].items():
        hooks.setdefault(event, []).extend(groups)
    out["hooks"] = {k: v for k, v in hooks.items() if v}
    if not out["hooks"]:
        del out["hooks"]
    return out


def remove_hooks(existing):
    return merge_hooks(existing, {"hooks": {}})


def _render(template_path, commands):
    """Fill {HOOK:<event>} placeholders with JSON-escaped hook commands."""
    with open(template_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    for event, cmd in commands.items():
        text = text.replace("{HOOK:%s}" % event, json.dumps(cmd)[1:-1])
    return json.loads(text)


def upsert_block(text, block):
    if BEGIN in text and END in text:
        pre = text[:text.index(BEGIN)]
        post = text[text.index(END) + len(END):]
        return pre + block.strip() + post
    sep = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + sep + block.strip() + "\n"


def remove_block(text):
    if BEGIN in text and END in text:
        pre = text[:text.index(BEGIN)].rstrip("\n")
        post = text[text.index(END) + len(END):].lstrip("\n")
        return (pre + "\n" + ("\n" + post if post else "")) if pre else post
    return text


def _edit_text(plan, path, fn, desc):
    def run():
        text = ""
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        new = fn(text)
        if new != text:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(new)
    plan.do(desc, run)


def install(project, agents, link=False, dry=False, python=None, hooks=True, instructions=True):
    plan = Plan(dry)
    py = _python_cmd(python)
    with open(os.path.join(HERE, "integrations", "instructions.snippet.md"), "r", encoding="utf-8") as fh:
        snippet = fh.read()
    if "claude" in agents:
        _copy_skill(plan, os.path.join(project, ".claude", "skills", "ue-direct"), link)
        if hooks:
            path = os.path.join(project, ".claude", "settings.json")
            cmds = {e: hook_command(py, e, project_env="CLAUDE_PROJECT_DIR") for e in ("pre-tool", "post-tool", "stop")}
            tpl = _render(os.path.join(HERE, "integrations", "claude-code", "hooks.template.json"), cmds)
            plan.do("merge hooks into %s" % path, lambda: _write_json(path, merge_hooks(_load_json(path), tpl)))
        if instructions:
            block = snippet.replace("{UED}", "%s .claude/skills/ue-direct/scripts/ued.py" % py)
            _edit_text(plan, os.path.join(project, "CLAUDE.md"), lambda t: upsert_block(t, block),
                       "add UE-Direct block to CLAUDE.md")
    if "codex" in agents:
        dest = os.path.join(project, ".agents", "skills", "ue-direct")
        _copy_skill(plan, dest, link)
        script = os.path.join(os.path.abspath(dest), "scripts", "ued.py")
        if hooks:
            path = os.path.join(project, ".codex", "hooks.json")
            cmds = {e: hook_command(py, e, script=script) for e in ("pre-tool", "post-tool", "stop")}
            tpl = _render(os.path.join(HERE, "integrations", "codex", "hooks.template.json"), cmds)
            plan.do("merge hooks into %s" % path, lambda: _write_json(path, merge_hooks(_load_json(path), tpl)))
        if instructions:
            block = snippet.replace("{UED}", "%s .agents/skills/ue-direct/scripts/ued.py" % py)
            _edit_text(plan, os.path.join(project, "AGENTS.md"), lambda t: upsert_block(t, block),
                       "add UE-Direct block to AGENTS.md")
    return plan


def install_user(agents, link=False, dry=False):
    plan = Plan(dry)
    home = os.path.expanduser("~")
    if "claude" in agents:
        _copy_skill(plan, os.path.join(home, ".claude", "skills", "ue-direct"), link)
    if "codex" in agents:
        _copy_skill(plan, os.path.join(home, ".agents", "skills", "ue-direct"), link)
    return plan


def uninstall(project, dry=False):
    plan = Plan(dry)
    for d in (os.path.join(project, ".claude", "skills", "ue-direct"),
              os.path.join(project, ".agents", "skills", "ue-direct")):
        if os.path.islink(d):
            plan.do("unlink %s" % d, lambda d=d: os.unlink(d))
        elif os.path.isfile(os.path.join(d, "SKILL.md")):
            plan.do("remove %s" % d, lambda d=d: shutil.rmtree(d))
    for path in (os.path.join(project, ".claude", "settings.json"), os.path.join(project, ".codex", "hooks.json")):
        if os.path.isfile(path):
            plan.do("remove UE-Direct hooks from %s" % path,
                    lambda p=path: _write_json(p, remove_hooks(_load_json(p))))
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = os.path.join(project, name)
        if os.path.isfile(path):
            _edit_text(plan, path, remove_block, "remove UE-Direct block from %s" % name)
    return plan


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="Unreal project directory (contains the .uproject)")
    ap.add_argument("--user", action="store_true", help="install the skill for all projects (no hooks)")
    ap.add_argument("--agents", default="claude,codex")
    ap.add_argument("--link", action="store_true", help="symlink instead of copy (for skill development)")
    ap.add_argument("--python", help="interpreter command used in hooks (default python3 / python on Windows)")
    ap.add_argument("--no-hooks", action="store_true")
    ap.add_argument("--no-instructions", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    agents = {x.strip() for x in a.agents.split(",") if x.strip()}
    if a.user:
        plan = install_user(agents, a.link, a.dry_run)
    else:
        if not a.project:
            ap.error("--project or --user is required")
        project = os.path.abspath(a.project)
        if not any(f.endswith(".uproject") for f in os.listdir(project)):
            ap.error("%s does not contain a .uproject" % project)
        plan = uninstall(project, a.dry_run) if a.uninstall else \
            install(project, agents, a.link, a.dry_run, a.python, not a.no_hooks, not a.no_instructions)
    print(json.dumps({"dry_run": a.dry_run, "actions": plan.actions}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
