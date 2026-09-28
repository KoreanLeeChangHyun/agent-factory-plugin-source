# Agent Factory plugin source

This repository is the single source for the Agent Factory plugin. Host
distributions are generated from it and must not be edited directly.

| Path | Contents |
|---|---|
| `skills/` | Host-neutral Skill documents (`agent`, `convention`, `document`) |
| `runtime/storage/` | Runtime home, paths, persistence, public state, errors and migration |
| `runtime/system/` | OS seams and process lifecycle: transport, containment, sandbox, Windows/macOS |
| `runtime/execution/` | One agent turn: CLI, requested options, policy, prompts, images, usage, streaming, worktrees, lessons |
| `runtime/tasks/` | Task modes, bindings, announcements, plan receipts and loop progress |
| `runtime/contracts/` | Receipt and capability contracts and their preflight |
| `runtime/runs/` | Run lifecycle: records, worker launch, attempts, and inspection/control commands |
| `runtime/adapters/` | Provider adapters (`codex`, `claude`, `antigravity`) |
| `scripts/` | Command entrypoints (`exec.py`, `loop.py`, document and lesson tools) |
| `distribution/package.json` | Shared name, version, description and host repositories |
| `distribution/hosts/<host>/` | Host-only manifests and README templates |
| `distribution/port.py` | Generator for every host distribution |
| `tests/` | Test suite for the source tree and generated layouts |

## Generate host distributions

```sh
python3 distribution/port.py --out dist            # every host into dist/<host>/
python3 distribution/port.py --host claude --out ../agent-factory-claude-plugin
python3 distribution/port.py --host codex --out ../agent-factory-codex-plugin --check
```

Generated hosts:

- Codex: <https://github.com/KoreanLeeChangHyun/agent-factory-codex-plugin>
- Claude Code: <https://github.com/KoreanLeeChangHyun/agent-factory-claude-plugin>

Adding a host means adding `distribution/hosts/<host>/` templates and a
`hosts.<host>` entry in `distribution/package.json`; the payload stays shared.

## Test

```sh
python -m pip install -r requirements.txt
python -m pytest tests -n auto
```
