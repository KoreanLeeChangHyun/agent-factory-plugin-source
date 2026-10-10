# `domains.py` usage

Generated from argparse by `distribution/tool_usage.py`; do not edit by hand.
Rules for when and why to run it stay in the owning Skill listed in [SKILL.md](../../SKILL.md).

Every subcommand with options also accepts: `--project-root PROJECT_ROOT`, `--runtime-home RUNTIME_HOME`, `--project-id PROJECT_ID`.

## `list`: Read the project's domains, worker memberships and recent change history

## `create`: Create a domain; for ai an existing matching name is reused
Required: `--actor {human,ai}`, `--source SOURCE`
One of: `--name` | `--placeholder`
- `--expected-revision EXPECTED_REVISION`: Fail with domain_conflict if the list changed since this revision
- `--name NAME`
- `--placeholder`: Human only: create an independent domain provisionally named 미분류, renamed later

## `rename`: Rename a domain; former names stay linked to recorded tasks
Required: `--actor {human,ai}`, `--source SOURCE`, `--domain-id DOMAIN_ID`, `--name NAME`
- `--expected-revision EXPECTED_REVISION`: Fail with domain_conflict if the list changed since this revision

## `assign`: Place a worker in a domain, or --unclassified
Required: `--actor {human,ai}`, `--source SOURCE`, `--agent AGENT`
One of: `--domain-id` | `--unclassified`
- `--expected-revision EXPECTED_REVISION`: Fail with domain_conflict if the list changed since this revision
- `--domain-id DOMAIN_ID`
- `--unclassified`

## `remove-worker`: Human only: hide a stopped worker from the worker list; its records are preserved
Required: `--actor {human,ai}`, `--source SOURCE`, `--agent AGENT`
- `--expected-revision EXPECTED_REVISION`: Fail with domain_conflict if the list changed since this revision

## `link`: Main: reuse or create the named domain and place the worker unless the Human placed it
Required: `--actor {human,ai}`, `--source SOURCE`, `--agent AGENT`, `--name NAME`
- `--expected-revision EXPECTED_REVISION`: Fail with domain_conflict if the list changed since this revision
