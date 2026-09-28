"""Claude print adapter. The parent runtime owns persistence and process containment.

Execution policies map to the nearest Claude permission mode (see policy.PERMISSION_MODES);
Claude tool permissions are not an implementation of Codex's filesystem/network sandbox.
Launch arguments live in command.py and stream translation in events.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from adapters.claude.command import (  # noqa: F401 - re-exported for callers and tests
    EXECUTE_REQUEST, PLAN_MODES, PLAN_REQUEST, build_command, cli_command, planning_phases,
)
from adapters.claude.events import Events  # noqa: F401
from adapters.claude.policy import validate
from execution.prompts import PromptParts
from storage import paths as runtime_paths
from storage.errors import ContractError
from storage.files import safe_read_json


def finish_planning(state, structured, *, execute_next):
    """Mirror the Codex Plan contract: keep plan.json, stop on a decision, record plan-only completion."""
    from tasks.plan_receipt import record_plan, record_plan_only_receipt
    text = str(structured.get("resultText", "")).strip()
    if structured.get("status") == "failed" or not text:
        raise ValueError("Claude planning result is invalid")
    decision = structured.get("status") == "needs-human-decision"
    record_plan(state, {"status": "needs-human-decision" if decision else "planned", "plan": text})
    terminal = None
    if decision:
        terminal = {"status": "needs-human-decision", "resultPath": state["resultPath"], "resultText": text,
                    "decisionKind": "clarification"}
    elif not execute_next:
        # Plan mode cannot write files. The host records only read-only completion.
        record_plan_only_receipt(state)
        terminal = {"status": "completed", "resultPath": state["resultPath"], "resultText": text}
    elif safe_read_json(Path(state["statePath"])).get("cancelRequested"):
        return False
    if terminal is not None:
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(terminal, ensure_ascii=False)}})
        return False
    emit({"type": "native.commentary", "text": "Planning is complete. Implementation is starting in the same Work session."})
    return True


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def main():
    process = None
    try:
        state = safe_read_json(Path(sys.argv[1]))
        runtime_paths.bind(state["runtimeBinding"])
        session = safe_read_json(Path(sys.argv[2]))
        validate({**session, **state.get("executionOptions", {})})
        parts = PromptParts.decode(sys.stdin.read())
        # The host's Codex thread and Claude Code nesting markers must not describe this child.
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        phases = planning_phases(state)
        session_id = session.get("sessionId")
        for index, phase in enumerate(phases):
            command, message = cli_command({**session, "sessionId": session_id}, state, parts, phase)
            # No new session/group: parent runtime containment must include all descendants.
            process = subprocess.Popen(command, cwd=session.get("workingDirectory", session["projectRoot"]),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, text=True,
                                       encoding="utf-8", env=environment)
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.close()
            events = Events(session_id, terminal=phase != "plan", request_id=message["uuid"])
            for line in process.stdout:
                translated = events.translate(json.loads(line))
                # A resumed execution phase re-announces the same thread; the runtime saw it already.
                if index and translated and translated[0].get("type") == "thread.started":
                    translated = translated[2:]
                for event in translated:
                    emit(event)
            code = process.wait()
            if not events.acknowledged:
                raise ValueError(f"Claude exited with {code} before acknowledging the current request")
            if code != 0 or not events.finished:
                raise ValueError(f"Claude exited with {code}; terminal result received: {events.finished}")
            session_id = events.session
            process.stdout.close()
            if phase == "plan" and not finish_planning(state, events.structured, execute_next=len(phases) > 1):
                return 0
        return 0
    except (OSError, ValueError, KeyError, ContractError) as error:
        emit({"type": "error", "message": str(error)})
        return 1
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
