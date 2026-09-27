#!/usr/bin/env python3
"""Generate host plugin distributions from the single Agent Factory source.

The source tree holds host-neutral `skills/`, `runtime/` and `scripts/`. Each host
directory under `distribution/hosts/<host>/` holds only that host's manifests and
README templates. Generated output is a complete installable plugin repository root.
"""
from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DISTRIBUTION = Path(__file__).resolve().parent
ROOT = DISTRIBUTION.parent
HOSTS = DISTRIBUTION / "hosts"
PLACEHOLDER = re.compile(r"\{\{([A-Za-z]+)\}\}")
TEMPLATE_SUFFIXES = {".json", ".md"}
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")
# Distribution metadata owned by the generator and preserved from the destination.
PRESERVED = {".git"}
PROVENANCE = "DISTRIBUTION.json"


def package() -> dict:
    return json.loads((DISTRIBUTION / "package.json").read_text(encoding="utf-8"))


def hosts() -> list[str]:
    return sorted(path.name for path in HOSTS.iterdir() if path.is_dir())


def source_commit() -> str | None:
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def default_build() -> str:
    """Derive a deterministic build token from the source commit time (UTC)."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", "--format=%cd", "--date=format-local:%Y%m%d%H%M%S"],
        capture_output=True, text=True, env={**os.environ, "TZ": "UTC"})
    token = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"\d{14}", token):
        raise SystemExit("Cannot derive a build token from Git; pass --build YYYYMMDDHHMMSS")
    return token


def values(meta: dict, host: str, build: str) -> dict[str, str]:
    config = meta["hosts"][host]
    return {
        "name": meta["name"],
        "version": meta["version"],
        "hostVersion": config["version"].format(version=meta["version"], build=build),
        "description": meta["description"],
        "repository": config["repository"],
        "source": meta["source"],
    }


def render(text: str, context: dict[str, str], origin: Path) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in context:
            raise SystemExit(f"Unknown placeholder {{{{{key}}}}} in {origin}")
        value = context[key]
        # JSON templates receive JSON-escaped string content.
        return json.dumps(value)[1:-1] if origin.suffix == ".json" else value
    return PLACEHOLDER.sub(replace, text)


def generate(host: str, output: Path, build: str) -> None:
    meta = package()
    if host not in meta["hosts"] or not (HOSTS / host).is_dir():
        raise SystemExit(f"Unknown host: {host}")
    context = values(meta, host, build)
    output.mkdir(parents=True, exist_ok=True)
    for child in output.iterdir():
        if child.name in PRESERVED:
            continue
        shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    for name in meta["payload"]:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, output / name, ignore=IGNORED)
        else:
            shutil.copy2(source, output / name)
    template_root = HOSTS / host
    for template in sorted(template_root.rglob("*")):
        if template.is_dir() or "__pycache__" in template.parts:
            continue
        target = output / template.relative_to(template_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if template.suffix in TEMPLATE_SUFFIXES:
            target.write_text(render(template.read_text(encoding="utf-8"), context, template), encoding="utf-8")
        else:
            shutil.copy2(template, target)
    provenance = {"generatedBy": "distribution/port.py", "host": host, "version": context["hostVersion"],
                  "source": meta["source"], "sourceCommit": source_commit()}
    (output / PROVENANCE).write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")


def differences(expected: Path, actual: Path) -> list[str]:
    """List relative paths that differ, ignoring preserved metadata and provenance."""
    found: list[str] = []

    def walk(comparison: filecmp.dircmp, prefix: str) -> None:
        for name in comparison.left_only + comparison.right_only + comparison.funny_files:
            if name not in PRESERVED and not (prefix == "" and name == PROVENANCE):
                found.append(prefix + name)
        _, mismatch, errors = filecmp.cmpfiles(comparison.left, comparison.right, comparison.common_files, shallow=False)
        found.extend(prefix + name for name in mismatch + errors if not (prefix == "" and name == PROVENANCE))
        for name, child in comparison.subdirs.items():
            if name not in PRESERVED:
                walk(child, f"{prefix}{name}/")

    walk(filecmp.dircmp(expected, actual, ignore=[]), "")
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", choices=[*hosts(), "all"], default="all")
    parser.add_argument("--out", type=Path, required=True,
                        help="Output directory; with --host all, one subdirectory per host")
    parser.add_argument("--build", help="Build token (YYYYMMDDHHMMSS); defaults to the source commit time")
    parser.add_argument("--check", action="store_true",
                        help="Compare a fresh generation with --out instead of writing it")
    args = parser.parse_args(argv)
    build = args.build or default_build()
    selected = hosts() if args.host == "all" else [args.host]
    status = 0
    for host in selected:
        target = args.out / host if args.host == "all" else args.out
        if not args.check:
            generate(host, target, build)
            print(f"{host}: generated {target}")
            continue
        with tempfile.TemporaryDirectory() as directory:
            fresh = Path(directory) / host
            generate(host, fresh, build)
            drift = differences(fresh, target) if target.is_dir() else ["<missing output>"]
        if drift:
            status = 1
            print(f"{host}: {len(drift)} path(s) differ from the source generation", file=sys.stderr)
            for path in drift:
                print(f"  {path}", file=sys.stderr)
        else:
            print(f"{host}: up to date")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
