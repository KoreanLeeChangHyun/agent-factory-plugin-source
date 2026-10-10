#!/usr/bin/env python3
"""Project work-area domains and worker memberships shared by the Human (control center) and Main."""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from execution.cli import JsonArgumentParser, add_project_argument  # noqa: E402
from storage import paths as runtime_paths  # noqa: E402
from storage.errors import ContractError  # noqa: E402
from storage.files import emit, error_document  # noqa: E402
from tasks import domains  # noqa: E402


def build_parser():
    parser = JsonArgumentParser(prog="domains.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, help_text, writes=True):
        sub = commands.add_parser(name, help=help_text)
        add_project_argument(sub)
        if writes:
            sub.add_argument("--actor", choices=domains.ACTORS, required=True,
                             help="human for control-center edits; ai for Main. Main may not change Human edits")
            sub.add_argument("--source", required=True, help="Evidence for the change, such as the Main run ID")
            sub.add_argument("--expected-revision", type=int, help="Fail with domain_conflict if the list changed since this revision")
        return sub
    command("list", "Read the project's domains, worker memberships and recent change history", writes=False)
    create = command("create", "Create a domain; for ai an existing matching name is reused")
    named = create.add_mutually_exclusive_group(required=True)
    named.add_argument("--name")
    named.add_argument("--placeholder", action="store_true",
                       help="Human only: create an independent domain provisionally named 미분류, renamed later")
    rename = command("rename", "Rename a domain; former names stay linked to recorded tasks")
    rename.add_argument("--domain-id", required=True)
    rename.add_argument("--name", required=True)
    assign = command("assign", "Place a worker in a domain, or --unclassified")
    assign.add_argument("--agent", required=True)
    target = assign.add_mutually_exclusive_group(required=True)
    target.add_argument("--domain-id")
    target.add_argument("--unclassified", action="store_true")
    remove = command("remove-worker", "Human only: hide a stopped worker from the worker list; its records are preserved")
    remove.add_argument("--agent", required=True)
    link = command("link", "Main: reuse or create the named domain and place the worker unless the Human placed it")
    link.add_argument("--agent", required=True)
    link.add_argument("--name", required=True)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        binding = runtime_paths.resolve(args.project_root, home=args.runtime_home, project_id=args.project_id)
        if not binding["registered"]:
            raise ContractError("project_uninitialized", "project has no registered runtime")
        root = Path(binding["runtimeRoot"])
        if args.command == "list":
            result = {"domains": domains.read(root)}
        elif args.command == "create":
            result = domains.create(root, args.name, args.actor, args.source, args.expected_revision, args.placeholder)
        elif args.command == "rename":
            result = domains.rename(root, args.domain_id, args.name, args.actor, args.source, args.expected_revision)
        elif args.command == "assign":
            result = domains.assign(root, args.agent, None if args.unclassified else args.domain_id,
                                    args.actor, args.source, args.expected_revision)
        elif args.command == "remove-worker":
            result = domains.remove_worker(root, args.agent, args.actor, args.source, args.expected_revision)
        else:
            if args.actor != "ai":
                raise ContractError("domain_actor_invalid", "link is Main's dispatch path; the Human uses assign")
            result = domains.link_dispatch(root, args.agent, args.name, args.source)
            result["domains"] = domains.read(root)
        emit({"schemaVersion": 1, "kind": "project-domains-result", "command": args.command, **result}, sys.stdout)
        return 0
    except (ContractError, OSError, ValueError) as error:
        emit(error_document(getattr(error, "code", "domains_failed"), str(error)), sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
