"""Native slash-command and cycle middleware tests; only a local fake model is used."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness2.cli.dsh_install import install
from harness2.substrate.dsh_runtime import command_argv, outcome, prepare_session

WORKER = """#!PYTHON
import json,os,sys
from pathlib import Path
request=json.loads(Path(sys.argv[sys.argv.index('--request')+1]).read_text())
root=Path(request['output'])
root.mkdir(parents=True)
if os.environ.get('TEST_HARNESS2_WORKER_FAIL') == '1':
 for _ in range(300): print('  [dsh agg1] 30s; completed steps=1; step_start', file=sys.stderr, flush=True)
 (root/'result.json').write_text(json.dumps({'ok':False,'error':'agg1: 500: cuda prefill state reset failed'}))
 sys.exit(1)
harness=root/'selected-harness'
(harness/'tool_descriptions').mkdir(parents=True)
for name in ['systemprompt.md','guardrails.md','LongTermMEMORY.md','tool_descriptions/tool_guidance.md']:
 (harness/name).write_text('NATIVE_MODE_GUIDANCE' if name=='systemprompt.md' else '')
(root/'response.md').write_text('Chosen cycle response')
(root/'changes.diff').write_text('No changes')
(root/'result.json').write_text(json.dumps({'ok':True,'output':str(root),'harness':str(harness),
 'workspace':request['workspace'],'response':str(root/'response.md'),'changes':str(root/'changes.diff')}))
