import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from storage import paths as runtime_paths
from storage.errors import ContractError
from tasks import domains

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def runtime_root(home, code):
    """Writes are confined to a pinned managed runtime, so tests bind a real isolated one."""
    code.mkdir(parents=True, exist_ok=True)
    return Path(runtime_paths.resolve(code, create=True, home=home)["runtimeRoot"])


def _concurrent_ai_link(home, code, index):
    domains.link_dispatch(runtime_root(Path(home), Path(code)), f"work-{index}", "shared area", f"run-{index}")


class ProjectDomainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.code = Path(self.temporary.name) / "project-a"
        self.root = runtime_root(self.home, self.code)

    def test_human_create_classify_rename_and_move_persist_with_history(self):
        created = domains.create(self.root, "extension UI", "human", "control-center")
        domain_id = created["domainId"]
        domains.assign(self.root, "work-legacy", domain_id, "human", "control-center", created["revision"])
        renamed = domains.rename(self.root, domain_id, "Extension surfaces", "human", "control-center")
        other = domains.create(self.root, "plugin runtime", "human", "control-center")
        domains.assign(self.root, "work-legacy", other["domainId"], "human", "control-center")
        stored = domains.read(self.root)  # A fresh read stands in for reload/restart.
        self.assertEqual(stored["assignments"]["work-legacy"]["domainId"], other["domainId"])
        first = next(item for item in stored["domains"] if item["id"] == domain_id)
        self.assertEqual((first["name"], first["aliases"]), ("Extension surfaces", ["extension UI"]))
        self.assertEqual(domains.find(stored, "EXTENSION ui")["id"], domain_id, "Former names keep recorded tasks linked")
        self.assertEqual([event["action"] for event in stored["history"]], ["create", "assign", "rename", "create", "assign"])
        self.assertTrue(all(event["actor"] == "human" and event["source"] for event in stored["history"]))
        self.assertGreater(stored["revision"], renamed["revision"])

    def test_names_are_validated_and_never_duplicated_or_merged(self):
        first = domains.create(self.root, "extension UI", "human", "control-center")["domainId"]
        second = domains.create(self.root, "plugin runtime", "human", "control-center")["domainId"]
        for name in ("", " padded", "two\nlines", "x" * 81, None):
            with self.subTest(name=name), self.assertRaises(ContractError):
                domains.create(self.root, name, "human", "control-center")
        with self.assertRaises(ContractError) as taken:
            domains.create(self.root, "Extension  UI", "human", "control-center")
        self.assertEqual(taken.exception.code, "domain_name_taken")
        with self.assertRaises(ContractError) as merge:
            domains.rename(self.root, second, "extension ui", "human", "control-center")
        self.assertEqual(merge.exception.code, "domain_name_taken")
        reused = domains.create(self.root, "EXTENSION UI", "ai", "run-1")
        self.assertEqual((reused["domainId"], reused["reused"]), (first, True))
        self.assertEqual(len(domains.read(self.root)["domains"]), 2)
        for actor, source in (("robot", "x"), ("human", " ")):
            with self.subTest(actor=actor), self.assertRaises(ContractError):
                domains.create(self.root, "new", actor, source)
        with self.assertRaises(ContractError):
            domains.assign(self.root, "../escape", first, "human", "control-center")
        with self.assertRaises(ContractError):
            domains.assign(self.root, "work-a", "domain-000000000000", "human", "control-center")

    def test_ai_reuses_or_creates_but_never_overrides_human_edits(self):
        link = domains.link_dispatch(self.root, "work-new", "plugin runtime", "loop one")
        self.assertEqual((link["reused"], link["assigned"]), (False, True))
        again = domains.link_dispatch(self.root, "work-other", "Plugin Runtime", "loop two")
        self.assertEqual((again["domainId"], again["reused"]), (link["domainId"], True))
        ai_named = link["domainId"]
        domains.rename(self.root, ai_named, "runtime core", "ai", "run-3")  # Main may rename its own domain.
        domains.rename(self.root, ai_named, "Plugin runtime", "human", "control-center")
        with self.assertRaises(ContractError) as renamed:
            domains.rename(self.root, ai_named, "runtime", "ai", "run-4")
        self.assertEqual(renamed.exception.code, "domain_protected")
        human = domains.create(self.root, "extension UI", "human", "control-center")["domainId"]
        domains.assign(self.root, "work-new", human, "human", "control-center")
        protected = domains.link_dispatch(self.root, "work-new", "plugin runtime", "loop three")
        self.assertEqual((protected["assigned"], protected["protectedDomainId"]), (False, human))
        domains.assign(self.root, "work-other", None, "human", "control-center")
        kept = domains.link_dispatch(self.root, "work-other", "plugin runtime", "loop four")
        domains.assign(self.root, "work-never-placed", None, "human", "control-center")
        self.assertFalse(domains.link_dispatch(self.root, "work-never-placed", "plugin runtime", "loop five")["assigned"],
                         "Unclassifying a worker that had no placement is protected as well")
        self.assertFalse(kept["assigned"], "A Human 'unclassified' choice is protected too")
        stored = domains.read(self.root)
        self.assertEqual(stored["assignments"]["work-new"]["domainId"], human)
        self.assertEqual(stored["assignments"]["work-other"], {"domainId": None, "setBy": stored["assignments"]["work-other"]["setBy"]})
        self.assertEqual(next(item for item in stored["domains"] if item["id"] == ai_named)["name"], "Plugin runtime")

    def test_stale_revision_conflicts_and_concurrent_writers_lose_nothing(self):
        first = domains.create(self.root, "extension UI", "human", "control-center")
        domains.create(self.root, "plugin runtime", "ai", "run-1")
        with self.assertRaises(ContractError) as stale:
            domains.rename(self.root, first["domainId"], "UI", "human", "control-center", first["revision"])
        self.assertEqual(stale.exception.code, "domain_conflict")
        self.assertEqual(domains.find(domains.read(self.root), "extension UI")["id"], first["domainId"])
        context = multiprocessing.get_context("spawn")
        with context.Pool(4) as pool:
            pool.starmap(_concurrent_ai_link, [(str(self.home), str(self.code), index) for index in range(8)])
        stored = domains.read(self.root)
        self.assertEqual([item["name"] for item in stored["domains"]].count("shared area"), 1)
        self.assertEqual(len([agent for agent in stored["assignments"] if agent.startswith("work-")]), 8)
        self.assertEqual(stored["revision"], 2 + 1 + 8)

    def test_placeholder_creations_are_independent_unmatched_and_renamed_later(self):
        real = domains.create(self.root, "extension UI", "human", "control-center")["domainId"]
        domains.assign(self.root, "work-real", real, "human", "control-center")
        first = domains.create(self.root, None, "human", "control-center", placeholder=True)
        second = domains.create(self.root, None, "human", "control-center", first["revision"], placeholder=True)
        self.assertNotEqual(first["domainId"], second["domainId"], "Repeated creation makes independent domains")
        stored = domains.read(self.root)
        placeholders = [item for item in stored["domains"] if item.get("provisional")]
        self.assertEqual([item["name"] for item in placeholders], [domains.PLACEHOLDER_NAME] * 2)
        self.assertEqual(stored["assignments"], {"work-real": stored["assignments"]["work-real"]}, "No worker is absorbed")
        self.assertIsNone(domains.find(stored, "미분류"))
        # Main naming a work area 미분류 gets its own real domain; it never reuses a provisional one.
        ai = domains.link_dispatch(self.root, "work-ai", "미분류", "loop one")
        self.assertNotIn(ai["domainId"], {first["domainId"], second["domainId"]})
        self.assertFalse(ai["reused"])
        with self.assertRaises(ContractError) as protected:
            domains.rename(self.root, first["domainId"], "AI name", "ai", "run-x")
        self.assertEqual(protected.exception.code, "domain_protected")
        with self.assertRaises(ContractError) as taken:
            domains.rename(self.root, first["domainId"], "Extension ui", "human", "control-center")
        self.assertEqual(taken.exception.code, "domain_name_taken", "Confirmed-name rules still apply")
        domains.rename(self.root, first["domainId"], "Docs site", "human", "control-center")
        stored = domains.read(self.root)
        renamed = next(item for item in stored["domains"] if item["id"] == first["domainId"])
        self.assertEqual((renamed["name"], renamed["aliases"], renamed.get("provisional")), ("Docs site", [], None))
        self.assertTrue(next(item for item in stored["domains"] if item["id"] == second["domainId"])["provisional"], "Others stay provisional")
        self.assertEqual(domains.find(stored, "docs SITE")["id"], first["domainId"])
        for actor, name in (("ai", None), ("human", "named")):
            with self.subTest(actor=actor), self.assertRaises(ContractError):
                domains.create(self.root, name, actor, "x", placeholder=True)
        with self.assertRaises(ContractError) as stale:
            domains.create(self.root, None, "human", "control-center", first["revision"], placeholder=True)
        self.assertEqual(stale.exception.code, "domain_conflict", "A duplicate click with the old revision does not create twice")

    def test_remove_worker_hides_only_stopped_workers_and_keeps_their_records(self):
        agent = self.root / "agents" / "work-done"
        (agent / "runs" / "run-1").mkdir(parents=True)
        (agent / "runs" / "run-1" / "state.json").write_text(json.dumps({"status": "completed"}))
        (agent / "runs" / "run-1" / "result.md").write_text("result kept")
        busy = self.root / "agents" / "work-busy"
        (busy / "loops" / "loop-1").mkdir(parents=True)
        (busy / "loops" / "loop-1" / "state.json").write_text(json.dumps({"status": "active"}))
        running = self.root / "agents" / "work-running"
        (running / "runs" / "run-2").mkdir(parents=True)
        (running / "runs" / "run-2" / "state.json").write_text(json.dumps({"status": "cancelling"}))
        removed = domains.remove_worker(self.root, "work-done", "human", "control-center")
        self.assertTrue(removed["removed"])
        again = domains.remove_worker(self.root, "work-done", "human", "control-center")
        self.assertEqual(again["revision"], removed["revision"], "A repeated removal changes nothing")
        for agent_id in ("work-busy", "work-running"):
            with self.subTest(agent=agent_id), self.assertRaises(ContractError) as error:
                domains.remove_worker(self.root, agent_id, "human", "control-center")
            self.assertEqual(error.exception.code, "worker_running")
        with self.assertRaises(ContractError):
            domains.remove_worker(self.root, "work-done-2", "ai", "run-1")
        with self.assertRaises(ContractError) as stale:
            domains.remove_worker(self.root, "work-other", "human", "control-center", removed["revision"] - 1)
        self.assertEqual(stale.exception.code, "domain_conflict")
        stored = domains.read(self.root)
        self.assertEqual(list(stored["removedWorkers"]), ["work-done"])
        self.assertEqual(stored["removedWorkers"]["work-done"]["removedBy"]["actor"], "human")
        self.assertEqual((agent / "runs" / "run-1" / "result.md").read_text(), "result kept", "Records stay")
        legacy = runtime_root(self.home, Path(self.temporary.name) / "legacy")
        document = domains.empty(); document.pop("removedWorkers")
        (legacy / domains.FILE).write_text(json.dumps(document))
        self.assertEqual(domains.read(legacy)["removedWorkers"], {}, "Stores without removals still read")

    def test_projects_are_separate_and_invalid_stores_are_rejected(self):
        domains.create(self.root, "extension UI", "human", "control-center")
        other = runtime_root(self.home, Path(self.temporary.name) / "project-b")
        self.assertNotEqual(other, self.root)
        self.assertEqual(domains.read(other)["domains"], [])
        (other / domains.FILE).write_text(json.dumps({"schemaVersion": 9}))
        with self.assertRaises(ContractError):
            domains.create(other, "x", "human", "control-center")
        self.assertEqual(json.loads((other / domains.FILE).read_text()), {"schemaVersion": 9}, "Failed writes leave the store unchanged")

    def test_cli_round_trip_in_an_isolated_runtime(self):
        home = Path(self.temporary.name) / "cli-home"
        project = Path(self.temporary.name) / "code"
        project.mkdir()
        environment = {**os.environ, "AGENT_FACTORY_HOME": str(home)}

        def run(*arguments):
            output = subprocess.run([sys.executable, str(SCRIPTS / arguments[0]), *arguments[1:], "--project-root", str(project)],
                                    capture_output=True, text=True, env=environment, check=False)
            return output.returncode, json.loads(output.stdout)
        self.assertEqual(run("exec.py", "init")[0], 0)
        code, created = run("domains.py", "create", "--actor", "human", "--source", "control-center", "--name", "extension UI")
        self.assertEqual(code, 0)
        code, linked = run("domains.py", "link", "--actor", "ai", "--source", "run-1", "--agent", "work-a", "--name", "Extension UI")
        self.assertEqual((code, linked["domainId"], linked["reused"]), (0, created["domainId"], True))
        code, error = run("domains.py", "rename", "--actor", "ai", "--source", "run-2", "--domain-id", created["domainId"], "--name", "UI")
        self.assertEqual((code, error["error"]["code"] if isinstance(error.get("error"), dict) else error.get("code")), (1, "domain_protected"))
        code, listed = run("domains.py", "list")
        self.assertEqual(listed["domains"]["assignments"]["work-a"]["domainId"], created["domainId"])


if __name__ == "__main__":
    unittest.main()
