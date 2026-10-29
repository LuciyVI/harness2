"""Only a successful final-tool result and completed turn authorize a handoff."""

import json
import tempfile
import unittest
from pathlib import Path

from harness2.substrate.dsh_runtime import final_handoff


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "report.md").write_text("Complete fixture report.\n")
        self.stream = self.root / "session.jsonl"

    def write(self, *, value=None, is_error=False, completed=True, session="s"):
        value = value or {
            "version": 1,
            "summary": "Done; verification recorded.",
            "report_path": "report.md",
        }
        events = [
            {"type": "turn/start", "data": {"turn": 1}},
            {"type": "tool/call", "data": {"name": "harness2_finish", "callId": "finish"}},
            {
                "type": "tool/result",
                "data": {
                    "message": {
                        "toolCallId": "finish",
                        "isError": is_error,
                        "content": [{"type": "text", "text": json.dumps(value)}],
                    }
                },
            },
            {
                "type": "turn/end",
                "data": {"reason": {"kind": "completed" if completed else "max-tokens"}},
            },
        ]
        self.stream.write_text(
            "\n".join(json.dumps({"sessionId": session, **event}) for event in events)
        )

    def test_complete_tool_handoff_preserves_the_report(self):
        self.write()
        result = final_handoff(self.stream, self.workspace, "s", 100)
        self.assertEqual(result["report_path"], "report.md")
        self.assertEqual((self.workspace / "report.md").read_text(), "Complete fixture report.\n")

    def test_failed_or_unfinished_tools_and_foreign_sessions_are_not_handoffs(self):
        for options in ({"is_error": True}, {"completed": False}, {"session": "other"}):
            with self.subTest(options=options):
                self.write(**options)
                with self.assertRaises(RuntimeError):
                    final_handoff(self.stream, self.workspace, "s", 100)

    def test_invalid_summary_and_report_references_are_rejected(self):
        (self.root / "outside.md").write_text("Outside")
        (self.workspace / "linked.md").symlink_to(self.root / "outside.md")
        values = [
            {"version": True, "summary": "Done", "report_path": "report.md"},
            {"version": 1, "summary": "x" * 101, "report_path": "report.md"},
            {"version": 1, "summary": "", "report_path": "report.md"},
            {"version": 1, "summary": "Done", "report_path": "../outside.md"},
            {"version": 1, "summary": "Done", "report_path": "linked.md"},
            {"version": 1, "summary": "Done", "report_path": "missing.md"},
            {"version": 1, "summary": "Done", "report_path": "."},
        ]
        for value in values:
            with self.subTest(value=value):
                self.write(value=value)
                with self.assertRaises(RuntimeError):
                    final_handoff(self.stream, self.workspace, "s", 100)

    def test_a_later_turn_cannot_reuse_an_old_handoff(self):
        self.write()
        with self.stream.open("a") as stream:
            stream.write(
                "\n" + json.dumps({"sessionId": "s", "type": "turn/start", "data": {"turn": 2}})
            )
            stream.write(
                "\n"
                + json.dumps(
                    {
                        "sessionId": "s",
                        "type": "turn/end",
                        "data": {"reason": {"kind": "completed"}},
                    }
                )
            )
        with self.assertRaises(RuntimeError):
            final_handoff(self.stream, self.workspace, "s", 100)
