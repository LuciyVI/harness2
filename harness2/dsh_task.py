"""Workspace-copy adapter for full Harness² cycles requested by interactive dsh."""

from __future__ import annotations

import difflib
import os
import shutil
from pathlib import Path

from . import config
from .core import runmark, tree
from .core.spec import BenchPrompts, BenchSpec, ChannelSpec
from .substrate.dsh import DSH_SURFACE, DshSubstrate, write_base

SKIP_DIRS = frozenset(
    (".git", ".venv", "venv", "node_modules", "__pycache__", ".dsh", ".harness2", "runs", "_build")
)


def snapshot(source: Path, dest: Path, *, max_bytes=100_000_000, max_files=10_000) -> dict:
    """Copy project files and relocate internal links without traversing build trees."""
    source, dest = source.resolve(), dest.resolve()
    if not source.is_dir() or source == Path(source.anchor) or source == Path.home():
        raise ValueError("Choose a project directory, rather than the filesystem or home root")
    if source == dest or source.is_relative_to(dest):
        raise ValueError("Snapshot destination must be outside its source or below it")
    files, links, skipped, size = [], [], [], 0

    def visit(directory):
        nonlocal size
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(source).as_posix()
            if path == dest or dest.is_relative_to(path) or path.name in SKIP_DIRS:
                skipped.append(rel)
            elif path.is_symlink():
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ValueError(
                        f"Snapshot contains a broken or cyclic symlink: {rel}"
                    ) from exc
                if not resolved.is_relative_to(source):
                    raise ValueError(f"Snapshot symlink leaves the project: {rel}")
                links.append((path, resolved))
                if len(files) + len(links) > max_files:
                    raise ValueError("Project snapshot exceeds 10000 files and links")
            elif path.is_dir():
                visit(path)
            elif path.is_file():
                if (
                    path.name == ".env"
                    or path.name.startswith(".env.")
                    and not path.name.endswith("example")
                ):
                    skipped.append(rel)
                    continue
                size += path.stat().st_size
                files.append(path)
                if size > max_bytes or len(files) + len(links) > max_files:
                    raise ValueError(
                        "Project snapshot exceeds 100 MB / 10000 files; choose a smaller workspace"
                    )
            else:
                raise ValueError(f"Snapshot contains a special file: {rel}")

    visit(source)
    dest.mkdir(parents=True)
    for path in files:
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    installed_links = []
    for path, resolved in links:
        rel = path.relative_to(source).as_posix()
        if resolved not in files and not any(p.is_relative_to(resolved) for p in files):
            skipped.append(rel)
            continue
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(
            os.path.relpath(dest / resolved.relative_to(source), target.parent),
            target_is_directory=resolved.is_dir(),
        )
        installed_links.append(rel)
    return {"files": len(files), "symlinks": installed_links, "bytes": size, "excluded": skipped}


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
        raise RuntimeError("Harness² cycle failed: " + "; ".join(result.errors))
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
