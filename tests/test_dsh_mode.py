"""Interactive dsh mode: project copies, real recursion and managed installation."""

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import config
from harness2.cli.dsh_install import BEGIN, END, install
from harness2.cli.dsh_task import main as run_worker
from harness2.core.proposer import Proposer, Reply
from harness2.core.schedule import run_task
from harness2.dsh_task import DshTaskAdapter, selected_result, snapshot
from harness2.substrate.dsh import write_base

FAKE_SOLVER = r"""
import json, os, sys
from pathlib import Path
sys.stdin.read()
if os.environ.get('TEST_FAIL_FINAL') == '1' and Path.cwd().parent.name == 'agg1':
 reason={'kind':'error','error':{'message':'500: cuda prefill state reset failed','code':'INVALID_REQUEST'}}
 Path(os.environ['HARNESS2_DSH_EVENTS']).write_text(json.dumps({'sessionId':'failed-final','type':'turn/end','data':{'reason':reason}})+'\n')
 print(json.dumps({'type':'session','sessionId':'failed-final'}))
 print(json.dumps({'type':'status','phase':'turn_end','reason':reason}))
 sys.exit(1)
overlay=json.loads(Path(sys.argv[sys.argv.index('--patch')+1]).read_text())
instructions=next(row['insert'][0]['config']['text'] for row in overlay if row.get('insert'))
if 'IMPROVED_GUIDANCE' in instructions:
 Path('app.txt').write_text('fixed\n')
reply='Task result'
if os.environ.get('TEST_LONG_REPORT') == '1':
 if 'Final response format for this project-copy execution:' not in instructions:
  raise SystemExit('Missing concise final response instructions')
 Path('report.txt').write_text('verification evidence\n'*4000)
 reply='Task result. Detailed verification: report.txt.'
events=Path(os.environ['HARNESS2_DSH_EVENTS'])
events.write_text(json.dumps({'sessionId':'native-test','type':'turn/end','data':{'reason':{'kind':'completed'}}})+'\n')
for event in [ {'type':'session','sessionId':'native-test'},
 {'type':'status','phase':'turn_end','reason':{'kind':'completed'}},
 {'type':'final','text':reply} ]:
 print(json.dumps(event))
"""


class Editor(Proposer):
    def call(self, mission, context_dir, label, **kwargs):
        workspace = None
        if label.startswith("experience."):
            text = "NO_ENTRY"
        else:
            workspace = Path(context_dir) / "harness"
            target = workspace / "systemprompt.md"
            target.write_text(target.read_text() + "\nIMPROVED_GUIDANCE\n")
            text = "<notes>Use the improved guidance.</notes>"
        reply = Reply(label=label, text=text, ok=True, attempts=1, workspace=workspace)
        self._calls.append(reply)
        return reply


class DshModeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_complete_cycle_selects_result_without_touching_live_project(self):
        project = self.root / "project"
        project.mkdir()
        (project / "app.txt").write_text("original\n")
        home = self.root / "home"
        profile = home / "profiles/headless"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text("[]\n")
        solver = self.root / "solver.py"
        solver.write_text(FAKE_SOLVER)
        output = self.root / "cycle"
        snapshot(project, output / "source")
        with patch.multiple(
            config,
            DSH_HOME=home,
            DSH_PROFILE="headless",
            DSH_COMMAND=shlex.join([sys.executable, str(solver)]),
            DSH_MODEL="local/test",
            DSH_PATCH="",
            DSH_CONTAINER_PATCH="",
            PHASE_WIDTH=1,
        ):
            adapter = DshTaskAdapter(output, "Fix app.txt", timeout=10)
            result = run_task(
                "task",
                1,
                adapter=adapter,
                prop=Editor(adapter.spec.channels["proposer"]),
                runs_root=adapter.spec.runs_root,
                modes=("agg",),
            )
            selected = selected_result(adapter, result, 1)
        self.assertTrue(selected["ok"], result.to_dict())
        self.assertEqual((Path(selected["workspace"]) / "app.txt").read_text(), "fixed\n")
        self.assertEqual((project / "app.txt").read_text(), "original\n")
        self.assertIn(
            "IMPROVED_GUIDANCE", (Path(selected["harness"]) / "systemprompt.md").read_text()
        )
        self.assertIn("+fixed", Path(selected["changes"]).read_text())

    def test_failed_final_keeps_backend_cause_and_summary_without_adopting_a_base(self):
        project = self.root / "project"
        project.mkdir()
        (project / "app.txt").write_text("original\n")
        home = self.root / "home"
        (home / "profiles/headless").mkdir(parents=True)
        solver = self.root / "solver.py"
        solver.write_text(FAKE_SOLVER)
        output = self.root / "failed-cycle"
        request = self.root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "workspace": str(project),
                    "output": str(output),
                    "prompt": "Fix app.txt",
                    "timeout": 10,
                }
            )
        )
        messages = io.StringIO()
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(messages),
            patch.dict(os.environ, {"TEST_FAIL_FINAL": "1"}),
            patch.multiple(
                config,
                DSH_HOME=home,
                DSH_PROFILE="headless",
                DSH_COMMAND=shlex.join([sys.executable, str(solver)]),
                DSH_MODEL="local/test",
                DSH_PATCH="",
                DSH_CONTAINER_PATCH="",
                PHASE_WIDTH=config.PHASE_WIDTH,
                PROPOSER_CONCURRENCY=config.PROPOSER_CONCURRENCY,
            ),
            patch("harness2.cli.dsh_task.Proposer", Editor),
        ):
            code = run_worker(["--request", str(request)])
        self.assertEqual(code, 1)
        report = json.loads((output / "result.json").read_text())
        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "cycle-failed")
        self.assertIn("agg1: error: 500: cuda prefill state reset failed", report["error"])
        self.assertIn("finals-only", report["error"])
        self.assertIn("cuda prefill state reset failed", messages.getvalue())
        self.assertTrue(report["summary"]["base"])
        self.assertEqual(report["failures"][0]["stage"], "agg1")
        self.assertTrue(Path(report["failures"][0]["report"]).is_file())
        self.assertFalse((output / "selected-harness").exists())
        self.assertEqual((project / "app.txt").read_text(), "original\n")

    def test_custom_harness_hands_off_a_long_report_through_a_short_reply(self):
        project = self.root / "project"
        project.mkdir()
        (project / "app.txt").write_text("original\n")
        output = self.root / "cycle"
        snapshot(project, output / "source")
        home = self.root / "home"
        (home / "profiles/headless").mkdir(parents=True)
        solver = self.root / "solver.py"
        solver.write_text(FAKE_SOLVER)
        custom = write_base(self.root / "custom-harness", "Custom project instructions.")
        original = (custom / "systemprompt.md").read_text()
        with (
            patch.dict(os.environ, {"TEST_LONG_REPORT": "1"}),
            patch.multiple(
                config,
                DSH_HOME=home,
                DSH_PROFILE="headless",
                DSH_COMMAND=shlex.join([sys.executable, str(solver)]),
                DSH_MODEL="local/test",
                DSH_PATCH="",
                DSH_CONTAINER_PATCH="",
                PHASE_WIDTH=1,
            ),
        ):
            adapter = DshTaskAdapter(
                output, "Fix app.txt and write a detailed report", timeout=10, base_harness=custom
            )
            result = run_task(
                "task",
                1,
                adapter=adapter,
                prop=Editor(adapter.spec.channels["proposer"]),
                runs_root=adapter.spec.runs_root,
                modes=("agg",),
            )
            selected = selected_result(adapter, result, 1)
        self.assertTrue(selected["ok"], result.to_dict())
        self.assertEqual(
            (Path(selected["workspace"]) / "report.txt").read_text(),
            "verification evidence\n" * 4000,
        )
        reply = Path(selected["response"]).read_text()
        self.assertLess(len(reply.split()), 200)
        self.assertIn("report.txt", reply)
        self.assertFalse((project / "report.txt").exists())
        self.assertEqual((custom / "systemprompt.md").read_text(), original)

    def test_snapshot_excludes_runtime_dirs_and_refuses_external_links_and_large_projects(self):
        project = self.root / "project"
        project.mkdir()
        (project / "app.py").write_text("print(1)\n")
        (project / ".env").write_text("NOT_A_REAL_SECRET=test")
        (project / "node_modules").mkdir()
        (project / "node_modules/large").write_text("ignored")
        manifest = snapshot(project, self.root / "copy")
        self.assertEqual(manifest["files"], 1)
        self.assertEqual(set(manifest["excluded"]), {".env", "node_modules"})
        with self.assertRaisesRegex(ValueError, "exceeds"):
            snapshot(project, self.root / "large", max_bytes=1)
        outside = self.root / "outside.txt"
        outside.write_text("external")
        (project / "linked").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink leaves the project"):
            snapshot(project, self.root / "linked-copy")

    def test_snapshot_skips_rebar_build_links_and_relocates_internal_links(self):
        project = self.root / "erlang-project"
        (project / "include").mkdir(parents=True)
        (project / "include/app.hrl").write_text("-define(APP, app).\n")
        build = project / "_build/test/lib/app"
        build.mkdir(parents=True)
        (build / "include").symlink_to(project / "include", target_is_directory=True)
        (project / "headers").symlink_to(project / "include", target_is_directory=True)
        (project / "app.hrl").symlink_to(project / "include/app.hrl")
        copied = self.root / "snapshot"
        manifest = snapshot(project, copied)
        self.assertIn("_build", manifest["excluded"])
        self.assertFalse((copied / "_build").exists())
        for name in ["headers", "app.hrl"]:
            self.assertTrue((copied / name).is_symlink())
            self.assertTrue((copied / name).resolve().is_relative_to(copied))
            self.assertFalse(os.readlink(copied / name).startswith("/"))
        import shutil

        workspace = self.root / "execution"
        shutil.copytree(copied, workspace, symlinks=True)
        self.assertEqual((workspace / "headers/app.hrl").read_text(), "-define(APP, app).\n")
        self.assertTrue((workspace / "app.hrl").resolve().is_relative_to(workspace))

    def test_worker_failure_retains_report_without_claiming_success(self):
        project = self.root / "project"
        project.mkdir()
        request = self.root / "request.json"
        output = self.root / "failed"
        request.write_text(
            json.dumps({"workspace": str(project), "output": str(output), "prompt": ""})
        )
        proc = subprocess.run(
            [sys.executable, "-m", "harness2.cli.dsh_task", "--request", str(request)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 1)
        result = json.loads((output / "result.json").read_text())
        self.assertFalse(result["ok"])
        self.assertIn("non-empty", result["error"])

    def test_worker_cancellation_stops_owned_solver_and_retains_failed_report(self):
        project = self.root / "project"
        project.mkdir()
        home = self.root / "home"
        (home / "profiles/headless").mkdir(parents=True)
        pid_file = self.root / "solver.pid"
        solver = self.root / "slow.py"
        solver.write_text(
            "import os,time\nfrom pathlib import Path\nPath(os.environ['TEST_PID_FILE']).write_text(str(os.getpid()))\ntime.sleep(60)\n"
        )
        request = self.root / "request.json"
        output = self.root / "cancelled"
        request.write_text(
            json.dumps(
                {"workspace": str(project), "output": str(output), "prompt": "Test cancellation"}
            )
        )
        env = {
            **os.environ,
            "HARNESS2_DSH_HOME": str(home),
            "HARNESS2_DSH_PROFILE": "headless",
            "HARNESS2_DSH_MODEL": "local/test",
            "HARNESS2_DSH_COMMAND": shlex.join([sys.executable, str(solver)]),
            "TEST_PID_FILE": str(pid_file),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "harness2.cli.dsh_task", "--request", str(request)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not pid_file.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.is_file(), "solver never started")
            pid = int(pid_file.read_text())
            proc.terminate()
            proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 130)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            report = json.loads((output / "result.json").read_text())
            self.assertTrue(report["cancelled"])
            self.assertFalse(report["ok"])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    @unittest.skipUnless(os.environ.get("HARNESS2_TEST_DSH_COMMAND"), "installed dsh required")
    def test_install_is_idempotent_preserves_profile_and_uninstalls_only_owned_block(self):
        home = self.root / "home"
        profile = home / "profiles/web"
        profile.mkdir(parents=True)
        original = "# keep comment\n- id: custom\n  config: {example: keep}\n"
        document = profile / "cordis.patch.yml"
        document.write_text(original)
        args = dict(
            home=home,
            profile="web",
            state=self.root / "plugin",
            command=os.environ["HARNESS2_TEST_DSH_COMMAND"],
            python=sys.executable,
        )
        install(**args)
        installed = document.read_text()
        self.assertIn(original, installed)
        self.assertEqual(installed.count(BEGIN), 1)
        install(**args)
        self.assertEqual(document.read_text(), installed)
        self.assertEqual(document.stat().st_mode & 0o777, 0o600)
        install(**args, uninstall=True)
        self.assertEqual(document.read_text().strip(), original.strip())
        self.assertNotIn(END, document.read_text())


if __name__ == "__main__":
    unittest.main()
