#!/usr/bin/env python3
"""Generate skills/tool/references/usage/<script>.md from each plugin script's argparse definition.

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


# Runs in a child interpreter: capture the script's argparse tree instead of printing --help.
PROBE = r"""
import argparse, json, runpy, sys
def describe(parser):
    groups = [[o for a in g._group_actions for o in a.option_strings[:1]] for g in parser._mutually_exclusive_groups]
    options, commands = [], []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction) or action.help == argparse.SUPPRESS:
            continue
        if isinstance(action, argparse._SubParsersAction):
            helps = {c.dest: c.help for c in action._choices_actions}
            for name, sub in action.choices.items():
                if not name.startswith("_"):
                    commands.append({"name": name, "help": helps.get(name) or sub.description, **describe(sub)})
            continue
        flags = [o for o in action.option_strings if not o.startswith("--no-")] or [action.dest]
        value = None
        if action.nargs != 0:
            value = "{" + ",".join(map(str, action.choices)) + "}" if action.choices else (action.metavar or action.dest.upper())
        options.append({"flags": flags, "value": value, "help": action.help, "required": action.required,
                        "negatable": any(o.startswith("--no-") for o in action.option_strings), "positional": not action.option_strings})
    return {"description": parser.description, "options": options, "groups": groups, "commands": commands}
def capture(self, *args, **kwargs):
    print(json.dumps(describe(self)))
    raise SystemExit(0)
argparse.ArgumentParser.parse_known_args = capture
sys.argv = [sys.argv[1]]
import os; sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0])))
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def describe(script: Path) -> dict:
    import json
    result = subprocess.run([sys.executable, "-c", PROBE, str(script)], capture_output=True, text=True,
                            env=ENVIRONMENT, timeout=30, check=False)
    return json.loads(result.stdout.strip().splitlines()[-1])


def option_text(option: dict) -> str:
    flag = option["flags"][0]
    if option["negatable"]:
        flag += f" | --no-{flag[2:]}"
    return f"`{flag}{' ' + option['value'] if option['value'] else ''}`"


def render_options(tree: dict, skip: set[str]) -> list[str]:
    lines = []
    required = [option_text(o) for o in tree["options"] if o["required"] or o["positional"]]
    if required:
        lines.append("Required: " + ", ".join(required))
    for group in tree["groups"]:
        lines.append("One of: " + " | ".join(f"`{flag}`" for flag in group))
    for option in tree["options"]:
        if option["flags"][0] in skip or option["required"] or option["positional"]:
            continue
        help_text = " ".join((option["help"] or "").split())
        lines.append(f"- {option_text(option)}" + (f": {help_text}" if help_text else ""))
    return lines


def render() -> dict[str, str]:
    files = {}
    for script in sorted((ROOT / "scripts").glob("*.py")):
        tree = describe(script)
        parts = [f"# `{script.name}` usage", "",
                 "Generated from argparse by `distribution/tool_usage.py`; do not edit by hand.",
                 "Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).", ""]
        commands = tree["commands"]
        # Options every subcommand shares are listed once.
        with_options = [c for c in commands if c["options"]]
        common = set.intersection(*[{o["flags"][0] for o in c["options"] if not o["required"]} for c in with_options]) if len(with_options) > 1 else set()
        if common:
            shared = [o for o in with_options[0]["options"] if o["flags"][0] in common]
            parts += ["Every subcommand with options also accepts: " + ", ".join(option_text(o) for o in shared) + ".", ""]
        parts += render_options(tree, set())
        while parts and parts[-1] == "": parts.pop()
        for command in commands:
            summary = " ".join((command["help"] or "").split())
            parts += ["", f"## `{command['name']}`" + (f": {summary}" if summary else "")]
            parts += render_options(command, common) or (["No options."] if not command["options"] else [])
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