"""

DRIVER = """import { writeFileSync } from 'node:fs';
export const inject = ['commands'];
export function apply(ctx, config) {
 ctx.on('agent/created', async ({agent}) => {
  const results = [];
  for (const command of config.commands) {
   results.push(await ctx.commands.execute(agent, '/harness2 ' + command, [], new AbortController().signal));
  }
  writeFileSync(config.output, JSON.stringify(results));
 });
}
"""


@unittest.skipUnless(os.environ.get("HARNESS2_TEST_DSH_COMMAND"), "installed dsh required")
class InstalledDshModeTests(unittest.TestCase):
    def test_native_on_off_and_invalid_commands_control_full_cycle_middleware(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(request)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta, finish in [
                    ({"role": "assistant", "content": "KEEP_BASE"}, None),
                    ({}, "stop"),
                ]:
                    event = {
                        "id": "test",
                        "object": "chat.completion.chunk",
                        "model": "test-model",
                        "created": 1,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    }
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="dsh-mode-native-") as tmp:
            root = Path(tmp)
            home = root / "source"
            profile = home / "profiles/headless"
            profile.mkdir(parents=True)
            provider = {
                "id": "llm-pi-ai",
                "disabled": False,
                "config": {
                    "providers": {
                        "test": {
                            "api": "openai-completions",
                            "baseURL": f"http://127.0.0.1:{server.server_port}/v1",
                            "apiKeyEnv": "TEST_DSH_MODE_KEY",
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
            }
            (profile / "cordis.patch.yml").write_text(
                json.dumps(
                    [
                        {"id": "session-title-llm", "disabled": True},
                        {"id": "tools", "config": {"mode": "native"}},
                        provider,
                    ]
                )
            )
            worker = root / "worker"
            worker.write_text(WORKER.replace("PYTHON", sys.executable))
            worker.chmod(0o700)
            plugin = root / "plugin"
            command = os.environ["HARNESS2_TEST_DSH_COMMAND"]
            install(
                home=home, profile="headless", state=plugin, command=command, python=str(worker)
            )
            # First test admission with a fixture; then run the actual Python
            # cycle with native solver/improver sessions against the fake model.
            driver = root / "driver.mjs"
            driver.write_text(DRIVER)
            for index, commands in enumerate(
                (
                    ["on", "status"],
                    ["on", "off", "invalid", "status"],
                    ["on", "status"],
                    ["on", "status"],
                )
            ):
                with self.subTest(commands=commands):
                    if index == 2:
                        install(
                            home=home,
                            profile="headless",
                            state=plugin,
                            command=command,
                            python=sys.executable,
                        )
                    elif index == 3:
                        install(
                            home=home,
                            profile="headless",
                            state=plugin,
                            command=command,
                            python=str(worker),
                        )
                    state = root / f"state{index}"
                    overrides = prepare_session(
                        state,
                        source_home=home,
                        source_profile="headless",
                        text="",
                        model="test/test-model",
                    )
                    overlay = state / "driver.json"
                    status = root / f"commands{index}.json"
                    overlay.write_text(
                        json.dumps(
                            [
                                {
                                    "insert": [
                                        {
                                            "id": "mode-test-driver",
                                            "name": str(driver),
                                            "config": {"commands": commands, "output": str(status)},
                                        }
                                    ]
                                }
                            ]
                        )
                    )
                    workspace = root / f"project{index}"
                    workspace.mkdir()
                    env = {
                        **os.environ,
                        **overrides,
                        "HARNESS2_DSH_CHILD": "0",
                        "HARNESS2_DSH_EVENTS": str(root / f"events{index}.jsonl"),
                        "DSH_PERMISSION_MODE": "workspace-write",
                        "DSH_TOOLS_MODE": "native",
                        "TEST_DSH_MODE_KEY": "test-only",
                        "TEST_HARNESS2_WORKER_FAIL": "1" if index == 3 else "0",
                    }
                    requests_before = len(requests)
                    result = subprocess.run(
                        [
                            *command_argv(command),
                            "--profile",
                            "harness2",
                            "--patch",
                            str(state / "overlay.json"),
                            "--patch",
                            str(overlay),
                            "--json",
                        ],
                        input="Complete this text task",
                        cwd=workspace,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=100,
                    )
                    projection = root / f"headless{index}.jsonl"
                    projection.write_text(result.stdout)
                    if index == 3:
                        failed = outcome(projection, result.returncode)
                        self.assertFalse(failed["ok"])
                        error = json.dumps(failed["reason"])
                        self.assertIn("cuda prefill state reset failed", error)
                        self.assertIn("progress log:", error)
                        self.assertNotIn("completed steps=", error)
                        self.assertEqual(len(requests), requests_before)
                        failed_report = next(
                            path
                            for path in (plugin / "cycles").glob("*/result.json")
                            if json.loads(path.read_text()).get("ok") is False
                        )
                        self.assertIn(
                            "completed steps=", failed_report.parent.with_suffix(".log").read_text()
                        )
                        continue
                    self.assertTrue(
                        outcome(projection, result.returncode)["ok"],
                        result.stderr[-7000:] + result.stdout[-3000:],
                    )
                    executed = json.loads(status.read_text())
                    serialized = json.dumps(executed)
                    visible = json.dumps(requests[-1])
                    if index == 0:
                        self.assertIn("ON", serialized)
                        self.assertIn("NATIVE_MODE_GUIDANCE", visible)
                        self.assertIn("Chosen cycle response", visible)
                    elif index == 1:
                        self.assertIn("OFF", serialized)
                        self.assertIn("Usage:", serialized)
                        self.assertNotIn("NATIVE_MODE_GUIDANCE", visible)
                        self.assertNotIn("Harness² mode is enabled", visible)
                    else:
                        self.assertIn("Harness\\u00b2 full cycle completed", visible)
                        real_report = next(
                            path
                            for path in (plugin / "cycles").glob("*/result.json")
                            if json.loads(path.read_text()).get("summary")
                        )
                        report = json.loads(real_report.read_text())
                        self.assertTrue(report["ok"])
                        self.assertEqual(report["summary"]["status"], "ok")
                        self.assertTrue(
                            list(real_report.parent.glob("audit/dsh-*/session.headless.jsonl")),
                            "Improver trajectories must survive inside the cycle directory",
                        )
                        self.assertIn(
                            "[harness2] completed after",
                            real_report.parent.with_suffix(".log").read_text(),
                        )
                        self.assertTrue((Path(report["harness"]) / "systemprompt.md").is_file())
                        self.assertGreaterEqual(len(requests), 7)
                        expected_guidance = (
                            (Path(__file__).parent / "expected/dsh-final-response.txt")
                            .read_text()
                            .strip()
                        )
                        execution_requests = []
                        for request in requests[requests_before:]:
                            system = "\n".join(
                                message["content"]
                                for message in request["messages"]
                                if message.get("role") == "system"
                                and isinstance(message.get("content"), str)
                            )
                            if "Final response format for this project-copy execution:" in system:
                                self.assertIn(expected_guidance, system)
                                execution_requests.append(request)
                        self.assertEqual(len(execution_requests), 4)
                        self.assertEqual(
                            [
                                request.get("max_completion_tokens", request.get("max_tokens"))
                                for request in execution_requests
                            ],
                            [1024] * 4,
                        )
            self.assertEqual(len(list((plugin / "cycles").glob("*.request.json"))), 3)


if __name__ == "__main__":
    unittest.main()
