"""Metadata measurements remain separate from model usage and survive run outcomes."""
import runtime_test_home  # noqa: F401

from pathlib import Path
from unittest import mock

from execution.measurement import AttemptMeasurement, save
from execution.usage import record_attempt
from native_fixtures import runtime


def test_metrics_keep_prompt_hashes_and_deduplicate_tools_without_storing_content():
    meter = AttemptMeasurement("fixed secret prompt", "요청", model="model", provider="codex")
    meter.observe({"type": "turn.started", "turn_id": "t1"}, 10)
    event = {"type": "item.completed", "item": {"type": "command_execution", "id": "tool1", "aggregated_output": "결과"}}
    meter.observe(event, 20)
    meter.observe(event, 20)
    value = meter.snapshot()
    assert value["toolCalls"] == 1
    assert value["toolOutputUtf8Bytes"] == 6
    assert value["toolOutputCharacters"] == 2
    assert value["eventUtf8Bytes"] == 50
    assert value["inferenceCalls"] is None
    assert "secret" not in str(value)
    assert value["prompt"]["dynamicUtf8Bytes"] == 6
    assert value["prompt"]["version"] == AttemptMeasurement("fixed secret prompt", "different").snapshot()["prompt"]["version"]
    meter.observe({"type": "turn.started", "turn_id": "t2"}, 10)
    meter.observe(event, 20)
    assert meter.snapshot()["toolCalls"] == 2
    assert meter.snapshot()["providerTurns"] == 2


def test_missing_output_and_inference_observations_stay_unknown():
    meter = AttemptMeasurement("", "")
    assert meter.snapshot()["toolCalls"] is None
    meter.observe({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "a"}}, 10)
    assert meter.snapshot()["toolOutputUtf8Bytes"] is None
    meter.observe({"type": "item.completed", "item": {"type": "mcp_tool_call", "id": "b", "result": "known"}}, 10)
    assert meter.snapshot()["toolOutputUtf8Bytes"] is None


def test_attempt_replacement_retains_failed_and_cancelled_usage_without_double_counting():
    state = {"status": "cancelled"}
    class Storage:
        @staticmethod
        def now():
            return "2026-10-16T12:00:00Z"
        @staticmethod
        def update_json(_path, _lock, update):
            update(state)
    meter = AttemptMeasurement("fixed", "request")
    save(Storage, Path("/run/state.json"), state, 1, meter)
    save(Storage, Path("/run/state.json"), state, 1, meter)
    save(Storage, Path("/run/state.json"), state, 2, meter)
    assert len(state["measurementAttempts"]) == 2
    snapshot = {"reports": 1, "counterDiscontinuities": 0, "inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 10, "reasoningOutputTokens": None}
    record_attempt(state, 1, snapshot)
    record_attempt(state, 2, snapshot)
    record_attempt(state, 2, snapshot)
    assert state["tokenUsage"]["inputTokens"] == 200
    assert state["status"] == "cancelled"
    assert runtime.public_state(state)["measurementAttempts"] == state["measurementAttempts"]


def test_measurement_persistence_failure_does_not_fail_work():
    class Broken:
        @staticmethod
        def now():
            return "2026-10-16T12:00:00Z"
        @staticmethod
        def update_json(*_args):
            raise OSError("disk error")
    with mock.patch("execution.measurement.trace.exception") as failure:
        save(Broken, Path("/run/state.json"), {}, 1, AttemptMeasurement("", ""))
    assert failure.call_args.args[1] == "measurement.persist.failed"
