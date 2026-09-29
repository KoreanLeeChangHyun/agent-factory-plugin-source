"""Codex sessions stream through app-server whenever the installed protocol can carry instructions."""
import runtime_test_home
import unittest
from unittest import mock
from native_fixtures import runtime

import adapters.codex as codex
import storage.files


def capabilities(instruction_delivery):
    fields = {"model": True, "reasoning": True, "fast": True, "goal": True, "plan": True,
              "instructionDelivery": instruction_delivery}
    return {"submit": dict(fields), "send": dict(fields), "diagnostic": None}


class CodexStreamingBackendTests(unittest.TestCase):
    def prepare(self, session, instruction_delivery):
        persisted = {}
        state = {"statePath": "/runtime/run/state.json", "executionOptions": {"taskMode": "direct"}}
        with mock.patch.object(codex, "inspect_capabilities", return_value=capabilities(instruction_delivery)), \
                mock.patch.object(storage.files, "atomic_write_json"), \
                mock.patch.object(storage.files, "update_json", side_effect=lambda path, lock, change: change(persisted)):
            codex.prepare(session, state, b"request")
        return session, persisted

    def test_legacy_direct_session_moves_to_streaming_app_server(self):
        session, state = self.prepare({"provider": "codex", "codex": "codex", "sessionId": None}, True)
        self.assertEqual(session["backend"], "app-server")
        self.assertTrue(codex.uses_prompt_parts(session))
        self.assertEqual(state["backend"], "app-server")

    def test_legacy_session_keeps_exec_without_instruction_delivery(self):
        session, state = self.prepare({"provider": "codex", "codex": "codex", "sessionId": None}, False)
        self.assertNotIn("backend", session)
        self.assertNotIn("backend", state)


if __name__ == "__main__":
    unittest.main()
