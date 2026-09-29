# Agent Factory for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Agent Factory is a Claude Code plugin for Human-directed software delivery. It provides
four Skills: `agent` for managed Work and Verification execution, `convention` for
shared project rules, `document` for authoring and synchronizing project documents, and
`tool`, a catalog of the plugin scripts and the Skill that owns each one.

Version: `{{hostVersion}}`

## Install

```sh
claude plugin marketplace add KoreanLeeChangHyun/agent-factory-claude-plugin
claude plugin install agent-factory@agent-factory
```

Update with `claude plugin marketplace update agent-factory` followed by
`claude plugin update agent-factory@agent-factory`.

The runtime uses the Claude Code CLI and its existing login, including subscription
login. No API key, MCP server or account connection is required. Python 3.10+ is required.

## VS Code extension

The Agent Factory VS Code extension is optional. When the `claude` CLI is available, it
installs or updates this plugin at the extension's semantic base version.

## Document synchronization

Project specification documents in `docs/skills/` are synchronized to `.claude/skills/` and
`.codex/skills/` by the Document Skill's `sync_documents.py`, making them available as
project Skills. Synchronized output must not be edited directly; independent edits are
reported as conflicts.

## License

MIT License. See [LICENSE](LICENSE).

## Source

This repository is generated. Do not edit it directly; changes are made in
[{{source}}]({{source}}) and published with `distribution/port.py`.
