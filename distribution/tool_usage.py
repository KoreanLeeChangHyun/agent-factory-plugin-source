#!/usr/bin/env python3
"""Generate skills/tool/references/usage/<script>.md from each plugin script's own --help.

One file per script, so an agent reads only the tool it is about to run.

argparse stays the single source for arguments; this file only renders it.
Usage: python3 distribution/tool_usage.py [--check]
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "skills" / "tool" / "references" / "usage"
ENVIRONMENT = {**os.environ, "COLUMNS": "100", "AGENT_FACTORY_HOME": os.environ.get("AGENT_FACTORY_HOME", "/nonexistent")}


def run_help(script: Path, *prefix: str) -> str:
    result = subprocess.run([sys.executable, str(script), *prefix, "--help"], capture_output=True, text=True,
                            env=ENVIRONMENT, timeout=30, check=False)
    text = (result.stdout or result.stderr).strip()
    # Python 3.10 prints "optional arguments:"; 3.12+ prints "options:".
    return text.replace("optional arguments:", "options:")


def subcommands(help_text: str) -> list[str]:
    """Public subcommands of a subparser-based script (names starting with _ are internal)."""
    match = re.search(r"^positional arguments:\n\s+\{([^}]+)\}", help_text, re.M)
    if not match or " ..." not in help_text.split("\n\n", 1)[0]:
        return []
    return [name for name in match.group(1).split(",") if not name.startswith("_")]


def render() -> dict[str, str]:
    files = {}
    for script in sorted((ROOT / "scripts").glob("*.py")):
        text = run_help(script)
        parts = [f"# `{script.name}` usage", "",
                 "Generated from `--help` by `distribution/tool_usage.py`; do not edit by hand.",
                 "Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).", "",
                 "```text", text, "```", ""]
        for name in subcommands(text):
            parts += [f"## `{name}`", "", "```text", run_help(script, name), "```", ""]
        files[script.stem + ".md"] = "\n".join(parts).rstrip() + "\n"
    return files


def main() -> int:
    files = render()
    existing = {path.name: path.read_text(encoding="utf-8") for path in OUTPUT.glob("*.md")} if OUTPUT.is_dir() else {}
    if "--check" in sys.argv[1:]:
        if existing != files:
            print(f"{OUTPUT.relative_to(ROOT)} is out of date; run python3 distribution/tool_usage.py", file=sys.stderr)
            return 1
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name in set(existing) - set(files):
        (OUTPUT / name).unlink()
    for name, content in files.items():
        (OUTPUT / name).write_text(content, encoding="utf-8")
    print(f"wrote {len(files)} files to {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
