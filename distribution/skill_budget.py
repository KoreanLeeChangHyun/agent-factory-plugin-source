#!/usr/bin/env python3
"""Report and enforce how many tokens agents spend reading Skill documents.

Agents always load every SKILL.md frontmatter description, read a SKILL.md when the Skill
applies, and open references only on demand. Budgets keep each of those layers small:
SKILL.md is a routing index, details live in references read one at a time.
Usage: python3 distribution/skill_budget.py [--check]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
# Approximate tokens as characters / 4 (English Markdown); budgets are in these units.
BUDGETS = {
    "description": 80,     # per Skill, always loaded
    "skill": 4600,         # per SKILL.md, loaded when the Skill applies
    "reference": 6000,     # per reference/asset/prompt file, loaded on demand
}


def tokens(text: str) -> int:
    return (len(text) + 3) // 4


def description(skill_md: Path) -> str:
    _, frontmatter, _ = skill_md.read_text(encoding="utf-8").split("---", 2)
    for line in frontmatter.splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def measure() -> list[tuple[str, str, int]]:
    rows = []
    for skill_md in sorted(SKILLS.glob("*/SKILL.md")):
        rows.append(("description", f"{skill_md.parent.name} (description)", tokens(description(skill_md))))
        rows.append(("skill", str(skill_md.relative_to(ROOT)), tokens(skill_md.read_text(encoding="utf-8"))))
        for path in sorted(skill_md.parent.rglob("*.md")):
            if path != skill_md:
                rows.append(("reference", str(path.relative_to(ROOT)), tokens(path.read_text(encoding="utf-8"))))
    return rows


def main() -> int:
    rows = measure()
    over = [(layer, name, size) for layer, name, size in rows if size > BUDGETS[layer]]
    if "--check" in sys.argv[1:]:
        for layer, name, size in over:
            print(f"{name}: ~{size} tokens exceeds the {layer} budget of {BUDGETS[layer]}; "
                  "move detail into an on-demand reference", file=sys.stderr)
        return 1 if over else 0
    always = sum(size for layer, _, size in rows if layer == "description")
    print(f"always loaded (all descriptions): ~{always} tokens")
    for layer, name, size in sorted(rows, key=lambda row: -row[2]):
        mark = " OVER" if size > BUDGETS[layer] else ""
        print(f"{size:>6}  {layer:<11} {name}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
