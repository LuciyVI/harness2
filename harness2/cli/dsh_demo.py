"""Run a small, model-backed Harness² cycle using dsh as executor and improver.

Unlike harness2-demo, this calls the configured model. It needs no benchmark download
or judge. The retained artifacts include raw trajectories, harnesses and library notes.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

from .. import config
from ..core import runmark as RM
from ..core.proposer import Proposer
from ..core.schedule import mode_token, run_domain
from ..core.spec import ChannelSpec
from ..demo import BASE_SCRIPT, DemoAdapter
from ..substrate.dsh import DshSubstrate, write_base


class DshDemoAdapter(DemoAdapter):
    def __init__(
        self, root: Path, *, timeout: int = 3600, full_tools: bool = False, progress: bool = False
    ):
        super().__init__(root)
        tools = None if full_tools else ("bash", "skill")
        self.substrate = DshSubstrate(tool_allowlist=tools, progress=progress)
        channel = ChannelSpec(
            "dsh",
            config.DSH_MODEL,
            timeout=timeout,
            tool_allowlist=tools,
            progress=progress,
            inline_context=True,
        )
        self.spec = replace(
            self.spec,
            install_mode="dsh_profile",
            allow_subagents=False,
            channels={"proposer": channel, "selector": channel},
        )
        self.timeout = timeout
        shutil.rmtree(self.base_harness())
        write_base(
            self.base_harness(),
            "Run python3 scripts/summarize.py ledger.csv > answer.txt in one bash call, "
            "then return a short final message. The helper handles the input; do not "
            "inventory or reread the workspace. Improving the helper is a separate stage.",
        )
        (self.base_harness() / "scripts").mkdir(exist_ok=True)
        (self.base_harness() / "scripts/summarize.py").write_text(BASE_SCRIPT)

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        run_dir = self.root / "executions" / run_id
        if RM.is_complete(run_dir, harness_dir=harness_dir) and self.artifacts_intact(run_dir):
            return run_dir
        if run_dir.exists():
            archive = (
                self.root / "failed_attempts" / f"{run_id.replace('/', '__')}-{time.time_ns()}"
            )
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(run_dir), archive)
        run_dir.mkdir(parents=True, exist_ok=True)
        workspace = run_dir / "workspace"
        workspace.mkdir()
        shutil.copy2(self.task_files(task_id) / "ledger.csv", workspace / "ledger.csv")
        self.substrate.run_session(
            self.task_prompt(task_id) + " Save your result to answer.txt.",
            workspace=workspace,
            stream_path=run_dir / "dsh_session.jsonl",
            harness_dir=harness_dir or self.base_harness(),
            timeout=timeout or self.timeout,
        )
        if not (workspace / "answer.txt").is_file():
            raise RuntimeError("dsh completed without answer.txt; rollout remains incomplete")
        shutil.copy2(workspace / "answer.txt", run_dir / "answer.txt")
        self.execution_count += 1
        RM.mark_complete(
            run_dir,
            run_id=run_id,
            harness_dir=harness_dir,
            extra={
                "output_fingerprint": RM.fingerprint(run_dir),
                "executor_identity": self.substrate.identity(),
            },
        )
        return run_dir

    def artifacts_intact(self, run_dir):
        marker = RM.read_mark(run_dir) or {}
        answer = Path(run_dir) / "answer.txt"
        return (
            answer.is_file()
            and marker.get("output_fingerprint") == RM.fingerprint(run_dir)
            and marker.get("executor_identity") == self.substrate.identity()
        )

    def trajectory(self, run_dir, *, obs_trunc):
        return self.substrate.read_trajectory(
            Path(run_dir) / "dsh_session.jsonl", obs_trunc=obs_trunc, max_chars=8_000_000
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/dsh-demo"))
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--mode", choices=("parallel", "sequential"), default="parallel")
    parser.add_argument("--max-tasks", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="wall-clock limit per dsh session, including all model and tool turns",
    )
    parser.add_argument(
        "--full-tools",
        action="store_true",
        help="use the source profile's complete tool set instead of the small demo tool set",
    )
    args = parser.parse_args(argv)
    if args.k < 1 or args.timeout < 1:
        parser.error("--k and --timeout must be positive")
    adapter = DshDemoAdapter(
        args.output.expanduser().resolve(),
        timeout=args.timeout,
        full_tools=args.full_tools,
        progress=True,
    )
    adapter.substrate.preflight()
    identity = {
        "dsh": adapter.substrate.identity(),
        "k": args.k,
        "mode": args.mode,
        "max_tasks": args.max_tasks,
    }
    path = adapter.root / "config.json"
    if path.is_file() and json.loads(path.read_text()) != identity:
        parser.error("This output belongs to another configuration; choose a new --output")
    path.write_text(json.dumps(identity, indent=2) + "\n")
    proposer = Proposer(
        adapter.spec.channels["proposer"], audit_dir=adapter.root / "audit", attempts=1
    )
    report = run_domain(
        "accounting",
        adapter.tasks()[: args.max_tasks],
        args.k,
        adapter=adapter,
        prop=proposer,
        runs_root=adapter.spec.runs_root,
        library_root=adapter.root / "library" / f"k{args.k}",
        modes=(mode_token(args.mode),),
    )
    report["execution_count"] = adapter.execution_count
    report["editor_calls"] = len(proposer._calls)
    (adapter.root / "demo.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        f"dsh demo: {report['done']} task(s) completed, {report['failed']} failed; {adapter.root}"
    )
    return int(report["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
