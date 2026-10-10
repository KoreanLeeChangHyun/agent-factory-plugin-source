import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from storage import paths as runtime_paths
from storage.errors import ContractError
from tasks import model_affinity
from tasks.allocation import validate

NOW = datetime(2026, 10, 10, 14, 30, tzinfo=timezone.utc)
# A table the Human wrote; the runtime ships none.
TABLE = {"schemaVersion": 1, "taskTypes": {
    "research": {"profile": "explore", "preferences": [{"model": "claude-opus-*"}, {"model": "gpt-6.1-sol"}]},
    "small-change": {"profile": "workLight", "preferences": [{"model": "gpt-*-luna"}, {"model": "claude-sonnet-*"}]},
    "design-diagnosis": {"profile": "work", "preferences": [{"model": "claude-opus-*"}, {"model": "gpt-6-astra"}]},
    "documentation": {"profile": "scribe", "preferences": [{"model": "claude-opus-*"}, {"model": "gpt-6.1-sol"}]}}}


def catalog(*ids, fetched="2026-10-10T14:24:14.672438943Z"):
    """The host's modelSelectionCatalog shape; observation times are recorded evidence, never an age limit."""
    return {"schemaVersion": 1, "checkedAt": "2026-10-10T14:29:00Z", "availability": "provider-catalog-observed",
            "candidates": [{"id": model, "provider": model_affinity.provider_of(model), "evidenceKind": "provider-observation",
                            "detail": {"fetchedAt": fetched}} for model in ids]}


class ModelAffinityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Writes are confined to a pinned managed runtime, so the table lives in a real isolated one.
        code = Path(self.temporary.name) / "project"
        code.mkdir()
        self.root = Path(runtime_paths.resolve(code, create=True, home=Path(self.temporary.name) / "home")["runtimeRoot"])
        self.table = self.root / model_affinity.FILE
        self.table.write_text(json.dumps(TABLE), encoding="utf-8")

    def recommend(self, task_type, observed, **options):
        table, reference, problem = model_affinity.load_table(self.root)
        return model_affinity.recommend(observed, table, reference, task_type, problem=problem, clock=NOW, **options)

    def test_absent_table_is_never_created_and_recommends_nothing(self):
        self.table.unlink()
        record = self.recommend("small-change", catalog("gpt-6-luna", "claude-opus-5-5"))
        self.assertEqual((record["status"], record["selected"], record["recommended"]), ("no-affinity-table", None, None))
        self.assertEqual((record["table"]["source"], record["table"]["sha256"]), ("absent", None))
        self.assertFalse(self.table.exists())
        # A Human-specified model is still kept without a table.
        kept = self.recommend("small-change", catalog("gpt-6-luna"), specified="gpt-6-sol")
        self.assertEqual((kept["status"], kept["selected"]), ("human-specified", "gpt-6-sol"))

    def test_the_humans_table_and_its_edits_are_used(self):
        record = self.recommend("small-change", catalog("gpt-6.1-sol", "gpt-6-luna", "claude-opus-5-5"))
        self.assertEqual((record["status"], record["selected"]), ("applied", "gpt-6-luna"))
        self.assertEqual(record["table"]["source"], "file")
        self.assertEqual(record["suggestedProfile"], "workLight")
        table = json.loads(self.table.read_text(encoding="utf-8"))
        table["taskTypes"]["small-change"]["preferences"] = [{"model": "claude-opus-*", "reason": "my choice"}]
        self.table.write_text(json.dumps(table), encoding="utf-8")
        edited = self.recommend("small-change", catalog("gpt-6-luna", "claude-opus-5-5"))
        self.assertEqual((edited["selected"], edited["recommended"]["basis"]), ("claude-opus-5-5", "human"))

    def test_only_detected_models_are_chosen_regardless_of_catalog_age(self):
        record = self.recommend("design-diagnosis", catalog("gpt-6-astra"))
        # claude-opus-* is preferred first but was not detected, so it is never chosen.
        self.assertEqual(record["selected"], "gpt-6-astra")
        self.assertIn("claude-opus-*", record["undetectedPreferences"])
        old = self.recommend("design-diagnosis", catalog("gpt-6-astra", fetched="2020-01-01T00:00:00Z"))
        self.assertEqual((old["status"], old["selected"], old["excluded"]), ("applied", "gpt-6-astra", []))
        missing = self.recommend("design-diagnosis", None)
        self.assertEqual((missing["status"], missing["selected"], missing["catalog"]), ("recheck-required", None, None))

    def test_human_specified_model_is_kept_even_when_undetected(self):
        record = self.recommend("research", catalog("claude-opus-5-5"), specified="gpt-6-sol")
        self.assertEqual((record["status"], record["selected"]), ("human-specified", "gpt-6-sol"))
        self.assertEqual(record["recommended"]["model"], "claude-opus-5-5")
        self.assertTrue(any("not in the detected catalog" in warning for warning in record["warnings"]))

    def test_existing_session_provider_and_unapplied_lists(self):
        record = self.recommend("documentation", catalog("claude-opus-5-5", "gpt-6.1-sol"), session_provider="codex")
        self.assertEqual(record["selected"], "gpt-6.1-sol")
        self.assertEqual(record["excluded"][0]["excludedBecause"], "existing-session-provider-codex")
        listed = self.recommend("documentation", catalog("claude-opus-5-5"), apply=False)
        self.assertEqual((listed["status"], listed["selected"]), ("recommended", None))

    def test_invalid_table_is_reported_without_choosing(self):
        self.table.write_text('{"schemaVersion": 1, "taskTypes": {"misc": {}}}', encoding="utf-8")
        record = self.recommend("research", catalog("claude-opus-5-5"))
        self.assertEqual((record["status"], record["selected"]), ("recheck-required", None))
        self.assertIn("unknown task type", record["warnings"][0])

    def test_allocation_accepts_task_type_and_runtime_record_only(self):
        from test_task_binding import TaskBindingTests
        allocation = {**TaskBindingTests.allocation(), "taskType": "research"}
        task = {"id": "task-one", "allocation": allocation}
        validate([task])
        allocation["modelRecommendation"] = self.recommend("research", catalog("claude-opus-5-5"))
        validate([task])
        for broken in ({**allocation, "taskType": "misc"},
                       {key: value for key, value in allocation.items() if key != "taskType"},
                       {**allocation, "modelRecommendation": {**allocation["modelRecommendation"], "selected": "other"}}):
            with self.assertRaises(ContractError):
                validate([{"id": "task-one", "allocation": broken}])


if __name__ == "__main__":
    unittest.main()
