# `migrate_document_paths.py` usage

Generated from `--help` by `distribution/tool_usage.py`; do not edit by hand.
Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).

```text
usage: migrate_document_paths.py [-h] --project-root PROJECT_ROOT --operations OPERATIONS
                                 [--task-id TASK_ID] [--backup | --apply]
                                 [--backup-dir BACKUP_DIR]

Preview, back up, and safely apply contract-listed document path moves.

options:
  -h, --help            show this help message and exit
  --project-root PROJECT_ROOT
  --operations OPERATIONS
  --task-id TASK_ID
  --backup              Create and verify a backup after preview.
  --apply               Apply only after a matching backup exists.
  --backup-dir BACKUP_DIR
```
