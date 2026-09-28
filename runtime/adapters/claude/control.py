"""Claude stop and Goal controls. Claude print runs have no native pause or Goal."""
from storage.errors import ContractError


def before_stop(state_path, state, *, cancel=False):
    # Nothing to negotiate: containment sends SIGTERM to the whole process group (Claude and its
    # tools included) and waits PROCESS_TERM_TIMEOUT before SIGKILL, so Claude can close its session.
    pass


def goal_command(runtime, args, root, session):
    if args.action == "get":
        runtime.emit({"schemaVersion": runtime.schema_version, "kind": "goal", "agentId": args.agent, "goal": None})
        return 0
    raise ContractError("claude_feature_unsupported", "Claude does not support native Goal controls")
