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

- `exec.py`: one managed agent run.
- `loop.py`: a Work/Verification loop over an announced task list.
- `lessons.py`: lesson records and published rules.
- `sync_documents.py`: continuous `docs/skills/` synchronization to host skill folders.
- `export_documents.py`: one-time copy of `docs/skills/` packages.
- `catalog_documents.py`: the live catalog of project Documents.
- `search_documents.py`: search over that catalog.
- `migrate_document_paths.py`: contract-listed document moves.
- Subcommands and arguments are listed only in `references/usage.md`; rules only in the owner Skill.
