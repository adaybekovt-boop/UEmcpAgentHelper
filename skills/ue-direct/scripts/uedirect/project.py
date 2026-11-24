"""Unreal project discovery, engine resolution and version compatibility."""

import configparser
import glob
import json
import os
import re
import sys

from .util import UedHostError

SUPPORTED_MIN = (5, 8)      # fully supported (Epic Unreal MCP available)
DEGRADED_MIN = (5, 3)       # works via Python remote execution, not release-tested


class Project(object):
    def __init__(self, uproject):
        self.uproject = os.path.abspath(uproject)
        self.dir = os.path.dirname(self.uproject)
        self.name = os.path.splitext(os.path.basename(self.uproject))[0]
        try:
            with open(self.uproject, "r", encoding="utf-8-sig") as fh:
                self.data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise UedHostError("BAD_UPROJECT", "cannot read %s: %s" % (self.uproject, exc))
        self.association = self.data.get("EngineAssociation", "")

    def plugin_enabled(self, name, default=False):
        for p in self.data.get("Plugins", []) or []:
            if p.get("Name") == name:
                return bool(p.get("Enabled", False))
        return default

    @property
    def content_dir(self):
        return os.path.join(self.dir, "Content")

    def plugin_content_dirs(self):
        """Mount point name -> Content dir for project plugins (for /PluginName/... packages)."""
        out = {}
        for up in glob.glob(os.path.join(self.dir, "Plugins", "**", "*.uplugin"), recursive=True):
            c = os.path.join(os.path.dirname(up), "Content")
            if os.path.isdir(c):
                out[os.path.splitext(os.path.basename(up))[0]] = c
        return out

    def package_to_file(self, package):
        """/Game/Maps/City -> <Content>/Maps/City.umap or .uasset (whichever exists)."""
        package = package.split(".")[0]
        parts = package.strip("/").split("/", 1)
        if len(parts) != 2:
            return None
        root, rel = parts
        if root == "Game":
            base = self.content_dir
        else:
            base = self.plugin_content_dirs().get(root)
            if base is None:
                return None
        stem = os.path.join(base, *rel.split("/"))
        for ext in (".umap", ".uasset"):
            if os.path.isfile(stem + ext):
                return stem + ext
        return stem + ".uasset"

    def python_remote_settings(self):
        """Read PythonScriptPlugin remote-execution settings from DefaultEngine.ini."""
        cfg = {"enabled": False, "group": ("239.0.0.1", 6766), "bind": "127.0.0.1", "ttl": 0}
        ini = os.path.join(self.dir, "Config", "DefaultEngine.ini")
        if not os.path.isfile(ini):
            return cfg
        parser = configparser.RawConfigParser(strict=False, allow_no_value=True)
        parser.optionxform = str
        try:
            with open(ini, "r", encoding="utf-8-sig", errors="replace") as fh:
                parser.read_string(_sanitize_ini(fh.read()))
        except configparser.Error:
            return cfg
        sec = "/Script/PythonScriptPlugin.PythonScriptPluginSettings"
        if not parser.has_section(sec):
            return cfg
        get = lambda k: parser.get(sec, k, fallback=None)  # noqa: E731
        cfg["enabled"] = (get("bRemoteExecution") or "").strip().lower() == "true"
        ep = get("RemoteExecutionMulticastGroupEndpoint")
        if ep and ":" in ep:
            host, port = ep.strip().rsplit(":", 1)
            cfg["group"] = (host, int(port))
        if get("RemoteExecutionMulticastBindAddress"):
            cfg["bind"] = get("RemoteExecutionMulticastBindAddress").strip()
        if get("RemoteExecutionMulticastTtl"):
            cfg["ttl"] = int(get("RemoteExecutionMulticastTtl"))
        return cfg


def _sanitize_ini(text):
    # UE ini files may repeat keys and use +/-/. prefixes; keep the last plain value.
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s[:1] in "+-.!" and "=" in s:
            s = s[1:]
        out.append(s)
    return "\n".join(out)


def find_project(explicit=None, start=None):
    """Find the .uproject: explicit path/dir, $UED_PROJECT, or walk up from cwd."""
    cand = explicit or os.environ.get("UED_PROJECT")
    if cand:
        if os.path.isdir(cand):
            found = glob.glob(os.path.join(cand, "*.uproject"))
            if len(found) != 1:
                raise UedHostError("PROJECT_AMBIGUOUS" if found else "NO_PROJECT",
                                   "expected exactly one .uproject in %s, found %d" % (cand, len(found)))
            return Project(found[0])
        if os.path.isfile(cand) and cand.endswith(".uproject"):
            return Project(cand)
        raise UedHostError("NO_PROJECT", "not a .uproject or project directory: %s" % cand)
    d = os.path.abspath(start or os.getcwd())
    while True:
        found = glob.glob(os.path.join(d, "*.uproject"))
        if len(found) == 1:
            return Project(found[0])
        if len(found) > 1:
            raise UedHostError("PROJECT_AMBIGUOUS", "several .uproject files in %s" % d,
                               hint="pass --project <path.uproject>")
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    raise UedHostError("NO_PROJECT", "no .uproject found from %s upward" % os.path.abspath(start or os.getcwd()),
                       hint="run inside an Unreal project or pass --project / set UED_PROJECT")


