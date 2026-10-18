"""Read-only workspace artifact layout check (not a distributed runtime CLI)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from storage.artifacts import layout_errors  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Workspace docs/artifact directory")
    args = parser.parse_args()
    errors = layout_errors(args.root.absolute())
    print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
