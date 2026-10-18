# Agent Factory for Codex

English | [한국어](README.ko.md)

Human-facing responses support the language the Human uses or explicitly selects.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Agent Factory is a Codex plugin for Human-directed software delivery. One conductor
talks with you, hands bounded work to performers, checks every report against what you
asked for, and keeps the project's documents, contracts and lessons.

## How it works

![You talk only to the Conductor. It writes a work request; performers carry it out and write a work report; the Conductor compares the report with the Done criteria before it answers you. A Verifier checks the work only when you ask.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-conductor.png)

- **Conductor (Main).** Converses, plans, interviews and routes. By default
  (orchestrator mode) it delegates every change and research task; in direct mode it
  implements itself.
- **Performers (Work profiles).** **Expert** (`work`) for open-ended changes, **Worker**
  (`workLight`) for bounded, already-decided changes, **Explorer** (`explore`) for
  read-only research and **Scribe** (`scribe`) for documents.
- **Work request and work report.** The request (goal, scope, Done criteria) is the
  only handoff to a performer; the report returns the result, changed paths and the
  performer's own checks.
- **Result alignment.** Before reporting, the conductor compares each report with its
  Done criteria and sends a mismatch back to the same task.
- **Verification on request.** A separate Verification agent checks the work only when
  the selected route calls for it; findings return to Work until it passes.
- **Domains.** Every performer belongs to a named work area; follow-up work in a domain
  goes to its performers (`domains.py`).

## Core features

### 1. Document system

![Documents, interviews and lessons: sources become research, accepted research becomes Specification, and Specification is synchronized into project Skills that agents load. Interviews record decisions; agents record lessons and contracts; supported lessons are consolidated into rules.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-knowledge.png)

- Keep project knowledge in a structured, searchable collection that agents can
  consult across tasks. Maintain one editable source for each document.
- Separate source references (**Original**), investigations and working knowledge
  (**Refined**), accepted facts and rules (**Specification**), execution records
  (**Progress**), and experience from previous work (**Lessons Learned**).
- Preserve the distinction between evidence, assumptions, and accepted decisions.
  Writing or summarizing a document does not automatically make it a project rule.
- When you request migration, organize existing documents into the project structure
  while preserving their content and references. Accepted Specification documents
  are synchronized into project Skills for agents to use in later work.

### 2. Contracts → Work–Verification loop

![Contract and loop: the Conductor returns a versioned contract; the Expert works and reports; a Verifier checks against Done and findings go back to the Expert until it passes; the Conductor reports with the contract record.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-loop.png)

- Turn a conversation into a work contract with six sections: contract information,
  hired workers and their IDs, task goals with assigned worker IDs and completion criteria,
  important constraints including task dependencies, a file tree showing each change
  operation and file-specific goal beside its task ID, and a task-level order diagram.
- Each contract version at `docs/progress/<contract-id>/contract-v<N>.md` also holds
  its execution record: runs, agents, Work and Verification status, and evidence per
  task. Revisions create a new version and preserve earlier ones.
- Verification findings return to Work for correction, and revised work returns
  to Verification for another check. Unresolved findings remain visible.
- Choose the execution route for each message. Direct Main work and Work-only execution
  are also available; a separate Verification agent runs only when the selected
  route calls for it.

### 3. Interviews

- Resolve missing requirements and decisions through guided questions in the
  conversation. The agent uses existing context first and asks about gaps that
  could materially change the outcome.
- Address one decision at a time, with meaningful options, their advantages and
  disadvantages, and a recommendation. Your answers guide the next question.
- Finish with a summary of the decisions and any remaining gaps, saved as a document
  when requested and used to inform a work contract or further planning.

### 4. Lessons learned

- Preserve errors, including recovered failures, alongside differences between
  your judgment and the agent's, with their context, known causes, attempted
  solutions, and actual outcomes.
- Retrieve relevant lessons before related work and record whether each applied lesson
  helped, needed correction, or failed again.
- The runtime keeps observed facts for each run (tool failures, failed runs, rework,
  failed Verification and your corrections) and queues the run for lesson writing.
  Main can add a fact it observed with `lessons.py note`; `lessons.py pending` lists or
  closes queued runs, and `lessons.py brief` drafts a lesson-writing request for them.
- When you request consolidation, supported lessons become reusable project rules.
  Lesson records remain evidence; they never become rules on their own.

## VS Code extension

![The Agent Factory VS Code extension: the Control Center shows performers by domain and the work report of one execution.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-control-center.png)

- This plugin is fully installable and usable on its own; the VS Code extension is optional.
- The Agent Factory VS Code extension requires this plugin to be installed and
  enabled at the identical semantic base version. For example, extension `{{version}}`
  accepts plugin `{{version}}+codex.<token>`.
- On activation, the extension checks whether the plugin is installed, enabled, and compatible,
  and attempts automatic installation when needed. An already active, compatible plugin is used without reinstalling.
  If compatibility cannot be confirmed afterward, activation stops with an error.
