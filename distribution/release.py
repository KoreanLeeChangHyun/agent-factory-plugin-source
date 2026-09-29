#!/usr/bin/env python3
"""Coordinate a plugin-source release across every generated host checkout.

The plugin source (this repository, `agent-factory-plugin-source`) is the single
origin for every host plugin declared in `distribution/package.json["hosts"]`
(currently `codex`, `claude`, `antigravity`; the set is read from that file, not
hardcoded here). `distribution/port.py` renders each host's plugin into a
directory; this script wraps that generator with the release discipline the
individual `port.py` invocation does not enforce on its own:

  1. `plan`   - print the version, build token and preflight status only.
  2. `commit` - bump `distribution/package.json`, commit it in this repository,
                regenerate every host into its sibling checkout, and commit each
                checkout. Nothing is pushed.
  3. `push`   - push the exact commits `commit` produced, one host at a time,
                fast-forward only (never `--force`).

Each host's checkout is a *separate* Git repository (its own clone of
`agent-factory-<host>-plugin` or equivalent), not a directory inside this
repository. Pass one `--checkout <host>=<path>` per host declared in
`distribution/package.json`; the path must already be a clone of that host's
`repository` URL. `push` re-reads the checkout paths and expected commits from
the state file `commit` saved, so it does not need `--checkout` again unless
`--checkout` is passed explicitly to override.

This script never pushes as a side effect of `plan` or `commit`, and `push`
always asks for `--yes` before touching a remote. It grants no merge, publish or
force-push authority beyond a plain fast-forward push of commits it just made;
Human authorization for the push itself is out of this script's scope.

After every declared host reports the matching version on its remote `main`, the
VS Code extension release is a separate, already-existing step: run
`npm run release -- --message '...' --version X.Y.Z` in the `extension`
checkout (see `extension/scripts/release.mjs` and `extension/README.md`). This
script does not build or publish the extension.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

DISTRIBUTION = Path(__file__).resolve().parent
ROOT = DISTRIBUTION.parent
STATE_FILE = ROOT / ".git" / "agent-factory-plugin-release.json"

sys.path.insert(0, str(DISTRIBUTION))
import port  # noqa: E402  (local module, path set above)


def fail(message: str) -> "SystemExit":
    return SystemExit(f"error: {message}")


def package() -> dict:
    return json.loads((DISTRIBUTION / "package.json").read_text(encoding="utf-8"))


def write_package(meta: dict) -> None:
    path = DISTRIBUTION / "package.json"
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def run(args: list[str], cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise fail(f"`{' '.join(args)}` in {cwd} failed:\n{result.stderr or result.stdout}")
    return result.stdout.strip()


def git(cwd: Path, *args: str) -> str:
    return run(["git", *args], cwd)


def next_version(current: str) -> str:
    major, minor, patch = (int(part) for part in current.split("."))
    return f"{major}.{minor}.{patch + 1}"


def validate_version(value: str, current: str) -> None:
    import re

    if not re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise fail(f"Invalid version: {value}")
    if [int(p) for p in value.split(".")] <= [int(p) for p in current.split(".")]:
        raise fail(f"Release version must increase past {current}.")


def parse_checkouts(pairs: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for pair in pairs:
        if "=" not in pair:
            raise fail(f"--checkout must be host=path, got: {pair}")
        host, _, raw_path = pair.partition("=")
        result[host] = Path(raw_path).expanduser().resolve()
    return result


def require_clean_tracking_repo(cwd: Path, expected_remote: str | None) -> str:
    """Verify a repo is a clean checkout of `main` tracking `origin/main`, return HEAD."""
    if Path(git(cwd, "rev-parse", "--show-toplevel")).resolve() != cwd.resolve():
        raise fail(f"{cwd} is not a Git repository root.")
    if git(cwd, "status", "--porcelain") != "":
        raise fail(f"{cwd} has uncommitted changes; resolve them before releasing.")
    if git(cwd, "branch", "--show-current") != "main":
        raise fail(f"{cwd} must be on branch main.")
    try:
        upstream = git(cwd, "rev-parse", "--abbrev-ref", "@{upstream}")
    except SystemExit:
        raise fail(f"{cwd} main must track origin/main.")
    if upstream != "origin/main":
        raise fail(f"{cwd} main tracks {upstream}, expected origin/main.")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        if (cwd / ".git" / marker).exists():
            raise fail(f"{cwd} has an unfinished Git operation ({marker}).")
    if expected_remote is not None:
        origin = git(cwd, "remote", "get-url", "origin")
        if origin.rstrip("/").removesuffix(".git") != expected_remote.rstrip("/").removesuffix(".git"):
            raise fail(f"{cwd} origin is {origin}, expected {expected_remote}.")
    git(cwd, "fetch", "origin", "main")
    head = git(cwd, "rev-parse", "HEAD")
    if git(cwd, "rev-parse", "origin/main") != head:
        raise fail(f"{cwd} HEAD must equal origin/main before releasing; integrate remote changes first.")
    return head


def commit_all(cwd: Path, message: str) -> str | None:
    """Stage every change and commit; return the new commit hash, or None if nothing changed."""
    if git(cwd, "status", "--porcelain") == "":
        return None
    git(cwd, "add", "-A")
    git(cwd, "commit", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


def cmd_plan(args: argparse.Namespace) -> None:
    meta = package()
    version = args.version or next_version(meta["version"])
    validate_version(version, meta["version"])
    checkouts = parse_checkouts(args.checkout)
    declared = set(meta["hosts"])
    missing = declared - checkouts.keys()
    if missing:
        raise fail(f"Missing --checkout for declared host(s): {', '.join(sorted(missing))}")
    require_clean_tracking_repo(ROOT, None)
    report = {"version": version, "sourceVersion": meta["version"], "hosts": {}}
    for host, path in checkouts.items():
        head = require_clean_tracking_repo(path, meta["hosts"][host]["repository"])
        report["hosts"][host] = {"path": str(path), "head": head,
                                  "targetVersion": meta["hosts"][host]["version"].format(version=version, build="<build>")}
    print(json.dumps(report, indent=2))


def cmd_commit(args: argparse.Namespace) -> None:
    if STATE_FILE.exists():
        raise fail(f"{STATE_FILE} exists from an unfinished release; run `push` or remove it after manual recovery.")
    meta = package()
    version = args.version or next_version(meta["version"])
    validate_version(version, meta["version"])
    checkouts = parse_checkouts(args.checkout)
    declared = set(meta["hosts"])
    missing = declared - checkouts.keys()
    if missing:
        raise fail(f"Missing --checkout for declared host(s): {', '.join(sorted(missing))}")

    # Preflight every repository before mutating any of them.
    require_clean_tracking_repo(ROOT, None)
    for host, path in checkouts.items():
        require_clean_tracking_repo(path, meta["hosts"][host]["repository"])

    build = args.build or time.strftime("%Y%m%d%H%M%S", time.gmtime())
    source_message = args.message or (
        f"chore: release plugin source {version} / 플러그인 원본 {version} 릴리스"
    )
    meta["version"] = version
    write_package(meta)
    source_commit = commit_all(ROOT, source_message)
    if source_commit is None:
        raise fail("Version bump produced no change; is the target version already current?")

    state = {"version": version, "build": build, "source": {"path": str(ROOT), "commit": source_commit}, "hosts": {}}
    for host, path in checkouts.items():
        port.generate(host, path, build)
        host_message = args.message or (
            f"release: generate {host} plugin {version} from plugin source {source_commit[:7]} "
            f"/ 플러그인 원본 {source_commit[:7]}에서 {host} 플러그인 {version} 생성"
        )
        host_commit = commit_all(path, host_message)
        if host_commit is None:
            raise fail(f"Generation for host {host} produced no change; investigate before continuing.")
        state["hosts"][host] = {"path": str(path), "commit": host_commit}

    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(state, indent=2))
    print(f"\nCommitted locally only. Run `release.py push` after review to push {1 + len(checkouts)} repositories.")


def cmd_push(args: argparse.Namespace) -> None:
    if not STATE_FILE.exists():
        raise fail(f"No saved release at {STATE_FILE}; run `commit` first.")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not args.yes:
        print(json.dumps(state, indent=2))
        raise fail("Pass --yes to push the commits above to their origin/main (fast-forward only).")

    targets = [("source", Path(state["source"]["path"]), state["source"]["commit"])]
    targets += [(host, Path(info["path"]), info["commit"]) for host, info in state["hosts"].items()]
    for name, path, commit in targets:
        if git(path, "rev-parse", "HEAD") != commit:
            raise fail(f"{name} at {path} no longer matches the saved release commit {commit}; aborting before any push.")

    pushed: list[str] = []
    for name, path, commit in targets:
        git(path, "push", "origin", f"{commit}:refs/heads/main")
        pushed.append(name)
        print(f"{name}: pushed {commit} to origin/main")

    STATE_FILE.unlink()
    print(f"\nPushed {len(pushed)} repositories: {', '.join(pushed)}.")
    print("Next: build and publish the VS Code extension with the same base version "
          "(`npm run release` in the extension checkout; see its README).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--checkout", action="append", default=[], metavar="HOST=PATH",
                         help="Sibling checkout for a declared host; repeat per host.")
    common.add_argument("--version", help="Target version; defaults to the next patch version.")

    plan = sub.add_parser("plan", parents=[common], help="Report version and preflight status only.")
    plan.set_defaults(func=cmd_plan)

    commit = sub.add_parser("commit", parents=[common], help="Bump, generate and commit locally; no push.")
    commit.add_argument("--build", help="Build token (YYYYMMDDHHMMSS); defaults to the current UTC time.")
    commit.add_argument("--message", help="Override the default bilingual commit message for every repository.")
    commit.set_defaults(func=cmd_commit)

    push = sub.add_parser("push", help="Push the commits from the last `commit` run.")
    push.add_argument("--yes", action="store_true", help="Confirm pushing to every repository's origin/main.")
    push.set_defaults(func=cmd_push)

    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
