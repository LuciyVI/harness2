"""Workspace-copy adapter for full Harness² cycles requested by interactive dsh."""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

from . import config
from .core import runmark, tree
from .core.spec import BenchPrompts, BenchSpec, ChannelSpec
from .snapshot import snapshot as snapshot
from .substrate.dsh import DSH_SURFACE, DshSubstrate, write_base

FINAL_RESPONSE_GUIDANCE = """Final response format for this project-copy execution:
Write the complete deliverable and detailed verification evidence to files in this workspace.
When the requested answer is long, preserve its full text in a workspace file.
Keep the final chat reply under 200 words: summarize the outcome, verification status
and remaining limitations, then give relative paths to the deliverable and report files.
Leave full file contents, patches, logs and detailed tables in those files.
The short reply must preserve an accurate account of incomplete or failed checks.
"""


class DshCycleError(RuntimeError):
    """A failed full cycle retains its summary and execution failure causes."""

    def __init__(self, root: Path, result):
        self.summary = result.to_dict()
        self.failures = []
        finals = {label for mode in result.modes.values() for label in mode.get("finals", [])}
        for path in sorted(root.glob("executions/*/*/*/dsh_session.result.json")):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("ok") is not False:
                continue
            reason = record.get("reason")
            detail = reason.get("error") if isinstance(reason, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else None
            self.failures.append(
                {
                    "stage": path.parent.name,
                    "reason": reason.get("kind") if isinstance(reason, dict) else reason,
                    "error": message or record.get("error") or record.get("status", "failed"),
                    "report": str(path),
                }
            )
        preferred = [failure for failure in self.failures if failure["stage"] in finals]
        primary = (preferred or self.failures)[-1] if self.failures else None
        cause = f"{primary['stage']}: {primary['reason']}: {primary['error']}; " if primary else ""
        super().__init__("Harness² cycle failed: " + cause + "; ".join(result.errors))


def changes(source: Path, workspace: Path) -> str:
    """Readable evidence for adoption; binary and large files remain in the workspace."""

    def inventory(root):
        return {
            p.relative_to(root).as_posix(): p
            for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
        }

    before, after = inventory(source), inventory(workspace)
    parts = []
    for rel in sorted(before.keys() | after.keys()):
        old = before[rel].read_bytes() if rel in before else b""
        new = after[rel].read_bytes() if rel in after else b""
        if old == new and (rel in before) == (rel in after):
            continue
        try:
            if max(len(old), len(new)) > 200_000 or b"\0" in old + new:
                raise UnicodeError()
            diff = "".join(
                difflib.unified_diff(
                    old.decode("utf-8").splitlines(keepends=True),
                    new.decode("utf-8").splitlines(keepends=True),
                    fromfile=f"before/{rel}",
                    tofile=f"after/{rel}",
                )
            )
            parts.append(diff or f"File membership changed: {rel}\n")
        except UnicodeError:
            parts.append(f"Binary or large file changed: {rel} ({len(old)} -> {len(new)} bytes)\n")
    return "\n".join(parts) or "No project file changes.\n"


class DshTaskAdapter:
    def __init__(self, root: Path, prompt: str, *, timeout=3600, base_harness=None):
        self.root, self.prompt, self.timeout = root, prompt, timeout
        self.substrate = DshSubstrate(progress=True)
        channel = ChannelSpec("dsh", config.DSH_MODEL, timeout=timeout, progress=True)
        self.spec = BenchSpec(
            name="dsh-task",
            runs_root=root / "runs",
            install_mode="dsh_profile",
            prompts=BenchPrompts(
                work="complete the user's project task",
                deliverable_word="response and project changes",
            ),
            channels={"proposer": channel, "selector": channel},
        )
        if base_harness is None:
            write_base(
                self.base_harness(),
                "Complete the user's task in this working directory. Inspect relevant files, "
                "make the requested changes and verify them. Return the result and verification evidence. "
                "Work only within this project copy; do not publish or contact external systems.",
            )
        else:
            content = tree.read_tree(DSH_SURFACE, Path(base_harness))
            tree.validate(DSH_SURFACE, content, allow_subagents=False)
            tree.write_tree(DSH_SURFACE, content, self.base_harness(), stubs=False)

    def tasks(self, split="all"):
        return ["task"]

    def domain_of(self, task_id):
        return "project"

    def task_prompt(self, task_id):
        return self.prompt

    def task_files(self, task_id):
        return self.root / "source"

    def base_harness(self):
        return self.root / "base-harness"

    def validate_tree(self, harness):
        pass

    def execute(self, task_id, harness_dir, run_id, *, timeout=None):
        run = self.root / "executions" / run_id
        if run.exists():
            raise ValueError(f"Execution directory already exists: {run}")
        workspace = run / "workspace"
        shutil.copytree(self.task_files(task_id), workspace, symlinks=True)
        result = self.substrate.run_session(
            self.prompt,
            workspace=workspace,
            stream_path=run / "dsh_session.jsonl",
            harness_dir=harness_dir or self.base_harness(),
            system=FINAL_RESPONSE_GUIDANCE,
            timeout=timeout or self.timeout,
        )
        (run / "response.md").write_text(result["text"])
        (run / "changes.diff").write_text(changes(self.task_files(task_id), workspace))
        runmark.mark_complete(
            run,
            run_id=run_id,
            harness_dir=harness_dir,
            extra={"output_fingerprint": runmark.fingerprint(run)},
        )
        return run

    def artifacts_intact(self, run_dir):
        mark = runmark.read_mark(run_dir) or {}
        return mark.get("output_fingerprint") == runmark.fingerprint(run_dir)

    def delivered(self, run_dir):
        path = Path(run_dir) / "response.md"
        return path.is_file() and bool(path.read_text().strip())

    def trajectory(self, run_dir, *, obs_trunc):
        return self.substrate.read_trajectory(
            Path(run_dir) / "dsh_session.jsonl", obs_trunc=obs_trunc, max_chars=8_000_000
        )

    def deliverable_files(self, run_dir):
        return {
            name: (Path(run_dir) / name).read_text() for name in ("response.md", "changes.diff")
        }

    def deliverable_text(self, run_dir):
        return "\n\n".join(self.deliverable_files(run_dir).values())


def selected_result(adapter, result, k):
    selected = result.selections.get("agg") or {}
    if result.status != "ok" or not selected.get("ok"):
        raise DshCycleError(adapter.root, result)
    from .core.schedule import Namespace

    task = Namespace(adapter.spec.name, "dsh", "agg", k, None, "").task_dir(
        adapter.spec.runs_root, "task"
    )
    harness = task / "harnesses" / selected["chosen_label"]
    run = adapter.root / "executions" / selected["chosen_run_id"]
    shutil.copytree(harness, adapter.root / "selected-harness")
    return {
        "ok": True,
        "output": str(adapter.root),
        "workspace": str(run / "workspace"),
        "harness": str(adapter.root / "selected-harness"),
        "response": str(run / "response.md"),
        "changes": str(run / "changes.diff"),
        "summary": result.to_dict(),
    }