- To install or update it yourself, see [Manual installation](#manual-installation) below.

## Independent components

- **Extension + plugin:** Main, Work, Verification and exec/loop provide the complete local workflow without Agent Factory MCP. No MCP package, server, account, tenant, connection or authenticated resource is required. The plugin also works without the extension.
- **MCP:** The service is independently installed and operated; it is not a dependency of the local plugin workflow.

## Skills

The plugin exposes four public Skills:

- [Agent](skills/agent/SKILL.md): Dispatches Work and Verification agents and manages
  their sessions, execution progress, and results.
- [Convention](skills/convention/SKILL.md): Provides shared rules for communication,
  user decisions, development, testing, research, and interviews.
- [Document](skills/document/SKILL.md): Guides project document writing, organization,
  storage, and search, and synchronizes project specifications to Codex Skills.
- [Tool](skills/tool/SKILL.md): Lists the plugin scripts and points to the Skill that owns each one.

### Agent execution

- For each message, you can select Work (delegate a task), Plan (create a plan only),
  or Verification (check existing work), or combine them as Plan·Work,
  Work·Verification or Plan·Work·Verification. The selection applies only to that
  message. See the [execution guide](skills/agent/references/execution-modes.md).
- With a task type and the host's detected model catalog, `loop.py start --model-catalog-file`
  recommends a Work model from your own `model-affinity.json` in the project runtime root.
  Only detected models are chosen and a model you specify is kept. See
  [task allocation](skills/agent/references/task-allocation.md#model-recommendation).
- `loop.py handoff` moves a running or stopped loop's Work task to another agent and model.
  The new session receives the original request, progress, changed paths, remaining work
  and open findings; the earlier session stays read-only. When a provider's usage limit
  stops a task, Main hands it to another provider itself (`--trigger quota`) and tells you.
- `operation_records.py` provides read-only lookup (`query`) and word search (`search`) over
  tasks, sessions, decisions, observations and lessons, with each hit citing its source.
  `supervise` classifies unfinished loops as normal, delayed, stuck or decision-needed.

### Models and sessions

- Agents can also run on Claude Code: choose a `claude-*` model such as `claude-opus-5-5`,
  `claude-sonnet-5-5`, `claude-fable-5-1` or `claude-haiku-5-5`, or the aliases
  `claude-opus`, `claude-sonnet` and `claude-haiku`.
- Every route works with Claude. Plan runs in Claude's plan mode, then the same session executes.
  Execution permissions map to the nearest Claude permission mode; they are not an OS sandbox.
  See the [execution guide](skills/agent/references/execution-modes.md#7-execution-providers).
- The Antigravity CLI (`agy`) can also run agents with Google AI subscription models: `gemini-*`
  models select it, and its other models are named `antigravity/<id>`. Antigravity runs are
  text-only. See [Antigravity](skills/agent/references/execution-modes.md#71-antigravity).
- This package is installed through Codex, and the runtime launches Claude and Antigravity runs.
  For Claude Code, install the separate [Claude Code distribution](https://github.com/KoreanLeeChangHyun/agent-factory-claude-plugin).

## Manual installation

- The Agent Factory VS Code extension installs the plugin automatically by default.
- To use the plugin on its own or install it manually, run:

  ```bash
  codex plugin marketplace add KoreanLeeChangHyun/agent-factory-codex-plugin --ref main
  codex plugin add agent-factory@agent-factory
  ```

- To install a published update:

  ```bash
  codex plugin marketplace upgrade agent-factory
  codex plugin add agent-factory@agent-factory
  ```

- Start a new Codex thread after installation or update so the Skills and tools are loaded.

## Document synchronization

- Project specification documents written according to Agent Factory rules in `docs/skills/`
  are synchronized to `.codex/skills/`, `.claude/skills/` and `.agents/skills/`, making them
  available to Codex, Claude Code and Antigravity as project Skills.
- After writing, modifying or deleting project specification documents in `docs/skills/`,
  agents run the
  [Document Skill synchronization script](skills/document/references/host-sync.md#continuous-codex-synchronization)
  and check the result.
- If synchronized documents are edited independently, synchronization reports a conflict
  and stops to preserve those changes.
- Existing Skills outside managed synchronization are preserved without modification.
  Document migration may proceed according to Agent Factory rules only when the user
  explicitly requests it, and only within the requested scope.

## Compatibility

- **Operating system:** Execution supports Linux, macOS and native Windows; WSL must meet the Linux requirements.
  On Windows, use a native Python (python.org or Microsoft Store), for example from Git Bash;
  MSYS2/Cygwin Python builds are unsupported. macOS and Windows require validation in your actual environment.
- **Python:** Python 3.10+.
- **Codex:** Codex CLI must be installed. Required capabilities and environment readiness are checked before execution.
- See [host readiness](skills/agent/references/installation.md#host-readiness-and-diagnostics)
  for environment-specific requirements and limitations.

## Bug reports

Please report bugs by email to [m.leechanghyun@gmail.com](mailto:m.leechanghyun@gmail.com).

## License

MIT License. See [LICENSE](LICENSE).

## Source

This repository is generated. Do not edit it directly; changes are made in
[{{source}}]({{source}}) and published with `distribution/port.py`.
