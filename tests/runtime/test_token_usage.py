"""Usage accounting must not charge restored history or repeated notifications."""
import runtime_test_home  # noqa: F401 - imported for its side effect: isolates the runtime home
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from native_fixtures import native_fixture, runtime
from execution.usage import (UsageAccumulator, record_attempt, compare_orchestration,
                             context_observation, fresh_context_percent)


def native_event(total, last, **extra):
    def count(n):
        return dict(inputTokens=n, cachedInputTokens=n // 2, outputTokens=n // 10,
                    reasoningOutputTokens=n // 20)
    return {"type": "token.usage", "tokenUsage": {"total": count(total), "last": count(last)}, **extra}


class TokenUsageTests(unittest.TestCase):
    def test_context_observation_requires_fresh_matching_session_and_turn(self):
        event = {"type": "provider.context", "usedTokens": 800, "contextWindowTokens": 1000,
                 "session_id": "thread", "turn_id": "turn", "estimated": True,
                 "source": "thread/tokenUsage/updated"}
        at = "2026-10-07T18:00:00+00:00"
        observation = context_observation(event, provider="codex", observed_at=at)
        def percent(**changes):
            return fresh_context_percent(observation, **{
                "now": at, "max_age_seconds": 30, "session_id": "thread", "turn_id": "turn", **changes})
        self.assertEqual(percent(), 80)
        self.assertIsNone(percent(now="2026-10-07T18:00:31+00:00"))
        self.assertIsNone(percent(now="2026-10-07T17:59:59+00:00"))
        self.assertIsNone(percent(session_id="other"))
        self.assertIsNone(percent(turn_id="next"))
        self.assertIsNone(percent(max_age_seconds=0))
        self.assertTrue(observation["estimated"])
        self.assertIsNone(observation["providerVersion"])
        for value in (None, 0, -1, True):
            unknown = context_observation({**event, "contextWindowTokens": value}, provider="codex", observed_at=at)
            self.assertEqual(unknown["availability"], "unknown")
            self.assertIsNone(unknown["usedPercent"])

    def test_comparison_keeps_subsets_retries_and_unknown_observations_separate(self):
        states = {
            'main.json': {'agentId': 'main', 'runId': 'one', 'role': 'main', 'attempt': 2,
                          'tokenUsage': dict(inputTokens=100, outputTokens=20, cachedInputTokens=40, reasoningOutputTokens=10),
                          'usageAttempts': {'1': {'inputTokens': 60}, '2': {'inputTokens': 40}}},
            'work.json': {'agentId': 'worker', 'runId': 'one', 'role': 'work'}
        }
        arm = {'messages': ['한글😀'], 'runStatePaths': list(states), 'detailReads': [{'runId': 'one', 'reason': 'constraint'}]}
        document = {'schemaVersion': 1, 'cases': [{'id': 'dependent', 'input': 'same', 'completionCriteria': 'same result',
                    'before': arm, 'after': {**arm, 'messages': ['한글😀', 'more']}}]}
        result = compare_orchestration(document, lambda path: states[str(path)])['cases'][0]
        self.assertEqual(result['before']['reportedModelUsageByRole']['main']['inputTokens'], 100)
        self.assertEqual(result['before']['reportedModelUsageByRole']['main']['outputTokens'], 20)
        self.assertIsNone(result['before']['reportedModelUsageByRole']['work']['inputTokens'])
        self.assertEqual(result['before']['runs'][0]['retryCount'], 1)
        self.assertEqual(result['staticUtf8ByteDifference'], 4)
        self.assertIsNone(result['modelTokenSavingsPercent'])
        self.assertIsNone(result['after']['quality'])
        arm['runStatePaths'].append('main.json')
        with self.assertRaises(runtime.ContractError):
            compare_orchestration(document, lambda path: states[str(path)])

    def test_resume_deduplicates_totals_and_counts_only_current_usage(self):
        usage = UsageAccumulator()
        self.assertTrue(usage.observe(native_event(1100, 100)))
        self.assertFalse(usage.observe(native_event(1100, 100)))
        self.assertTrue(usage.observe(native_event(1350, 250)))
        # A delayed replay must not look like a counter reset and get charged again.
        self.assertFalse(usage.observe(native_event(1100, 100)))
        self.assertEqual(usage.snapshot()["counterDiscontinuities"], 0)
        result = usage.snapshot()
        self.assertEqual(result["inputTokens"], 350)
        self.assertEqual(result["cachedInputTokens"], 175)
        self.assertEqual(result["outputTokens"], 35)
        self.assertEqual(result["reports"], 2)

    def test_counter_reset_does_not_subtract_usage(self):
        usage = UsageAccumulator()
        usage.observe(native_event(1100, 100))
        usage.observe(native_event(200, 200))
        self.assertEqual(usage.snapshot()["inputTokens"], 300)
        self.assertEqual(usage.snapshot()["counterDiscontinuities"], 1)
        # The same totals in another turn can be a genuine reset, not a replay.
        usage.observe(native_event(1100, 100, turn_id="next"))
        self.assertTrue(usage.observe(native_event(200, 200, turn_id="next")))

    def test_cli_optional_counters_are_unknown_and_identified_turns_are_unique(self):
        usage = UsageAccumulator()
        event = {"type": "turn.completed", "turn_id": "one", "usage": {"input_tokens": 100, "output_tokens": 20}}
        self.assertTrue(usage.observe(event))
        self.assertFalse(usage.observe(event))
        self.assertIsNone(usage.snapshot()["cachedInputTokens"])
        self.assertIsNone(usage.snapshot()["reasoningOutputTokens"])
        self.assertEqual(usage.snapshot()["outputTokens"], 20)

    def test_malformed_and_missing_reports_do_not_become_zero_usage(self):
        usage = UsageAccumulator()
        for value in (None, [], {}, {"input_tokens": True, "output_tokens": 3},
                      {"input_tokens": -1, "output_tokens": 3},
                      {"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 3}):
            self.assertFalse(usage.observe({"type": "turn.completed", "usage": value}))
        self.assertEqual(usage.snapshot()["coverage"], "unavailable")
        self.assertIsNone(usage.snapshot()["inputTokens"])
        self.assertFalse(usage.observe(native_event(100, 200)))

    def test_attempt_snapshots_replace_not_add_and_remain_public(self):
        state = {"role": "main"}
        usage = UsageAccumulator()
        usage.observe(native_event(100, 100))
        record_attempt(state, 1, usage.snapshot())
        record_attempt(state, 1, usage.snapshot())
        record_attempt(state, 2, usage.snapshot())
        self.assertEqual(runtime.public_state(state)["tokenUsage"]["inputTokens"], 200)
        record_attempt(state, 3, UsageAccumulator().snapshot())
        self.assertIsNone(state["tokenUsage"]["inputTokens"])
        self.assertEqual(state["usageAttempts"]["1"]["inputTokens"], 100)

    def test_native_bridge_forwards_current_turn_usage_only(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            bridge, rpc, _ = native_fixture(Path(directory), goal=False)
            def notification(thread, turn):
                usage = native_event(1100, 100)["tokenUsage"]
                usage["last"]["totalTokens"] = 110
                usage["modelContextWindow"] = 1000
                return {"method": "thread/tokenUsage/updated", "params": {
                    "threadId": thread, "turnId": turn, "tokenUsage": usage}}
            rpc.events[1:1] = [notification("other", "turn-0"), notification("thread-exact", "old-turn"),
                               notification("thread-exact", "turn-0")]
            bridge.run("bounded request")
            events = [json.loads(line) for line in output.getvalue().splitlines()]
            reports = [event for event in events if event["type"] == "token.usage"]
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0]["turn_id"], "turn-0")
            contexts = [event for event in events if event["type"] == "provider.context"]
            self.assertEqual(len(contexts), 1)
            self.assertEqual(contexts[0]["usedTokens"], 110)
            self.assertEqual(contexts[0]["contextWindowTokens"], 1000)
            self.assertEqual(contexts[0]["session_id"], "thread-exact")