# ------------------------------------------------------------------ engine

def read_build_version(engine_root):
    p = os.path.join(engine_root, "Engine", "Build", "Build.version")
    try:
        with open(p, "r", encoding="utf-8-sig") as fh:
            d = json.load(fh)
        return (int(d["MajorVersion"]), int(d["MinorVersion"]), int(d.get("PatchVersion", 0)))
    except (OSError, ValueError, KeyError):
        return None


def _is_engine_root(p):
    return bool(p) and os.path.isfile(os.path.join(p, "Engine", "Build", "Build.version"))


def _launcher_dat_paths():
    if sys.platform == "win32":
        base = os.environ.get("ProgramData", r"C:\ProgramData")
        return [os.path.join(base, "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")]
    if sys.platform == "darwin":
        return [os.path.expanduser("~/Library/Application Support/Epic/UnrealEngineLauncher/LauncherInstalled.dat")]
    return []


def _from_launcher(assoc):
    for dat in _launcher_dat_paths():
        try:
            with open(dat, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        for inst in data.get("InstallationList", []):
            if inst.get("AppName") == "UE_" + assoc and _is_engine_root(inst.get("InstallLocation")):
                return inst["InstallLocation"]
    return None


def _from_registry(assoc):
    if sys.platform != "win32":
        return None
    try:
        import winreg  # noqa: PLC0415 - Windows only
    except ImportError:
        return None
    probes = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\EpicGames\Unreal Engine\%s" % assoc, "InstalledDirectory"),
        (winreg.HKEY_CURRENT_USER, r"Software\Epic Games\Unreal Engine\Builds", assoc),
    ]
    for hive, key, value in probes:
        try:
            with winreg.OpenKey(hive, key) as k:
                path, _ = winreg.QueryValueEx(k, value)
                if _is_engine_root(path):
                    return path
        except OSError:
            continue
    return None


def _from_install_ini(assoc):
    ini = os.path.expanduser("~/.config/Epic/UnrealEngine/Install.ini")
    if not os.path.isfile(ini):
        return None
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    try:
        parser.read(ini, encoding="utf-8")
        path = parser.get("Installations", assoc, fallback=None)
    except configparser.Error:
        return None
    return path if _is_engine_root(path) else None


def _default_dirs(assoc):
    if sys.platform == "win32":
        bases = [os.environ.get("ProgramFiles", r"C:\Program Files") + r"\Epic Games"]
    elif sys.platform == "darwin":
        bases = ["/Users/Shared/Epic Games"]
    else:
        bases = [os.path.expanduser("~"), "/opt"]
    for b in bases:
        for name in ("UE_" + assoc, "UnrealEngine-" + assoc, "UnrealEngine"):
            p = os.path.join(b, name)
            if _is_engine_root(p):
                return p
    return None


def resolve_engine(project, explicit=None):
    """Return (engine_root, source) or raise with an actionable message."""
    for cand, src in ((explicit, "--engine"), (os.environ.get("UE_ENGINE_ROOT"), "UE_ENGINE_ROOT")):
        if cand:
            if _is_engine_root(cand):
                return os.path.abspath(cand), src
            raise UedHostError("BAD_ENGINE_ROOT", "%s=%s is not an engine root (no Engine/Build/Build.version)"
                               % (src, cand))
    assoc = project.association
    if not assoc:
        d = project.dir
        while True:
            if _is_engine_root(d):
                return d, "parent-of-project"
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    for fn, src in ((_from_registry, "registry"), (_from_launcher, "launcher"),
                    (_from_install_ini, "Install.ini"), (_default_dirs, "default-dir")):
        path = fn(assoc)
        if path:
            return os.path.abspath(path), src
    raise UedHostError("ENGINE_NOT_FOUND", "cannot locate engine for EngineAssociation=%r" % assoc,
                       hint="set UE_ENGINE_ROOT to the folder that contains Engine/")


def editor_binary(engine_root):
    if sys.platform == "win32":
        p = os.path.join(engine_root, "Engine", "Binaries", "Win64", "UnrealEditor.exe")
    elif sys.platform == "darwin":
        p = os.path.join(engine_root, "Engine", "Binaries", "Mac", "UnrealEditor.app", "Contents", "MacOS",
                         "UnrealEditor")
    else:
        p = os.path.join(engine_root, "Engine", "Binaries", "Linux", "UnrealEditor")
    return p


def parse_version(text):
    """'5.8.1-12345+++UE5+Release-5.8' or '5.8' -> (5, 8, 1)."""
    m = re.match(r"\s*(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def compat(version):
    """Return 'supported' | 'degraded' | 'incompatible' | 'unknown'."""
    if not version:
        return "unknown"
    if version[:2] >= SUPPORTED_MIN:
        return "supported"
    if version[:2] >= DEGRADED_MIN:
        return "degraded"
    return "incompatible"
