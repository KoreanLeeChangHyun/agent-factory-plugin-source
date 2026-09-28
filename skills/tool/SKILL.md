---
name: tool
description: Find which Agent Factory plugin script performs an operation and where its rules live. Use to choose a plugin tool, not for ordinary conversation or general-purpose CLIs.
metadata:
  specification-id: tool
---

# Agent Factory Tool

- `<plugin-root>` is the installed Agent Factory plugin directory that contains `skills/`,
  `runtime/` and `scripts/`; it is two levels above this `SKILL.md`.
- This is a catalog only. Before running a tool, follow the owning Skill's rules; they
  remain the single source for arguments, authority and failure handling.
- Run every tool as `python3 <plugin-root>/scripts/<script> ...`.
- `references/usage.md`: every script's and subcommand's arguments, generated from its
  `--help`. Read only the section for the tool you are about to run.

<a id="catalog"></a>

## 1. Plugin tools

| Script | Owner |
|---|---|
| `exec.py` | [Agent](../agent/SKILL.md) |
| `loop.py` | [Agent](../agent/SKILL.md) |
| `lessons.py` | [Document](../document/references/lessons-learned.md#lifecycle-cli) |
| `sync_documents.py` | [Document](../document/SKILL.md#continuous-codex-synchronization) |
| `export_documents.py` | [Document](../document/SKILL.md#explicit-codex-export) |
| `catalog_documents.py` | [Document](../document/SKILL.md) |
| `search_documents.py` | [Document](../document/SKILL.md) |
| `migrate_document_paths.py` | [Document](../document/SKILL.md) |

- `exec.py`: start, message, inspect, cancel and reconcile one managed agent run
  (`submit`, `send`, `status`, `result`, `cancel`, `list`, `inbox`, `doctor`, `announce-tasks`).
- `loop.py`: drive a Work/Verification loop over an announced task list
  (`start`, `status`, `reconcile`, `recover-receipt`, `skip`, `close`, `refresh-progress`).
- `lessons.py`: record, resolve, retrieve and audit lessons; publish evaluated rules.
- `sync_documents.py`: synchronize `docs/skills/` into `.codex/skills/` and `.claude/skills/`.
- `export_documents.py`: preview or apply a one-time copy of `docs/skills/` packages.
- `catalog_documents.py`: build the live catalog of project Documents.
- `search_documents.py`: search that catalog.
- `migrate_document_paths.py`: preview, back up and apply contract-listed document moves.
