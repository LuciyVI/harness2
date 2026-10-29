"""A status-only observer must include legacy audits and omit model payloads."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from harness2.cli.dsh_status import latest_cycle, session_status


class DshStatusTests(unittest.TestCase):
    def test_legacy_session_timeout_is_terminal_even_without_a_native_turn_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            cycle = Path(temporary)
            run = cycle / "executions/namespace/task/base1"
            run.mkdir(parents=True)
            (run / "dsh_session.headless.jsonl").write_text(
                json.dumps({"type": "status", "phase": "step_start", "step": 20}) + "\n"
            )
            (run / "dsh_session.result.json").write_text(
                json.dumps({"ok": False, "status": "timeout", "timeout_s": 3600})
            )
            row = session_status(cycle)[0]
            self.assertEqual(row["reason"], "wall-timeout")
            self.assertEqual(row["result"], "timeout")

    def test_legacy_token_limit_and_current_probe_are_reported_without_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cycle = root / "cycle"
            cycle.mkdir()
            legacy = root / "temp/dsh-dom1.a1-a2-example"
            legacy.mkdir(parents=True)
            events = [
                {"type": "status", "phase": "step_end", "step": 1},
                {"type": "text", "text": "PRIVATE_MODEL_PAYLOAD"},
                {"type": "status", "phase": "turn_end", "reason": {"kind": "max-tokens"}},
            ]
            (legacy / "session.headless.jsonl").write_text(
                "\n".join(map(json.dumps, events)) + "\n"
            )
            (legacy / "session.result.json").write_text(
                json.dumps({"ok": False, "status": "failed", "text": "PRIVATE_MODEL_PAYLOAD"})
            )
            Path(str(cycle) + ".log").write_text("[dsh dsh-dom1.a1-a2-example] 300s\n")
            probe = cycle / "executions/namespace/task/inv1"
            probe.mkdir(parents=True)
            (probe / "dsh_session.headless.jsonl").write_text(
                json.dumps({"type": "status", "phase": "step_start", "step": 2}) + "\n"
            )
            rows = session_status(cycle, temp_root=root / "temp")
            by_stage = {row["stage"]: row for row in rows}
            self.assertEqual(by_stage[legacy.name]["reason"], "max-tokens")
            self.assertEqual(by_stage[legacy.name]["result"], "failed")
            self.assertEqual(by_stage["inv1"]["phase"], "step_start")
            self.assertNotIn("PRIVATE_MODEL_PAYLOAD", json.dumps(rows))

    def test_new_audits_are_found_and_completed_results_are_distinct_from_open_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            cycle = Path(temporary) / "cycle"
            audit = cycle / "audit/dsh-inv1.a1-a1-example"
            audit.mkdir(parents=True)
            (audit / "session.headless.jsonl").write_text(
                json.dumps({"type": "status", "phase": "turn_end", "reason": {"kind": "completed"}})
                + "\n"
            )
            (audit / "session.result.json").write_text(json.dumps({"ok": True}))
            rows = session_status(cycle)
            self.assertEqual(rows[0]["result"], "ok")
            self.assertEqual(rows[0]["reason"], "completed")

    def test_backend_error_message_is_visible_without_model_or_tool_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            cycle = Path(temporary)
            run = cycle / "executions/namespace/task/agg1"
            run.mkdir(parents=True)
            events = [
                {"type": "text", "text": "PRIVATE_MODEL_PAYLOAD"},
                {
                    "type": "status",
                    "phase": "turn_end",
                    "reason": {
                        "kind": "error",
                        "error": {
                            "message": "cuda prefill state reset failed",
                            "code": "INVALID_REQUEST",
                        },
                    },
                },
            ]
            (run / "dsh_session.headless.jsonl").write_text("\n".join(map(json.dumps, events)))
            row = session_status(cycle)[0]
            self.assertEqual(row["reason"], "error")
            self.assertEqual(row["error"], "cuda prefill state reset failed")
            self.assertNotIn("PRIVATE_MODEL_PAYLOAD", json.dumps(row))

    def test_latest_cycle_uses_request_creation_not_the_heartbeat_of_an_older_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.request.json").write_text("{}")
            (root / "two.request.json").write_text("{}")
            os.utime(root / "one.request.json", (1, 1))
            os.utime(root / "two.request.json", (2, 2))
            (root / "one.log").write_text("new heartbeat of an older cycle")
            self.assertEqual(latest_cycle(root), root / "two")
            self.assertIsNone(latest_cycle(root / "missing"))


if __name__ == "__main__":
    unittest.main()
