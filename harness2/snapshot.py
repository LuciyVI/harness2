"""Bounded project snapshots, selected from Git metadata without entering submodules."""

from __future__ import annotations

import fnmatch
import heapq
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_BYTES = 100_000_000
DEFAULT_MAX_FILES = 10_000
SKIP_DIRS = frozenset(
    (".git", ".venv", "venv", "node_modules", "__pycache__", ".dsh", ".harness2", "runs", "_build")
)


class SnapshotError(ValueError):
    """A refused or incomplete snapshot, with retained planning diagnostics."""

    def __init__(self, message: str, manifest: dict):
        super().__init__(message)
        self.manifest = manifest


@dataclass
class SnapshotPlan:
    source: Path
    destination: Path
    files: list[Path]
    links: list[tuple[Path, Path]]
    manifest: dict


def _matches(rel: str, pattern: str) -> bool:
    pattern = pattern.removeprefix("./").rstrip("/")
    if pattern.endswith("/**"):
        pattern = pattern[:-3]
    parts = rel.split("/")
    return any(
        fnmatch.fnmatchcase("/".join(parts[:end]), pattern)
        or ("/" not in pattern and fnmatch.fnmatchcase(parts[end - 1], pattern))
        for end in range(1, len(parts) + 1)
    )


def plan_snapshot(
    source: Path,
    dest: Path,
    *,
    max_bytes=DEFAULT_MAX_BYTES,
    max_files=DEFAULT_MAX_FILES,
    exclude: tuple[str, ...] = (),
) -> SnapshotPlan:
    """Inspect selected file metadata only; never traverse a Git submodule's contents."""
    source, dest = source.resolve(), dest.resolve()
    if not source.is_dir() or source == Path(source.anchor) or source == Path.home():
        raise ValueError("Choose a project directory, rather than the filesystem or home root")
    if source == dest or source.is_relative_to(dest):
        raise ValueError("Snapshot destination must be outside its source or below it")
    for name, value in (("max_bytes", max_bytes), ("max_files", max_files)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"snapshot {name} must be a positive integer")
    if not isinstance(exclude, (tuple, list)) or any(
        not isinstance(pattern, str)
        or not pattern
        or pattern.startswith(("/", "\\"))
        or ".." in pattern.replace("\\", "/").split("/")
        for pattern in exclude
    ):
        raise ValueError("snapshot exclude must contain project-relative glob patterns")
    files, links, sizes = [], [], {}
    manifest = {
        "status": "planning",
        "selection": "filesystem",
        "source": str(source),
        "files": 0,
        "bytes": 0,
        "symlinks": [],
        "excluded": [],
        "exclusion_reasons": {},
        "git_submodules": [],
        "exclude_patterns": list(exclude),
        "limits": {"max_bytes": max_bytes, "max_files": max_files},
        "complete": False,
    }

    def omit(rel, reason):
        if rel not in manifest["exclusion_reasons"]:
            manifest["excluded"].append(rel)
            manifest["exclusion_reasons"][rel] = reason

    def fail(message, reason, rel=""):
        manifest.update(status="rejected", reason=reason, offending_path=rel)
        manifest["largest_files"] = [
            {"path": name, "bytes": size}
            for name, size in heapq.nlargest(10, sizes.items(), key=lambda item: item[1])
        ]
        raise SnapshotError(message, manifest)

    gitlinks = set()

    def excluded(rel):
        parts = Path(rel).parts
        if any(rel == link or rel.startswith(link + "/") for link in gitlinks):
            return "git-submodule"
        if any(part in SKIP_DIRS for part in parts):
            return "runtime-directory"
        if any(
            part == ".env" or part.startswith(".env.") and not part.endswith("example")
            for part in parts
        ):
            return "credential-file"
        if any(_matches(rel, pattern) for pattern in exclude):
            return "configured-exclusion"
        path = source / rel
        if path == dest or dest.is_relative_to(path) or path.is_relative_to(dest):
            return "snapshot-output"
        return None

    git = shutil.which("git")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        )
    }
    env["GIT_OPTIONAL_LOCKS"] = "0"

    def git_read(*args, required=True):
        try:
            result = subprocess.run(
                [git, "-c", "core.fsmonitor=false", "-C", str(source), *args],
                capture_output=True,
                timeout=30,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(f"Cannot read Git snapshot metadata: {exc}", "git-metadata")
        if result.returncode and required:
            fail(
                "Cannot read Git snapshot metadata; check repository ownership and Git configuration",
                "git-metadata",
            )
        return result

    # Ancestor mount guards may expose an empty .git directory that Git itself
    # does not recognize as a repository. An actual ancestor has a gitfile or
    # repository metadata; a marker in the selected root is always intentional.
    has_git_marker = (source / ".git").exists() or any(
        (p / ".git").is_file() or (p / ".git/HEAD").is_file() or (p / ".git/config").is_file()
        for p in source.parents
    )
    in_git = git_read("rev-parse", "--is-inside-work-tree", required=False) if git else None
    candidates = None
    if in_git and in_git.returncode == 0 and in_git.stdout.strip() == b"true":
        manifest["selection"] = "git-tracked-and-untracked"
        for record in git_read("ls-files", "--stage", "-z").stdout.split(b"\0"):
            if record and record.split(b" ", 1)[0] == b"160000":
                gitlinks.add(os.fsdecode(record.split(b"\t", 1)[1]).rstrip("/"))
        manifest["git_submodules"] = sorted(gitlinks)
        for rel in sorted(gitlinks):
            omit(rel, "git-submodule")
        for name in git_read(
            "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
        ).stdout.split(b"\0"):
            if name:
                omit(os.fsdecode(name).rstrip("/"), "git-ignore")
        candidates = sorted(
            set(
                os.fsdecode(name)
                for name in git_read(
                    "ls-files", "--cached", "--others", "--exclude-standard", "-z"
                ).stdout.split(b"\0")
                if name
            )
        )
    elif has_git_marker:
        fail(
            "Git is required to read this project's snapshot selection; Git metadata is unavailable",
            "git-metadata",
        )

    def add(path):
        rel = path.relative_to(source).as_posix()
        reason = excluded(rel)
        if reason:
            omit(rel, reason)
            return
        if path.is_symlink():
            # Excluded lexical targets are omitted before resolving the link.
            lexical = Path(os.path.abspath(path.parent / os.readlink(path)))
            if lexical.is_relative_to(source) and excluded(lexical.relative_to(source).as_posix()):
                omit(rel, "excluded-link-target")
                return
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                fail(
                    f"Snapshot contains a broken or cyclic symlink: {rel}: {exc}",
                    "invalid-link",
                    rel,
                )
            if not resolved.is_relative_to(source):
                fail(f"Snapshot symlink leaves the project: {rel}", "external-link", rel)
            if excluded(resolved.relative_to(source).as_posix()):
                omit(rel, "excluded-link-target")
                return
            links.append((path, resolved))
            manifest["symlinks"].append(rel)
        elif path.is_file():
            sizes[rel] = path.stat().st_size
            manifest["bytes"] += sizes[rel]
            files.append(path)
            manifest["files"] = len(files)
        elif path.is_dir():
            omit(rel, "nested-repository")
        elif not path.exists():
            omit(rel, "missing-working-file")
        else:
            fail(f"Snapshot contains a special file: {rel}", "special-file", rel)
        if manifest["bytes"] > max_bytes:
            fail(
                f"Project snapshot exceeds max_bytes={max_bytes}: selected {manifest['bytes']} bytes at {rel}; configure snapshot exclusions or snapshotMaxBytes",
                "max-bytes",
                rel,
            )
        if len(files) + len(links) > max_files:
            fail(
                f"Project snapshot exceeds max_files={max_files}: selected {len(files) + len(links)} entries at {rel}; configure snapshot exclusions or snapshotMaxFiles",
                "max-files",
                rel,
            )

    if candidates is None:

        def visit(directory):
            for path in sorted(directory.iterdir()):
                rel = path.relative_to(source).as_posix()
                reason = excluded(rel)
                if reason:
                    omit(rel, reason)
                elif path.is_symlink() or not path.is_dir():
                    add(path)
                elif (path / ".git").exists():
                    omit(rel, "nested-repository")
                else:
                    visit(path)

        visit(source)
    else:
        for rel in candidates:
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                fail("Git returned a path outside the selected workspace", "git-path", rel)
            add(source / rel)

    copied_links = []
    file_set = set(files)
    for path, resolved in links:
        if resolved in file_set or any(p.is_relative_to(resolved) for p in files):
            copied_links.append((path, resolved))
        else:
            omit(path.relative_to(source).as_posix(), "excluded-link-target")
    manifest["symlinks"] = [p.relative_to(source).as_posix() for p, _ in copied_links]
    manifest["largest_files"] = [
        {"path": name, "bytes": size}
        for name, size in heapq.nlargest(10, sizes.items(), key=lambda item: item[1])
    ]
    manifest.update(status="planned", complete=True)
    return SnapshotPlan(source, dest, files, copied_links, manifest)


def snapshot(source: Path, dest: Path, **options) -> dict:
    """Copy a fully planned snapshot and relocate its internal symbolic links."""
    plan = plan_snapshot(source, dest, **options)
    try:
        plan.destination.mkdir(parents=True)
        for path in plan.files:
            target = plan.destination / path.relative_to(plan.source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        for path, resolved in plan.links:
            target = plan.destination / path.relative_to(plan.source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(
                os.path.relpath(
                    plan.destination / resolved.relative_to(plan.source), target.parent
                ),
                target_is_directory=resolved.is_dir(),
            )
    except OSError as exc:
        plan.manifest.update(status="copy-failed", complete=False, error=str(exc))
        raise SnapshotError(f"Project snapshot copy failed: {exc}", plan.manifest) from exc
    plan.manifest["status"] = "copied"
    return plan.manifest
