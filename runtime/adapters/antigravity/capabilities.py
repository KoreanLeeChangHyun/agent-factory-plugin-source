"""Antigravity CLI (agy) capabilities. Model identifiers come from `agy models` and pass through."""
from pathlib import Path
import shutil
import subprocess
import time

from tasks.modes import TASK_MODES

EFFORTS = ("low", "medium", "high", "max")
# Agent Factory effort names without an agy level map to the nearest; "none" leaves agy's default.
EFFORT_ALIASES = {"none": None, "minimal": "low", "xhigh": "max", "ultra": "max"}
REQUIRED_OPTIONS = ("--print", "--output-format", "--input-format", "--json-schema", "--conversation",
                    "--dangerously-skip-permissions", "--add-dir", "--effort", "--disable-slash-commands")
CAPABILITY_CACHE_TTL = 60


def inspect_capabilities(executable, *, refresh=False, runtime_home=None, **_kwargs):
    """Probe `agy --help`; reuse a recent successful probe of the same binary."""
    identity = file = None
    try:
        from storage import paths
        identity = _identity(executable)
        file = paths.home_path(runtime_home) / "cache" / "native-capabilities" / "antigravity.json"
    except (OSError, ValueError):
        file = None
    if file is not None and not refresh:
        try:
            cached = paths.read(file)
            if (isinstance(cached, dict) and cached.get("identity") == identity
                    and isinstance(cached.get("created"), (int, float))
                    and 0 <= time.time() - cached["created"] < CAPABILITY_CACHE_TTL):
                return cached["capabilities"]
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Missing or unreadable cache: probe again.
    result = _probe(executable)
    if file is not None and result["diagnostic"] is None:
        try:
            paths.mkdir(file.parent)
            paths.write(file, {"identity": identity, "created": time.time(), "capabilities": result})
        except (OSError, ValueError):
            pass  # A cache is an optimization; a failed write never fails the probe.
    return result


def _identity(executable):
    path = Path(shutil.which(executable) or executable).resolve(strict=True)
    info = path.stat()
    return {"path": str(path), "size": info.st_size, "mtimeNs": info.st_mtime_ns}


def _probe(executable):
    diagnostic = None
    available = False
    try:
        # agy prints its flag list to stderr.
        result = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=20)
        text = result.stdout + result.stderr
        missing = [flag for flag in REQUIRED_OPTIONS if flag not in text]
        available = result.returncode == 0 and not missing
        if not available:
            diagnostic = ("Installed Antigravity CLI lacks required print options: " + ", ".join(missing)
                          if missing else f"Antigravity CLI --help exited with {result.returncode}")
    except (OSError, subprocess.TimeoutExpired) as error:
        diagnostic = f"Antigravity CLI unavailable: {error}"
    # Print mode accepts only text blocks, so no images. Goal uses agy's own /goal command.
    supported = {"model": available, "reasoning": available, "fast": False, "goal": available,
                 "plan": available, "instructionDelivery": available, "images": False,
                 "taskModes": list(TASK_MODES) if available else [], "automaticRequestHash": True,
                 "worktrees": available}
    return {"schemaVersion": "0.1.0", "kind": "execution-capabilities", "backend": "antigravity-print",
            "submit": supported, "send": dict(supported), "diagnostic": diagnostic}
