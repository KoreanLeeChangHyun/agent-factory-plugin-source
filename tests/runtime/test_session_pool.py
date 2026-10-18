import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from conductor import domains, session_pool
from storage import paths
from storage.errors import ContractError
from storage.files import atomic_write_json, safe_read_json


class SessionPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name) / "home"
        project = Path(self.temp.name) / "project"
        project.mkdir()
        self.root = Path(paths.resolve(project, create=True, home=home)["runtimeRoot"])
        self.domain = domains.create(self.root, "coordination", "human", "test")["domainId"]

    def run_record(self, agent, number, status="queued"):
        domains.assign(self.root, agent, self.domain, "human", "test")
        folder = self.root / "agents" / agent
        folder.mkdir(exist_ok=True, parents=True)
        atomic_write_json(folder / "session.json", {"agentId": agent, "role": "work", "sessionId": "native-" + agent})
        record = folder / "runs" / f"run-{number}" / "state.json"
        record.parent.mkdir(parents=True)
        atomic_write_json(record, {"agentId": agent, "role": "work", "runId": f"run-{number}", "status": status,
                                   "acceptedAt": f"2026-10-16T00:00:{number:02d}Z", "sessionId": "native-" + agent})
        (record.parent / "request.md").write_text("original request")
        (record.parent / "result.md").write_text("original report")
        return record

    def status(self, record, status):
        atomic_write_json(record, {**safe_read_json(record), "status": status})

    def capacity(self, maximum):
        return domains.set_capacity(self.root, self.domain, maximum, "human", "test")

    def view(self):
        return session_pool.public_document(self.root, domains.read(self.root))["domains"][0]

    def test_default_pool_archives_idle_identities_without_changing_any_history(self):
        records = [self.run_record(f"work-{i}", i, "completed") for i in range(18)]
        before = {p: p.read_bytes() for p in self.root.glob("agents/*/runs/*/*")}
        view = self.view()
        self.assertEqual(view["maxSessions"], 5)
        self.assertEqual(len(view["operatingAgentIds"]), 5)
        self.assertEqual(view["operatingAgentIds"][0], "work-17")
        self.assertEqual(len(domains.read(self.root)["assignments"]), 18)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(len(records), 18)

    def test_simultaneous_admission_is_bounded_fifo_and_waiting_resumes(self):
        self.capacity(2)
        records = [self.run_record(f"work-{i}", i) for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            admitted = list(executor.map(lambda p: session_pool.admit(self.root, p), reversed(records)))
        self.assertEqual(sum(admitted), 2)
        self.assertTrue(session_pool.admit(self.root, records[0]))
        self.assertTrue(session_pool.admit(self.root, records[1]))
        self.assertFalse(session_pool.admit(self.root, records[3]))
        self.status(records[0], "completed")
        self.assertTrue(session_pool.admit(self.root, records[2]))
        self.assertFalse(session_pool.admit(self.root, records[3]))
        self.status(records[1], "completed")
        self.status(records[3], "cancelled")
        self.assertTrue(session_pool.admit(self.root, records[4]), "cancelled waiting work does not block the queue")
        self.assertEqual(len(self.view()["operatingAgentIds"]), 2)

    def test_decrease_preserves_running_work_and_increase_unblocks_without_eager_sessions(self):
        self.capacity(3)
        records = [self.run_record(f"work-{i}", i) for i in range(4)]
        for record in records[:3]:
            self.assertTrue(session_pool.admit(self.root, record))
            self.status(record, "running")
        before = [p.read_bytes() for p in records[:3]]
        self.capacity(1)
        self.assertEqual(len(self.view()["operatingAgentIds"]), 3)
        self.assertEqual(before, [p.read_bytes() for p in records[:3]])
        self.assertFalse(session_pool.admit(self.root, records[3]))
        for record in records[:3]:
            self.status(record, "completed")
        self.assertEqual(len(self.view()["operatingAgentIds"]), 1)
        self.assertTrue(session_pool.admit(self.root, records[3]))
        self.capacity(10)
        self.assertEqual(len(self.view()["operatingAgentIds"]), 1, "raising capacity creates no sessions")

    def test_pending_decision_keeps_slot_and_reusing_agent_keeps_its_prior_run(self):
        self.capacity(1)
        first = self.run_record("work-a", 1)
        second = self.run_record("work-b", 2)
        self.assertTrue(session_pool.admit(self.root, first))
        self.status(first, "needs-human-decision")
        self.assertFalse(session_pool.admit(self.root, second))
        self.status(first, "completed")
        historical = first.read_bytes()
        self.assertTrue(session_pool.admit(self.root, second))
        self.status(second, "completed")
        resumed = self.run_record("work-a", 3)
        self.assertTrue(session_pool.admit(self.root, resumed))
        self.assertEqual(first.read_bytes(), historical)

    def test_settings_validate_revision_and_human_ownership(self):
        revision = domains.read(self.root)["revision"]
        self.capacity(4)
        with self.assertRaises(ContractError) as conflict:
            domains.set_capacity(self.root, self.domain, 3, "human", "test", revision)
        self.assertEqual(conflict.exception.code, "domain_conflict")
        for value in (0, -1, True, 1.5, "3"):
            with self.assertRaises(ContractError):
                self.capacity(value)
        with self.assertRaises(ContractError):
            domains.set_capacity(self.root, self.domain, 3, "ai", "test")
        self.assertEqual(json.loads((self.root / "domains.json").read_text())["domains"][0]["maxSessions"], 4)

    def test_full_pool_requires_a_compatible_idle_performer_before_new_identity(self):
        from test_team import TeamAssignmentTests
        fixture = TeamAssignmentTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        started = fixture.start("first")
        fixture.finish(started, "done")
        root = Path(fixture.agent_exec.runtime_paths.resolve(fixture.root)["runtimeRoot"])
        document = domains.read(root)
        domain_id = document["assignments"]["team-worker"]["domainId"]
        domains.set_capacity(root, domain_id, 1, "human", "test")
        fixture.refused("worker_reuse_required", "next", agent="team-other")
        continued = fixture.start("continued", strategy="reuse")
        self.assertEqual(continued["workAgentId"], "team-worker")


if __name__ == "__main__":
    unittest.main()
