# Agent Factory plugin source

This repository is the single source for the Agent Factory plugin. Host
distributions are generated from it and must not be edited directly.

| Path | Contents |
|---|---|
| `skills/` | Host-neutral Skill documents (`agent`, `convention`, `document`, `tool`) |
| `runtime/storage/` | Runtime home, paths, persistence, public state, errors and migration |
| `runtime/system/` | OS seams and process lifecycle: transport, containment, sandbox, Windows/macOS, pinned Archify renderer |
| `runtime/execution/` | One agent turn: CLI, requested options, policy, prompts, images, usage, streaming, worktrees, Goal state, Interview events, document context, lesson capture and facts |
| `runtime/tasks/` | Task modes, bindings, announcements, allocation and model recommendation (`model-affinity.json`), preparation, plan receipts, loop progress, task history, decisions, revisions, operation records and supervision, Work Units, integration checks and commits |
| `runtime/conductor/` | Conductor domain: chat routing, the SQLite queue ledger, pool admission, performers, roles, responsibility, intents, workflows and Work Unit adapters, the queue runtime, work-area domains, team roster and the control-center view |
| `runtime/hooks/` | Host `PreToolUse` hooks: the orchestrator guard and the Work sub-agent guard |
| `runtime/contracts/` | Receipt and capability contracts and their preflight |
| `runtime/runs/` | Run lifecycle: records, worker launch, attempts, and inspection/control commands |
| `runtime/adapters/` | Provider adapters (`codex`, `claude`, `antigravity`) |
| `scripts/` | Command entrypoints: `exec.py`, `loop.py`, `conductor.py`, `coordination.py`, `commit.py`, `domains.py`, `operation_records.py`, `lessons.py`, `evidence.py` (screen captures and check baselines), `archify.py` and the document tools |
| `distribution/package.json` | Shared name, version, description and host repositories |
| `distribution/hosts/<host>/` | Host-only manifests and README templates |
| `distribution/port.py` | Generator for every host distribution |
| `distribution/release.py` | Release coordinator across the plugin source and every host checkout (`plan`, `commit`, `push`) |
| `distribution/tool_usage.py` | Generates `skills/tool/references/usage/` from each script's argparse definition (`--check`) |
| `distribution/skill_budget.py` | Reports and enforces Skill document token budgets (`--check`) |
| `tests/` | Test suite for the source tree and generated layouts |

## Runtime architecture

![You talk only to the Conductor. It writes a work request; performers carry it out and write a work report; the Conductor compares the report with the Done criteria before it answers you. A Verifier checks the work only when you ask.](https://raw.githubusercontent.com/KoreanLeeChangHyun/agent-factory-vscode-extension/main/static/images/readme-conductor.png)

The conductor–performer flow above runs on these layers. Entry scripts parse commands;
everything below them is shared by every host distribution.

```mermaid
flowchart TB
  accTitle: Plugin runtime layers
  accDescr: Command scripts call the conductor, task and run layers. Runs execute one agent turn through the execution layer and a provider adapter, which starts the Codex, Claude Code or Antigravity CLI. Storage and system seams sit under every layer, and contracts define receipts and capabilities.
  S["scripts/<br/>exec.py · loop.py · conductor.py · domains.py · lessons.py · archify.py"]
  C["runtime/conductor/<br/>routing · queue · domains · team roster"]
  T["runtime/tasks/<br/>modes · allocation · loops · decisions · Work Units"]
  R["runtime/runs/<br/>run records · worker launch · attempts"]
  E["runtime/execution/<br/>one agent turn · prompts · usage · lesson facts"]
  A["runtime/adapters/<br/>codex · claude · antigravity"]
  CLI["Codex · Claude Code · Antigravity CLIs"]
  K["runtime/contracts/<br/>receipts · capabilities"]
  B["runtime/storage/ · runtime/system/<br/>runtime home · files · processes · sandbox"]
  H["runtime/hooks/<br/>host PreToolUse guards"]
  S --> C & T & R
  C --> T --> R --> E --> A --> CLI
  R --> K
  E -.-> B
  R -.-> B
  CLI -.-> H
```

A delegated task, end to end:

```mermaid
sequenceDiagram
  accTitle: A delegated task
  accDescr: Main starts a loop with a work request; the runtime launches a Work run on the provider CLI, records its events, result and receipt, and Main reads the result, compares it with Done and reports.
  participant M as Main (conductor)
  participant L as loop.py / exec.py
  participant R as runtime runs
  participant W as Work session (CLI)
  M->>L: start --work-agent … (work request)
  L->>R: accept run, bind task and domain
  R->>W: launch turn with the request
  W-->>R: events, result.md, receipt.json
  R-->>L: loop state and completion notice
  L-->>M: result, receipt, changed paths
  M->>M: compare with Done, then revise or report
```

## How distributions are produced

```mermaid
flowchart LR
  accTitle: Plugin distribution flow
  accDescr: The shared payload of skills, runtime and scripts is combined with each host's templates by port.py to generate the Codex, Claude Code and Antigravity plugin repositories.
  P["Shared payload<br/>skills · runtime · scripts"] --> G["distribution/port.py"]
  T["distribution/hosts/&lt;host&gt;/<br/>manifests and README templates"] --> G
  G --> X["Codex plugin"]
  G --> Y["Claude Code plugin"]
  G --> Z["Antigravity plugin"]
```

## Generate host distributions

```sh
python3 distribution/port.py --out dist            # every host into dist/<host>/
python3 distribution/port.py --host claude --out ../agent-factory-claude-plugin
python3 distribution/port.py --host codex --out ../agent-factory-codex-plugin --check
```

Generated hosts:

- Codex: <https://github.com/KoreanLeeChangHyun/agent-factory-codex-plugin>
- Claude Code: <https://github.com/KoreanLeeChangHyun/agent-factory-claude-plugin>
- Antigravity: <https://github.com/KoreanLeeChangHyun/agent-factory-antigravity-plugin>

Adding a host means adding `distribution/hosts/<host>/` templates and a
`hosts.<host>` entry in `distribution/package.json`; the payload stays shared.

## Test

```sh
python -m pip install -r requirements.txt
python -m pytest tests -n auto --maxprocesses=4 --dist=worksteal
python -m ruff check --no-cache
```

- Run a small selection serially with `-n 0`. `tests/benchmarks/` holds performance
  measurements that are run explicitly.
- After adding or changing a script or its arguments, run
  `python3 distribution/tool_usage.py` to regenerate `skills/tool/references/usage/`.
