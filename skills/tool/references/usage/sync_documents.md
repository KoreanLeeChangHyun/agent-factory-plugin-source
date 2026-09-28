# `sync_documents.py` usage

Generated from `--help` by `distribution/tool_usage.py`; do not edit by hand.
Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).

```text
usage: sync_documents.py [-h] --project-root PROJECT_ROOT [--host {codex,claude,all}]
                         [--reconcile]

Synchronize .codex/skills and .claude/skills from the authoritative docs/skills source.

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --host {codex,claude,all}
                        Destination host; default synchronizes every host.
  --reconcile           Back up conflicting or interrupted output, then rebuild it from
                        docs/skills.
```
