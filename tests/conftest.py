"""Auxiliary check evidence; never a functional E2E completion verdict."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def pytest_report_header(config):
    return "Auxiliary code/API checks only; actual E2E operation, correlated runtime logs and visual review remain unverified."


def pytest_runtest_logreport(report):
    # Opt in to an exact run-owned file. Let only the controller write under xdist;
    # the controller receives each worker report. Never double-count those events,
    # create a new project output hierarchy or copy fixture/credential contents.
    filename = os.environ.get("AF_CHECK_LOG")
    if not filename or os.environ.get("PYTEST_XDIST_WORKER"):
        return
    path = Path(filename)
    event = {"at": datetime.now(timezone.utc).isoformat(), "scope": "plugin.code-check",
             "nodeId": report.nodeid, "phase": report.when, "outcome": report.outcome,
             "durationSeconds": report.duration, "functionalStatus": "unverified"}
    if getattr(report, "worker_id", None):
        event["workerId"] = report.worker_id
    if report.failed:
        event["failure"] = report.longreprtext
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line("Code/API check results only. Functional E2E and direct visual inspection: unverified.")
    if os.environ.get("AF_CHECK_LOG"):
        terminalreporter.write_line(f"Auxiliary check log: {os.environ['AF_CHECK_LOG']}")
