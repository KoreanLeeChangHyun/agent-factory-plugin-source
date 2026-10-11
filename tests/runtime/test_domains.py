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
        self.assertEqual((link["reused"], link["assigned"], link["member"]), (False, True, True))
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
        self.assertEqual((protected["assigned"], protected["protectedDomainId"], protected["member"]), (False, human, True))
        with self.assertRaises(ContractError) as moved:
            domains.assign(self.root, "work-new", ai_named, "ai", "run-5")
        self.assertEqual(moved.exception.code, "domain_protected")
        stored = domains.read(self.root)
        self.assertEqual(stored["assignments"]["work-new"]["domainId"], human)
        self.assertEqual(next(item for item in stored["domains"] if item["id"] == ai_named)["name"], "Plugin runtime")

    def test_no_write_leaves_a_worker_without_a_real_domain(self):
        human = domains.create(self.root, "extension UI", "human", "control-center")
        domains.assign(self.root, "work-a", human["domainId"], "human", "control-center")
        before = (self.root / domains.FILE).read_text()
        for actor in ("human", "ai"):
            with self.subTest(actor=actor), self.assertRaises(ContractError) as unplaced:
                domains.assign(self.root, "work-a" if actor == "human" else "work-b", None, actor, "x")
            self.assertEqual(unplaced.exception.code, "domain_membership_required")
        # Names that mean "no domain" are refused for everyone; Main must also avoid catch-all names.
        for name in ("미분류", "Unclassified", "무소속", "UNASSIGNED", "none"):
            for actor in ("human", "ai"):
                with self.subTest(name=name, actor=actor), self.assertRaises(ContractError) as reserved:
                    domains.create(self.root, name, actor, "x")
                self.assertEqual(reserved.exception.code, "domain_name_reserved")
        for name in ("기타", "기본", "Default", "misc", "General"):
            with self.subTest(name=name), self.assertRaises(ContractError) as catch_all:
                domains.link_dispatch(self.root, "work-c", name, "loop one")
            self.assertEqual(catch_all.exception.code, "domain_name_reserved")
        with self.assertRaises(ContractError) as rename:
            domains.rename(self.root, human["domainId"], "미분류", "human", "control-center")
        self.assertEqual(rename.exception.code, "domain_name_reserved")
        with self.assertRaises(ContractError) as missing:
            domains.assign(self.root, "work-b", "domain-000000000000", "ai", "x")
        self.assertEqual(missing.exception.code, "domain_missing")
        self.assertEqual((self.root / domains.FILE).read_text(), before, "Refused writes change nothing")
        self.assertNotIn("work-c", domains.read(self.root)["assignments"])
        # The Human's own name for a work area is theirs; only Main is held to a specific area.
        self.assertFalse(domains.create(self.root, "Other tools", "human", "control-center")["reused"])

        def drop_domain(document):
            document["domains"] = [item for item in document["domains"] if item["id"] != human["domainId"]]
            return {}

        def drop_member(document):
            document["assignments"].pop("work-a")
            return {}
        for change, code in ((drop_domain, "domain_in_use"), (drop_member, "domain_membership_required")):
            with self.subTest(change=change.__name__), self.assertRaises(ContractError) as kept:
                domains._mutate(self.root, None, change)
            self.assertEqual(kept.exception.code, code)
        self.assertEqual(domains.read(self.root)["assignments"]["work-a"]["domainId"], human["domainId"])

    def write_legacy_store(self):
        """A store written before membership was required, plus worker records the runtime already holds."""
        human = {"actor": "human", "at": "2026-10-01T00:00:00Z", "source": "control-center"}
        ai = {"actor": "ai", "at": "2026-10-01T00:00:00Z", "source": "loop old"}
        document = domains.empty()
        document["revision"] = 7
        document["domains"] = [
            {"id": "domain-aaaaaaaaaaaa", "name": "Extension surfaces", "aliases": ["extension UI"], "createdBy": human, "nameSetBy": human},
            {"id": "domain-bbbbbbbbbbbb", "name": "미분류", "aliases": [], "provisional": True, "createdBy": human, "nameSetBy": human}]
        document["assignments"] = {
            "work-placed": {"domainId": "domain-aaaaaaaaaaaa", "setBy": human},
            "work-human-null": {"domainId": None, "setBy": human},
            "work-in-provisional": {"domainId": "domain-bbbbbbbbbbbb", "setBy": ai},
            "work-dangling": {"domainId": "domain-cccccccccccc", "setBy": ai}}
        document["removedWorkers"] = {"work-removed": {"removedBy": human}}
        document["history"] = [{**human, "action": "create", "domainId": "domain-aaaaaaaaaaaa", "name": "extension UI", "revision": 1}]
        (self.root / domains.FILE).write_text(json.dumps(document))

        def worker(agent_id, role="work", runs=(), loops=()):
            directory = self.root / "agents" / agent_id
            directory.mkdir(parents=True)
            (directory / "session.json").write_text(json.dumps({"agentId": agent_id, "role": role}))
            for index, name in enumerate(runs):
                (directory / "runs" / f"run-{index}").mkdir(parents=True)
                allocation = {} if name is None else {"domain": name}
                (directory / "runs" / f"run-{index}" / "state.json").write_text(json.dumps({"taskBinding": {"allocation": allocation}}))
            for index, name in enumerate(loops):
                (directory / "loops" / f"loop-{index}").mkdir(parents=True)
                (directory / "loops" / f"loop-{index}" / "state.json").write_text(json.dumps(
                    {"status": "completed", "workflow": {"tasks": [{"id": "task-1", "allocation": {"domain": name}}]}}))
        worker("work-old-ui", runs=["EXTENSION ui", None], loops=["extension UI"])  # One area, by a former name.
        worker("work-old-docs", runs=["docs site"])  # One area not in the list yet.
        worker("work-old-mixed", runs=["docs site", "plugin runtime"])  # Ambiguous: two areas.
        worker("work-old-none", runs=[None])  # No recorded area at all.
        worker("work-old-reserved", runs=["미분류"])  # A recorded "no domain" name is no evidence.
        worker("verify-old", role="verification", runs=["plugin runtime"])
        worker("work-human-null", runs=["plugin runtime"])
        worker("work-removed", runs=[None])
        worker("main-0000", role="main", runs=["plugin runtime"])  # Main is not a worker.
        return document

    def test_legacy_entries_are_kept_reported_and_recovered_only_from_evidence(self):
        original = self.write_legacy_store()
        reasons = {item["agentId"]: item["reason"] for item in domains.unresolved(self.root)}
        self.assertEqual(reasons, {
            "work-human-null": "legacy-unclassified", "work-in-provisional": "unnamed-domain", "work-dangling": "missing-domain",
            "work-old-ui": "unassigned", "work-old-docs": "unassigned", "work-old-mixed": "unassigned", "work-old-none": "unassigned",
            "work-old-reserved": "unassigned", "verify-old": "unassigned", "work-removed": "unassigned"})
        self.assertTrue(next(item for item in domains.unresolved(self.root) if item["agentId"] == "work-removed")["removed"],
                        "Removed workers stay visible as unresolved, never hidden")
        preview = domains.recover(self.root, "ai", "run-recover", dry_run=True)
        self.assertEqual(json.loads((self.root / domains.FILE).read_text()), original, "A dry run writes nothing")
        self.assertEqual({item["agentId"] for item in preview["recovered"]}, {"work-old-ui", "work-old-docs", "verify-old"})
        with self.assertRaises(ContractError) as stale:
            domains.recover(self.root, "ai", "run-recover", expected_revision=6)
        self.assertEqual(stale.exception.code, "domain_conflict")
        recovered = domains.recover(self.root, "ai", "run-recover", expected_revision=7)
        stored = domains.read(self.root)
        placed = {item["agentId"]: item for item in recovered["recovered"]}
        self.assertEqual(placed["work-old-ui"]["domainId"], "domain-aaaaaaaaaaaa", "A former name reuses the renamed domain")
        self.assertIn("run run-0", placed["work-old-ui"]["evidence"])
        docs = domains.find(stored, "docs site")
        self.assertEqual((stored["assignments"]["work-old-docs"]["domainId"], docs["createdBy"]["source"]), (docs["id"], "run-recover"))
        self.assertEqual(stored["assignments"]["verify-old"]["domainId"], domains.find(stored, "plugin runtime")["id"])
        left = {item["agentId"]: item for item in recovered["unresolved"]}
        self.assertEqual(set(left), {"work-human-null", "work-in-provisional", "work-dangling", "work-old-mixed", "work-old-none",
                                     "work-old-reserved", "work-removed"})
        self.assertEqual(left["work-old-mixed"]["candidates"], ["docs site", "plugin runtime"])
        self.assertEqual(left["work-human-null"]["candidates"], ["plugin runtime"], "Evidence is offered, not applied over the Human")
        # Every original entry, domain and history event is preserved as it was.
        for agent_id, entry in original["assignments"].items():
            self.assertEqual(stored["assignments"][agent_id], entry)
        self.assertEqual(stored["domains"][:2], original["domains"])
        self.assertEqual(stored["history"][0], original["history"][0])
        self.assertEqual(stored["removedWorkers"], original["removedWorkers"])
        self.assertEqual(domains.recover(self.root, "ai", "run-again")["recovered"], [], "Recovery is idempotent")

        # Explicit choices resolve the rest; Main still cannot override the Human's legacy choice.
        with self.assertRaises(ContractError) as protected:
            domains.assign(self.root, "work-human-null", docs["id"], "ai", "run-x")
        self.assertEqual(protected.exception.code, "domain_protected")
        self.assertFalse(domains.link_dispatch(self.root, "work-human-null", "docs site", "loop x")["member"])
        with self.assertRaises(ContractError) as unnamed:
            domains.assign(self.root, "work-old-none", "domain-bbbbbbbbbbbb", "human", "control-center")
        self.assertEqual(unnamed.exception.code, "domain_name_required")
        domains.assign(self.root, "work-human-null", docs["id"], "human", "control-center")
        domains.assign(self.root, "work-dangling", docs["id"], "ai", "run-x")
        domains.link_dispatch(self.root, "work-old-mixed", "plugin runtime", "run-x")
        domains.rename(self.root, "domain-bbbbbbbbbbbb", "Release tooling", "human", "control-center")
        remaining = {item["agentId"] for item in domains.unresolved(self.root)}
        self.assertEqual(remaining, {"work-old-none", "work-old-reserved", "work-removed"})

    def test_cli_refuses_unclassified_options_and_reports_unresolved_workers(self):
        home = Path(self.temporary.name) / "cli-home-2"
        project = Path(self.temporary.name) / "code-2"
        project.mkdir()
        environment = {**os.environ, "AGENT_FACTORY_HOME": str(home)}

        def run(*arguments):
            output = subprocess.run([sys.executable, str(SCRIPTS / arguments[0]), *arguments[1:], "--project-root", str(project)],
                                    capture_output=True, text=True, env=environment, check=False)
            return output.returncode, json.loads(output.stdout)

        def code_of(document):
            return document["error"]["code"] if isinstance(document.get("error"), dict) else document.get("code")
        self.assertEqual(run("exec.py", "init")[0], 0)
        code, created = run("domains.py", "create", "--actor", "human", "--source", "control-center", "--name", "extension UI")
        self.assertEqual(code, 0)
        for arguments in (("create", "--actor", "human", "--source", "control-center", "--placeholder"),
                          ("assign", "--actor", "human", "--source", "control-center", "--agent", "work-a", "--unclassified")):
            with self.subTest(command=arguments[0]):
                code, error = run("domains.py", *arguments)
                self.assertEqual((code, code_of(error)), (1, "domain_membership_required"))
        code, error = run("domains.py", "link", "--actor", "ai", "--source", "run-1", "--agent", "work-a", "--name", "미분류")
        self.assertEqual((code, code_of(error)), (1, "domain_name_reserved"))
        code, error = run("domains.py", "assign", "--actor", "human", "--source", "control-center", "--agent", "work-a")
        self.assertEqual((code, code_of(error)), (1, "invalid_arguments"))
        code, listed = run("domains.py", "list")
        self.assertEqual((code, listed["unresolved"], listed["domains"]["revision"]), (0, [], created["revision"]))
        runtime = Path(runtime_paths.resolve(project, home=home)["runtimeRoot"])
        (runtime / "agents" / "work-old" / "runs" / "run-1").mkdir(parents=True)
        (runtime / "agents" / "work-old" / "session.json").write_text(json.dumps({"agentId": "work-old", "role": "work"}))
        (runtime / "agents" / "work-old" / "runs" / "run-1" / "state.json").write_text(json.dumps({"taskBinding": {"allocation": {"domain": "Extension UI"}}}))
        code, listed = run("domains.py", "list")
        self.assertEqual(listed["unresolved"], [{"agentId": "work-old", "reason": "unassigned"}])
        code, preview = run("domains.py", "recover", "--actor", "ai", "--source", "run-2", "--dry-run")
        self.assertEqual((code, preview["dryRun"], preview["domains"]["revision"]), (0, True, created["revision"]))
        code, recovered = run("domains.py", "recover", "--actor", "ai", "--source", "run-2", "--expected-revision", str(created["revision"]))
        self.assertEqual((code, recovered["recovered"][0]["domainId"], recovered["unresolved"]), (0, created["domainId"], []))
        code, listed = run("domains.py", "list")
        self.assertEqual((listed["unresolved"], listed["domains"]["assignments"]["work-old"]["domainId"]), ([], created["domainId"]))

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
