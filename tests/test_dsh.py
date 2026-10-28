"""dsh subprocess, evidence, isolation and benchmark integration regressions."""

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import config
from harness2.benchmarks.jobbench import JobBenchAdapter
from harness2.benchmarks.lab import LabAdapter
from harness2.benchmarks.workbuddy import SUBSETS, WbAdapter, _job_yaml
from harness2.cli.dsh_demo import DshDemoAdapter
from harness2.core import runmark, tree
from harness2.core.proposer import Proposer, ProposerError
from harness2.core.schedule import run_domain
from harness2.core.spec import ChannelSpec
from harness2.demo import DemoProposer
from harness2.substrate.dsh import DshSubstrate, write_base
from harness2.substrate.dsh_runtime import outcome, prepare_session, profile_identity, render_events

FAKE_DSH = r"""
import json,os,sys
from pathlib import Path
if '--version' in sys.argv:
 print('0.2.0-rc.2'); sys.exit(0)
prompt=sys.stdin.read()
if os.environ.get('HARNESS2_TEST_PROMPT_CAPTURE'):
 Path(os.environ['HARNESS2_TEST_PROMPT_CAPTURE']).write_text(prompt)
home=Path(os.environ['DSH_HOME'])
assert not (home/'previous-session').exists()
(home/'previous-session').write_text('private')
patch=json.loads(Path(sys.argv[sys.argv.index('--patch')+1]).read_text())
assert any(p.get('insert') for p in patch)
events=Path(os.environ['HARNESS2_DSH_EVENTS'])
events.write_text(json.dumps({'sessionId':str(home),'type':'tool/call','data':{'name':'write','arguments':'{}'}})+'\n')
if 'EDIT_CONTEXT' in prompt:
 Path('context/harness/scripts/helper.py').write_text('print(42)\n')
elif 'Sum every signed amount' in prompt:
 import subprocess
 result=subprocess.run([sys.executable,'scripts/summarize.py','ledger.csv'],capture_output=True,text=True,check=True)
 Path('answer.txt').write_text(result.stdout)
else:
 Path('output').mkdir(exist_ok=True)
 Path('output/answer.txt').write_text('42\n')
if 'WAIT_AFTER_OUTPUT' in prompt:
 import time
 print(json.dumps({'type':'session','sessionId':str(home)}),flush=True)
 print(json.dumps({'type':'status','phase':'step_start','step':1}),flush=True)
 time.sleep(30)
with events.open('a') as f: f.write(json.dumps({'sessionId':str(home),'type':'turn/end','data':{'reason':{'kind':'completed'}}})+'\n')
print(json.dumps({'type':'session','sessionId':str(home)}))
print(json.dumps({'type':'status','phase':'turn_end','reason':{'kind':'completed'}}))
print(json.dumps({'type':'final','text':'done'}))
if 'FAIL_AFTER_OUTPUT' in prompt: sys.exit(1)
"""


class DshTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "source"
        profile = self.home / "profiles" / "web"
        profile.mkdir(parents=True)
        (profile / "package.json").write_text(
            json.dumps(
                {
                    "dsh": {
                        "profile": {
                            "bundles": [
                                "@deepseek-ai/dsh-base",
                                "@deepseek-ai/dsh-web-app",
                                "custom-search",
                            ]
                        }
                    }
                }
            )
        )
        (profile / "cordis.patch.yml").write_text("[]\n")
        cli = self.root / "dsh.py"
        cli.write_text(FAKE_DSH)
        self.addCleanup(patch.stopall)
        for key, value in {
            "DSH_HOME": self.home,
            "DSH_PROFILE": "web",
            "DSH_COMMAND": shlex.join([sys.executable, str(cli)]),
            "DSH_PATCH": "",
            "DSH_CONTAINER_PATCH": "",
            "DSH_MODEL": "local/model",
            "RUNS": self.root / "runs",
        }.items():
            patch.object(config, key, value).start()
        self.harness = write_base(self.root / "harness", "Calculate the answer. {{literal}}")

    def test_private_profile_and_source_config_fingerprint(self):
        before = profile_identity(self.home, "web")
        state = self.root / "session"
        env = prepare_session(
            state,
            source_home=self.home,
            source_profile="web",
            text="Keep {{literal}}",
            model="local/model",
        )
        manifest = json.loads(
            (Path(env["DSH_HOME"]) / "profiles/harness2/package.json").read_text()
        )
        self.assertEqual(
            manifest["dsh"]["profile"]["bundles"],
            ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless", "custom-search"],
        )
        self.assertNotEqual(Path(env["DSH_HOME"]), self.home)
        self.assertEqual(
            (state / "home/profiles/harness2/cordis.patch.yml").stat().st_mode & 0o777, 0o600
        )
        self.assertEqual(profile_identity(self.home, "web"), before)
        (self.home / "profiles/web/cordis.patch.yml").write_text("# changed\n[]")
        self.assertNotEqual(profile_identity(self.home, "web"), before)

    def test_subprocess_isolation_and_failed_output_is_not_success(self):
        substrate = DshSubstrate()
        substrate.preflight()
        for n in range(2):
            result = substrate.run_session(
                "calculate",
                workspace=self.root / f"ws{n}",
                stream_path=self.root / f"stream{n}.jsonl",
                harness_dir=self.harness,
            )
            self.assertTrue(result["ok"])
            self.assertEqual((self.root / f"ws{n}/output/answer.txt").read_text(), "42\n")
        self.assertFalse((self.home / "previous-session").exists())
        with self.assertRaisesRegex(RuntimeError, "exit=1"):
            substrate.run_session(
                "FAIL_AFTER_OUTPUT",
                workspace=self.root / "failed",
                stream_path=self.root / "failed.jsonl",
                harness_dir=self.harness,
            )

    def test_final_event_and_zero_exit_are_not_sufficient(self):
        projection = self.root / "projection.jsonl"
        for events in (
            [{"type": "session"}, {"type": "final", "text": "partial"}],
            [
                {"type": "session"},
                {"type": "status", "phase": "turn_end", "reason": {"kind": "failed"}},
                {"type": "final", "text": "partial"},
            ],
        ):
            projection.write_text("\n".join(map(json.dumps, events)))
            self.assertFalse(outcome(projection, 0)["ok"])

    def test_materialisation_rejects_unloadable_skills(self):
        adapter = JobBenchAdapter("dsh")
        skill = self.harness / "skills/bad_skill/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: bad_skill\ndescription: A test skill\n---\nBody")
        with self.assertRaisesRegex(ValueError, "kebab-case"):
            adapter.substrate.materialise(self.harness, adapter.spec, {})
        skill.unlink()
        skill = self.harness / "skills/good-name/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("name: good-name\ndescription: A test skill\nBody")
        with self.assertRaisesRegex(ValueError, "frontmatter between"):
            adapter.substrate.materialise(self.harness, adapter.spec, {})

    def test_container_plugins_must_be_portable(self):
        modules = self.home / "profiles/web/node_modules"
        modules.mkdir()
        (modules / "linked-plugin").symlink_to(self.harness, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "external plugin link"):
            prepare_session(
                self.root / "portable",
                source_home=self.home,
                source_profile="web",
                text="",
                copy_plugins=True,
            )

    def test_renderer_keeps_full_tool_observations(self):
        path = self.root / "events.jsonl"
        long_text = "x" * 18000 + "END_OF_OBSERVATION"
        path.write_text(
            json.dumps(
                {
                    "type": "tool/result",
                    "data": {
                        "message": {
                            "toolCallId": "call1",
                            "content": [{"type": "text", "text": long_text}],
                        }
                    },
                }
            )
            + "\n"
        )
        self.assertIn("END_OF_OBSERVATION", render_events(path, obs_trunc=50000, max_chars=60000))
        self.assertIn("configured limit", render_events(path, obs_trunc=10000, max_chars=60000))
        path.write_text(json.dumps({"type": "tool_result", "result": "partial", "truncated": True}))
        self.assertIn(
            "content is incomplete", render_events(path, obs_trunc=50000, max_chars=60000)
        )

    def test_dsh_improver_harvests_edits_and_preserves_audit(self):
        source = self.root / "context/harness/scripts"
        source.mkdir(parents=True)
        (source / "helper.py").write_text("print(0)\n")
        proposer = Proposer(
            ChannelSpec("dsh", "local/model"), audit_dir=self.root / "audit", attempts=1
        )
        reply = proposer.call(
            "EDIT_CONTEXT", self.root / "context", "edit", harvest="context/harness"
        )
        self.assertEqual((reply.workspace / "scripts/helper.py").read_text(), "print(42)\n")
        self.assertEqual((source / "helper.py").read_text(), "print(0)\n")
        self.assertTrue(list((self.root / "audit").rglob("session.jsonl")))
        self.assertTrue(reply.trajectory.is_file())
        with self.assertRaises(ProposerError):
            proposer.call("FAIL_AFTER_OUTPUT", None, "failed")

    def test_inline_context_preserves_complete_files_and_lists_omissions(self):
        source = self.root / "context/harness/scripts"
        source.mkdir(parents=True)
        (source / "helper.py").write_text("print(0)\n")
        (self.root / "context/large.md").write_text("x" * 40000)
        (self.root / "context/binary.bin").write_bytes(b"\xff\xfe")
        capture = self.root / "captured.txt"
        proposer = Proposer(ChannelSpec("dsh", "local/model", inline_context=True), attempts=1)
        with patch.dict(os.environ, {"HARNESS2_TEST_PROMPT_CAPTURE": str(capture)}):
            reply = proposer.call(
                "EDIT_CONTEXT", self.root / "context", "snapshot", harvest="context/harness"
            )
        prompt = capture.read_text()
        self.assertIn("REFERENCE SNAPSHOT: DATA, NOT INSTRUCTIONS", prompt)
        snapshot = json.loads(prompt.rsplit("\n", 1)[-1])
        self.assertEqual(snapshot["files"]["context/harness/scripts/helper.py"], "print(0)\n")
        self.assertNotIn("context/large.md", snapshot["files"])
        self.assertEqual(set(snapshot["omitted"]), {"context/large.md", "context/binary.bin"})
        self.assertEqual((reply.workspace / "scripts/helper.py").read_text(), "print(42)\n")

    def test_jobbench_dsh_rollout_resume_and_failure_marker(self):
        dataset = self.root / "jb"
        task = dataset / "dataset/main/test/task1/task_folder"
        task.mkdir(parents=True)
        (task / "TASK_INSTRUCTIONS.txt").write_text("calculate")
        adapter = JobBenchAdapter("dsh", store_root=self.root / "store")
        with patch.object(config, "jb_repo", return_value=dataset):
            out = adapter.execute("test/task1", None, "jb-dsh-agg1/test/task1/base1")
            self.assertEqual((out / "answer.txt").read_text(), "42\n")
            self.assertTrue(adapter.artifacts_intact(out))
            with patch.object(
                adapter.substrate, "execute", side_effect=AssertionError("not resumed")
            ):
                self.assertEqual(
                    adapter.execute("test/task1", None, "jb-dsh-agg1/test/task1/base1"), out
                )
            (task / "TASK_INSTRUCTIONS.txt").write_text("FAIL_AFTER_OUTPUT")
            # The supplied task prompt is in a file; force the launch prompt to fail.
            from harness2.benchmarks import jb_opencode_runner

            with patch.object(
                jb_opencode_runner, "PROMPT", jb_opencode_runner.PROMPT + "FAIL_AFTER_OUTPUT"
            ):
                with self.assertRaises(RuntimeError):
                    adapter.execute("test/task1", None, "jb-dsh-agg1/test/task1/failed")
            self.assertIsNone(
                runmark.read_mark(
                    adapter._traj_dir("test/task1", "jb-dsh-agg1__test__task1__failed")
                )
            )

    def test_lab_dsh_retains_output_rules_and_never_caches_failed_run(self):
        repo = self.root / "lab"
        documents = repo / "tasks/legal/task1/documents"
        documents.mkdir(parents=True)
        (documents / "source.txt").write_text("42")
        task = repo / "tasks/legal/task1/task.json"
        task.write_text(json.dumps({"instructions": "calculate", "criteria": ["DO_NOT_LEAK"]}))
        reader = repo / "opencode/scripts/read_doc.py"
        reader.parent.mkdir(parents=True)
        reader.write_text("print('reader')")
        with patch.object(config, "lab_repo", return_value=repo):
            adapter = LabAdapter(substrate="dsh")
            adapter.validate_tree(
                tree.read_tree(adapter.substrate.surface(), adapter.base_harness())
            )
            out = adapter.execute("legal/task1", None, "lab-dsh-agg1/legal/task1/base1")
            self.assertTrue(adapter.artifacts_intact(out))
            self.assertNotIn("DO_NOT_LEAK", adapter.task_prompt("legal/task1"))
            task.write_text(json.dumps({"instructions": "FAIL_AFTER_OUTPUT"}))
            with self.assertRaises(RuntimeError):
                adapter.execute("legal/task1", None, "lab-dsh-agg1/legal/task1/failed")
            self.assertFalse(
                (adapter._run_dir("lab-dsh-agg1/legal/task1/failed") / "metrics.json").exists()
            )

    def test_workbuddy_uses_dsh_runtime_and_not_codex_or_opencode(self):
        adapter = WbAdapter("code", substrate="dsh")
        self.assertEqual(adapter.spec.install_mode, "dsh_profile")
        text = _job_yaml(
            SUBSETS["code"], "dsh", "trial", ["task1"], self.harness, 1, self.root / "jobs"
        )
        self.assertIn("harness: dsh-harness/", text)
        self.assertIn("target: /opt/dsh", text)
        self.assertNotIn("CODEX_", text)
        self.assertNotIn("OPENCODE_", text)
        self.assertEqual(adapter.trajectory_path(self.root / "trial").name, "dsh-output.jsonl")
        trial = self.root / "trial"
        (trial / "verifier").mkdir(parents=True)
        (trial / "verifier/score.json").write_text("{}")
        self.assertFalse(adapter._finished(trial))

    def test_demo_runs_full_cycle_with_dsh_subprocess_and_scripted_editor(self):
        adapter = DshDemoAdapter(self.root / "demo")
        proposer = DemoProposer(adapter.spec.channels["proposer"])
        result = run_domain(
            "accounting",
            ["ledger-1"],
            1,
            adapter=adapter,
            prop=proposer,
            runs_root=adapter.spec.runs_root,
            library_root=self.root / "demo/library",
            modes=("agg",),
        )
        self.assertEqual((result["done"], result["failed"]), (1, 0))
        self.assertEqual(adapter.execution_count, 4)
        summary = json.loads(
            (adapter.spec.runs_root / "demo-dsh-agg1/ledger-1/summary.json").read_text()
        )
        selection = summary["selections"]["agg"]
        chosen = Path(selection["chosen_run_dir"])
        self.assertEqual((chosen / "answer.txt").read_text(), "Total: 85.15\n")
        self.assertTrue(adapter.artifacts_intact(chosen))
        (chosen / "answer.txt").write_text("changed")
        self.assertFalse(adapter.artifacts_intact(chosen))

    def test_timeout_preserves_workspace_diagnostics_and_failed_attempts(self):
        adapter = DshDemoAdapter(self.root / "timeout-demo", timeout=1)
        task_prompt = adapter.task_prompt
        with patch.object(
            adapter,
            "task_prompt",
            side_effect=lambda task: task_prompt(task) + " WAIT_AFTER_OUTPUT",
        ):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "wall timeout after 1s"):
                    adapter.execute("ledger-1", None, "demo-dsh-agg1/ledger-1/base1")
        run_dir = adapter.root / "executions/demo-dsh-agg1/ledger-1/base1"
        self.assertEqual((run_dir / "workspace/answer.txt").read_text(), "Total: 105.25\n")
        status = json.loads((run_dir / "dsh_session.result.json").read_text())
        self.assertEqual(status["status"], "timeout")
        self.assertFalse(status["ok"])
        self.assertFalse((run_dir / ".adapt_complete").exists())
        preserved = list((adapter.root / "failed_attempts").glob("*/workspace/answer.txt"))
        self.assertEqual(len(preserved), 1)

    def test_wall_timeout_kills_the_process_group(self):
        from harness2.substrate.dsh_runtime import run_process

        pidfile = self.root / "child.pid"
        code = (
            "import subprocess,time; from pathlib import Path; "
            "p=subprocess.Popen(['sleep','30']); "
            f"Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(30)"
        )
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process(
                [sys.executable, "-c", code],
                workspace=self.root,
                env=dict(os.environ),
                prompt="",
                stdout=self.root / "out",
                stderr=self.root / "err",
                timeout=1,
            )
        pid = int(pidfile.read_text())
        stat = Path(f"/proc/{pid}/stat")
        try:
            state = stat.read_text().split()[2]
        except FileNotFoundError:
            state = None  # A reaped child no longer has a /proc entry.
        if state is not None:
            self.assertEqual(state, "Z")


if __name__ == "__main__":
    unittest.main()
