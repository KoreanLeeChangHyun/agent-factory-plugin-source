"""Reproducible fixture comparison; launches no agents or model sessions.

The compression arm is an ideal lossless retained-record fixture, not a model
compression implementation. Only Python execution time is measured here.
"""
import argparse
import hashlib
import json
import time
from pathlib import Path

from execution.handoff import configure, transition
from execution.usage import context_observation
from storage.errors import ContractError


def workload(cycles):
    return [{"id": f"{kind}-{cycle}", "kind": kind,
             "reference": f"fixture/original/{kind}/{cycle}",
             "text": (f"{kind}: revision {cycle}; authorization applies only to this revision.\n" +
                      ("long conversation context preserved.\n" * 256 if kind == "message" else ""))}
            for cycle in range(cycles) for kind in ("message", "goal", "decision", "approval", "late-result")]


def compare(prepare, target, cycles, repetition):
    inputs = workload(cycles)
    digest = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    start = time.perf_counter()
    baseline = {item["id"]: item for item in inputs}
    baseline_seconds = time.perf_counter() - start
    state = configure(sessions={"A": "fixture-a", "B": "fixture-b"}, prepare_percent=prepare,
                      target_percent=target, max_age_seconds=30,
                      references=["shared-skills", "task-original", "decision-original", "approval-original"])
    now = "2026-10-07T18:00:00+00:00"
    rejected = {"unready": 0, "staleDelta": 0, "inFlightTool": 0, "preparationFailure": 0}

    def act(action_type, **values):
        nonlocal state
        state = transition(state, {"type": action_type, "slot": state["owner"], "epoch": state["epoch"], **values})

    def observe(percent):
        observation = context_observation({"type": "provider.context", "usedTokens": percent,
            "contextWindowTokens": 100, "session_id": state["sessions"][state["owner"]], "turn_id": "turn"},
            provider="fixture", observed_at=now)
        act("observe", now=now, turnId="turn", observation=observation)

    def refusal(case):
        try:
            act("switch", now=now)
        except ContractError:
            rejected[case] += 1
        else:
            raise AssertionError(f"Unsafe handoff accepted: {case}")

    start = time.perf_counter()
    for cycle in range(cycles):
        group = inputs[cycle * 5:(cycle + 1) * 5]
        for item in group[:-1]:
            act("event", id=item["id"], reference=item["reference"], kind=item["kind"])
        act("turn-start", id="turn")
        observe(prepare)
        standby = state["preparation"]["slot"]
        refusal("unready")
        act("tool-start", id="tool")
        refusal("inFlightTool")
        act("tool-end", id="tool")
        act("ready", slot=standby, cursor=state["cursor"], references=state["references"], receipt="fixture/read")
        item = group[-1]
        act("event", id=item["id"], reference=item["reference"], kind=item["kind"])
        act("turn-end", id="turn")
        observe(target)
        refusal("staleDelta")
        act("prepare-failed", slot=standby, receipt="fixture/preparation-failure")
        refusal("preparationFailure")
        act("ready", slot=standby, cursor=state["cursor"], references=state["references"], receipt="fixture/caught-up")
        state = json.loads(json.dumps(state))  # Restart from retained state.
        act("switch", now=now)
        for item in group:
            act("processed", id=item["id"])
            # Repeated delivery has no duplicate effect.
            act("event", id=item["id"], reference=item["reference"], kind=item["kind"])
    elapsed = time.perf_counter() - start
    assert len(state["events"]) == len(baseline) == len(state["processed"])
    assert {event["id"]: event["reference"] for event in state["events"]} == {
        key: item["reference"] for key, item in baseline.items()}
    return {"preparePercent": prepare, "targetPercent": target, "repetition": repetition,
            "inputSha256": digest, "inputCount": len(inputs), "switches": len(state["switches"]),
            "measurementKind": "fixture-simulation", "rejectedUnsafeTransitions": rejected,
            "fixtureIntegrity": {"missing": 0, "duplicates": 0, "wrongOwnerAssignments": 0},
            "compressionFixture": {"pythonElapsedSeconds": baseline_seconds, "retainedOriginals": len(baseline)},
            "handoffFixture": {"pythonElapsedSeconds": elapsed, "retainedOriginals": len(state["events"])},
            "modelMeasurements": {"userWaitSeconds": None, "preparationSeconds": None, "elapsedSeconds": None,
                "goalAccuracy": None, "decisionAccuracy": None, "approvalAccuracy": None, "progressAccuracy": None,
                "allocationErrors": None, "rework": None, "inputTokens": None, "outputTokens": None,
                "cachedInputTokens": None, "reasoningOutputTokens": None, "retryUsage": None}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=6)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.cycles < 1 or args.repetitions < 1:
        parser.error("cycles and repetitions must be positive")
    report = {"schemaVersion": 1, "kind": "dual-orchestrator-fixture-comparison",
              "accounting": "No model calls. Cache/reasoning are subsets; model usage, retry usage and user latency remain null.",
              "baseline": "Ideal retained-record fixture; not measured provider compaction.",
              "cases": [compare(p, t, args.cycles, r) for p, t in ((60, 80), (70, 85), (80, 90))
                        for r in range(1, args.repetitions + 1)]}
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
