# Agent Factory plugin source

This repository is the single source for the Agent Factory plugin. Host
distributions are generated from it and must not be edited directly.

| Path | Contents |
|---|---|
| `skills/` | Host-neutral Skill documents (`agent`, `convention`, `document`) |
| `runtime/` | Python runtime modules, including provider adapters |
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
