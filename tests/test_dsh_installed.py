"""Optional integration test using an installed dsh and a local fake inference server.

HARNESS2_TEST_DSH_COMMAND='node /path/to/dsh/lib/bin.js' python -m unittest discover -s tests -p test_dsh_installed.py -v
No real model or external API is used.
"""

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from harness2 import config
from harness2.substrate.dsh import DshSubstrate, write_base


@unittest.skipUnless(
    os.environ.get("HARNESS2_TEST_DSH_COMMAND"),
    "set HARNESS2_TEST_DSH_COMMAND to test the installed runtime",
)
class InstalledDshTests(unittest.TestCase):
    def test_real_cli_loads_instructions_skills_tools_and_durable_recorder(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                if any(message.get("role") == "tool" for message in request["messages"]):
                    delta = {"content": "DONE"}
                    finish = "stop"
                else:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-write",
                                "type": "function",
                                "function": {
                                    "name": "write",
                                    "arguments": json.dumps(
                                        {"file_path": "answer.txt", "content": "42\n"}
                                    ),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", **delta},
                                "finish_reason": None,
                            }
                        ]
                    },
                    {
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
                    },
                ]
                for chunk in chunks:
                    chunk.update(
                        {
                            "id": "mock",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "test-model",
                        }
                    )
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="harness2-dsh-installed-") as temp:
            root = Path(temp)
            home = root / "source"
            profile = home / "profiles/headless"
            profile.mkdir(parents=True)
            (profile / "cordis.patch.yml").write_text(
                json.dumps(
                    [
                        {"id": "session-title-llm", "disabled": True},
                        {"id": "tools", "config": {"mode": "native"}},
                        {
                            "id": "llm-pi-ai",
                            "disabled": False,
                            "config": {
                                "providers": {
                                    "test": {
                                        "api": "openai-completions",
                                        "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                                        "apiKeyEnv": "HARNESS2_TEST_KEY",
                                        "reasoning": "off",
                                        "defaultContextWindow": 32768,
                                        "defaultMaxTokens": 1024,
                                        "models": [
                                            {
                                                "id": "test-model",
                                                "name": "test-model",
                                                "contextWindow": 32768,
                                                "maxTokens": 1024,
                                                "input": ["text"],
                                            }
                                        ],
                                    }
                                }
                            },
                        },
                    ]
                )
            )
            harness = write_base(
                root / "harness", "HARNESS2_LITERAL {{literal}}. Use sample skill when appropriate."
            )
            skill = harness / "skills/sample/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text(
                "---\nname: sample\ndescription: Sample workflow for this task\n---\nSample body\n"
            )
            with (
                patch.multiple(
                    config,
                    DSH_COMMAND=os.environ["HARNESS2_TEST_DSH_COMMAND"],
                    DSH_HOME=home,
                    DSH_PROFILE="headless",
                    DSH_MODEL="test/test-model",
                    DSH_PATCH="",
                ),
                patch.dict(
                    os.environ, {"HARNESS2_TEST_KEY": "test-only", "DSH_TOOLS_MODE": "native"}
                ),
            ):
                try:
                    result = DshSubstrate(tool_allowlist=("write", "skill")).run_session(
                        "Write 42 to answer.txt",
                        workspace=root / "workspace",
                        stream_path=root / "events.jsonl",
                        harness_dir=harness,
                        timeout=30,
                    )
                except RuntimeError:
                    self.fail((root / "events.stderr.log").read_text()[-5000:])
            self.assertEqual(result["text"], "DONE")
            self.assertEqual((root / "workspace/answer.txt").read_text(), "42\n")
            self.assertTrue(requests)
            visible = json.dumps(requests[0])
            self.assertIn("HARNESS2_LITERAL {{literal}}", visible)
            self.assertIn("Sample workflow for this task", visible)
            self.assertIn('"name": "write"', visible)
            self.assertEqual(
                {tool["function"]["name"] for tool in requests[0]["tools"]}, {"write", "skill"}
            )
            trajectory = DshSubstrate().read_trajectory(
                root / "events.jsonl", obs_trunc=50000, max_chars=100000
            )
            self.assertIn("[ACTION] write", trajectory)
            self.assertIn("[OBSERVATION]", trajectory)


if __name__ == "__main__":
    unittest.main()
