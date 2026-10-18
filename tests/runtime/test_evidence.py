"""evidence.py: baseline-compared checks, shared environment holds and capture preconditions (Human request 2026-10-18)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from storage import paths as runtime_paths

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evidence.py"


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.code = self.base / "project"
        self.code.mkdir()
        self.home = self.base / "home"
        self.storage = Path(runtime_paths.resolve(self.code, create=True, home=self.home)["runtimeRoot"])

    def run_script(self, *args, check=False, env=None):
        command = [sys.executable, str(SCRIPT), *args[:1], "--project-root", str(self.code), "--runtime-home", str(self.home), *args[1:]]
        # Failure logs go to the system temp directory; keep them inside this test's directory.
        finished = subprocess.run(command, capture_output=True, text=True, timeout=120, env={**os.environ, "TMPDIR": str(self.base), **(env or {})})
        if check:
            self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        return finished

    def checks(self, *checks, save=False):
        finished = self.run_script("checks", "--set", "unit", *[value for check in checks for value in ("--check", check)],
                                   *(["--save-baseline"] if save else []))
        return finished.returncode, json.loads(finished.stdout)

    def test_only_failures_absent_from_the_baseline_belong_to_the_change(self):
        code, first = self.checks("stable=true", "broken=echo broken >&2; exit 3", save=True)
        self.assertEqual(code, 1, "without a baseline every failure counts")
        self.assertEqual([item["name"] for item in first["newFailures"]], ["broken"])
        self.assertEqual(first["newFailures"][0]["tail"], ["broken"])
        self.assertTrue((self.storage / "check-baseline-unit.json").is_file(), "the baseline lives in runtime project storage")
        code, second = self.checks("stable=true", "broken=exit 3")
        self.assertEqual(code, 0, "a failure the baseline already had is not the change's")
        self.assertEqual((second["newFailures"], second["existingFailures"]), ([], ["broken"]))
        self.assertIsNone(second["logs"], "only failures that need a look keep their logs")
        code, third = self.checks("stable=exit 1", "broken=true", "added=exit 2")
        self.assertEqual(code, 1)
        self.assertEqual(sorted(item["name"] for item in third["newFailures"]), ["added", "stable"])
        self.assertEqual(third["nowPassing"], ["broken"])
        self.assertFalse(any(path.is_relative_to(self.code) for path in [Path(third["logs"])]), "logs stay outside the project")

    def test_invalid_checks_are_refused(self):
        for args in (("--check", "no-command"), ("--check", "a=true", "--check", "a=true"), ("--check", "../x=true")):
            finished = self.run_script("checks", *args)
            self.assertEqual(finished.returncode, 1)
            self.assertEqual(json.loads(finished.stdout)["kind"], "error")

    def test_a_hold_makes_other_sessions_wait_and_runs_nested_commands_inside_it(self):
        marker = self.base / "order.txt"
        first = subprocess.Popen([sys.executable, str(SCRIPT), "hold", "--project-root", str(self.code), "--runtime-home", str(self.home),
                                  "--name", "sandbox", "--purpose", "first", "--", "sh", "-c", f"echo first-start >> {marker}; sleep 2; echo first-end >> {marker}"])
        self.addCleanup(first.wait)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        nested = f"{sys.executable} {SCRIPT} hold --project-root {self.code} --runtime-home {self.home} --name sandbox --purpose nested -- sh -c 'echo nested >> {marker}'"
        second = self.run_script("hold", "--name", "sandbox", "--purpose", "second", "--", "sh", "-c", f"echo second-start >> {marker}; {nested}")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("waiting for sandbox: held by first", second.stderr)
        self.assertEqual(marker.read_text().split(), ["first-start", "first-end", "second-start", "nested"])
        self.assertFalse((self.storage / "hold-sandbox.json").exists(), "the holder record ends with the hold")

    def test_a_held_environment_fails_after_the_wait_with_its_holder(self):
        first = subprocess.Popen([sys.executable, str(SCRIPT), "hold", "--project-root", str(self.code), "--runtime-home", str(self.home),
                                  "--name", "db", "--purpose", "migration test", "--", "sleep", "3"])
        self.addCleanup(first.wait)
        for _ in range(100):
            if (self.storage / "hold-db.json").exists():
                break
            time.sleep(0.05)
        finished = self.run_script("hold", "--name", "db", "--purpose", "other", "--wait-sec", "0", "--", "true")
        self.assertEqual(finished.returncode, 1)
        self.assertRegex(json.loads(finished.stdout)["error"]["message"], "db stayed held by migration test")

    def test_capture_names_missing_targets_outputs_and_playwright(self):
        finished = self.run_script("capture", "--target", "missing.html", "--out", str(self.base / "a.png"))
        self.assertEqual(json.loads(finished.stdout)["error"]["code"], "capture_target_missing")
        (self.code / "page.html").write_text("<h1>x</h1>")
        finished = self.run_script("capture", "--target", "page.html", "--out", str(self.base / "missing-dir" / "a.png"))
        self.assertEqual(json.loads(finished.stdout)["error"]["code"], "capture_output_invalid")
        # Discovery stops at the user's home; an isolated HOME and an empty npm root find nothing.
        empty = {"HOME": str(self.base), "PATH": "/nonexistent", "PLAYWRIGHT_MODULE": ""}
        finished = self.run_script("capture", "--target", "page.html", "--out", str(self.base / "a.png"), env=empty)
        self.assertEqual(json.loads(finished.stdout)["error"]["code"], "playwright_unavailable")

    def test_capture_rejects_existing_nested_artifact_destination(self):
        (self.code / "page.html").write_text("<h1>x</h1>")
        nested = self.code / "docs/artifact/evidence/topic"
        nested.mkdir(parents=True)
        finished = self.run_script("capture", "--target", "page.html", "--out", str(nested / "capture.png"))
        self.assertEqual(json.loads(finished.stdout)["error"]["code"], "capture_output_invalid")
        self.assertFalse((nested / "capture.png").exists())


if __name__ == "__main__":
    unittest.main()
