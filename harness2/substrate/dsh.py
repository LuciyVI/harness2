"""DeepSeek Harness substrate: private headless profiles and full durable trajectories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping

from .. import config
from ..core import runmark as RM
from ..core import tree as HT
from ..core.tree import SurfaceSpec
from . import dsh_runtime as runtime
from .base import Install, Launcher, RunRequest

DSH_SURFACE = SurfaceSpec(
    name="dsh",
    always_on=(
        "systemprompt.md",
        "guardrails.md",
        "LongTermMEMORY.md",
        "tool_descriptions/tool_guidance.md",
    ),
    always_on_max=24_000,
    always_on_cap_reason="the complete instruction block accompanies each model step",
    skill_dir="skills",
    skill_name_rule="must_match",
    trigger_hint="Name `{name}` in systemprompt.md or guardrails.md and load it with the skill tool.",
    autotrigger_file="guardrails.md",
    trigger_line="- When {desc}, load the `{name}` skill.\n",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    compile_skill_scripts=True,
    skill_name_pattern=r"[a-z0-9]+(?:-[a-z0-9]+)*",
    require_skill_frontmatter=True,
    stubs=(
        ("scripts/README.md", "<!-- Python helpers: python3 scripts/<name>.py -->\n"),
        (
            "skills/README.md",
            "<!-- Native dsh skills: skills/<name>/SKILL.md with name and description frontmatter. -->\n",
        ),
    ),
)


class DshSubstrate:
    name = "dsh"

    def __init__(
        self,
        launcher: Launcher | None = None,
        *,
        tool_allowlist: tuple[str, ...] | None = None,
        progress: bool = False,
    ):
        self._launcher = launcher
        self.tool_allowlist = tool_allowlist
        self.progress = progress

    def surface(self) -> SurfaceSpec:
        return DSH_SURFACE

    def materialise(self, tree_dir: Path, spec, ctx: Mapping[str, str]) -> Install:
        if spec.install_mode != "dsh_profile":
            raise ValueError("DshSubstrate requires install_mode='dsh_profile'")
        if not HT.is_tree(DSH_SURFACE, tree_dir):
            raise ValueError(f"{tree_dir} is not a dsh harness tree")
        staged = Path(tempfile.mkdtemp(prefix="adapt-dsh-"))
        try:
            tree = HT.read_tree(DSH_SURFACE, tree_dir)
            HT.validate(
                DSH_SURFACE,
                tree,
                allow_subagents=False,
                reserved_scripts=getattr(spec, "reserved_scripts", HT.RESERVED_SCRIPTS),
            )
            HT.write_tree(DSH_SURFACE, tree, staged, stubs=False)
            for key, body in ctx.items():
                if key.startswith("file:"):
                    dest = staged / HT.normalize_rel(key[5:])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(body)
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
        return Install(staged, RM.fingerprint(tree_dir), {"install_mode": "dsh_profile"})

    def execute(
        self, task_ref: str, install: Install, run_ids: list[str], timeout: int
    ) -> list[Path]:
        if self._launcher is None:
            raise RuntimeError("DshSubstrate requires a benchmark-supplied launcher")
        return self._launcher(RunRequest(task_ref, tuple(run_ids), int(timeout)), install)

    def read_trajectory(
        self, stream_path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000
    ) -> str:
        return runtime.render_events(
            stream_path, obs_trunc=obs_trunc, max_chars=max_chars, args_trunc=args_trunc
        )

    def preflight(self) -> None:
        argv = runtime.command_argv(config.DSH_COMMAND)
        if (
            config.DSH_PROFILE != "headless"
            and not (config.DSH_HOME / "profiles" / config.DSH_PROFILE).is_dir()
        ):
            raise RuntimeError(
                f"dsh source profile missing: {config.DSH_HOME / 'profiles' / config.DSH_PROFILE}"
            )
        try:
            result = subprocess.run(
                [*argv, "--version"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("Cannot run HARNESS2_DSH_COMMAND --version") from exc
        if result.returncode:
            raise RuntimeError("HARNESS2_DSH_COMMAND --version failed")

    def identity(self) -> dict:
        import hashlib

        patches = {
            name: hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
            for name, path in (
                ("patch", config.DSH_PATCH),
                ("container_patch", config.DSH_CONTAINER_PATCH),
            )
            if path
        }
        identity = {
            "command": config.DSH_COMMAND,
            "model": config.DSH_MODEL,
            "profile": config.DSH_PROFILE,
            "config_sha256": runtime.profile_identity(config.DSH_HOME, config.DSH_PROFILE),
            "patches": patches,
            "permission_mode": config.DSH_PERMISSION_MODE,
        }
        if self.tool_allowlist is not None:
            identity["tool_allowlist"] = list(self.tool_allowlist)
        return identity

    def run_session(
        self,
        prompt: str,
        *,
        workspace: Path,
        stream_path: Path,
        harness_dir: Path | None = None,
        model: str | None = None,
        system: str = "",
        timeout: int = 3600,
        extra_env: Mapping[str, str] | None = None,
        runtime_files: Mapping[str, Path] | None = None,
        handoff_max_chars: int | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """One fresh session; failed/partial runs raise and cannot become cached evidence."""
        workspace = Path(workspace).resolve()
        stream_path = Path(stream_path).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        stream_path.parent.mkdir(parents=True, exist_ok=True)
        stream_path.unlink(missing_ok=True)
        text = system
        if harness_dir is not None:
            harness = HT.read_tree(DSH_SURFACE, harness_dir)
            text = "\n\n".join(filter(None, (HT.always_on_text(DSH_SURFACE, harness), system)))
            for rel, body in harness.items():
                if rel.startswith(("scripts/", "skills/")):
                    path = workspace / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(body)
        for rel, source in (runtime_files or {}).items():
            path = workspace / HT.normalize_rel(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, path)
        projection = stream_path.with_suffix(".headless.jsonl")
        stderr = stream_path.with_suffix(".stderr.log")
        status_path = stream_path.with_suffix(".result.json")
        started = time.monotonic()

        def notice(message):
            if self.progress:
                print(f"  [dsh {stream_path.parent.name}] {message}", file=sys.stderr, flush=True)

        def progress(elapsed):
            try:
                events = list(runtime.read_events(projection))
                age = max(0, time.time() - projection.stat().st_mtime)
            except OSError:
                notice(f"{elapsed:.0f}s; event log is not readable yet")
                return
            statuses = [event for event in events if event.get("type") == "status"]
            completed = sum(event.get("phase") == "step_end" for event in statuses)
            phase = statuses[-1].get("phase", "starting") if statuses else "starting"
            notice(
                f"{elapsed:.0f}s; completed steps={completed}; {phase}; last recorded event {age:.0f}s ago"
            )

        if self.progress:
            notice(f"starting; timeout={timeout}s; events={projection}")
        with tempfile.TemporaryDirectory(prefix="harness2-dsh-state-") as tmp:
            state = Path(tmp)
            patches = (Path(config.DSH_PATCH).expanduser().resolve(),) if config.DSH_PATCH else ()
            overrides = runtime.prepare_session(
                state,
                source_home=config.DSH_HOME,
                source_profile=config.DSH_PROFILE,
                text=text,
                model=config.DSH_MODEL if model is None else model,
                patch_files=patches,
                tools=self.tool_allowlist,
                handoff_max_chars=handoff_max_chars,
                max_tokens=max_tokens,
            )
            skills = workspace / "skills"
            if skills.is_dir():
                (state / "skills").symlink_to(skills, target_is_directory=True)
            env = {
                **os.environ,
                **(extra_env or {}),
                **overrides,
                "HARNESS2_DSH_EVENTS": str(stream_path),
                "DSH_PERMISSION_MODE": config.DSH_PERMISSION_MODE,
            }
            env.pop("npm_config_manage_package_manager_versions", None)
            argv = [*runtime.command_argv(config.DSH_COMMAND), "--profile", "harness2"]
            for index in range(len(patches)):
                argv += ["--patch", str(state / f"extra-{index}.yml")]
            argv += ["--patch", str(state / "overlay.json"), "--json"]
            try:
                rc = runtime.run_process(
                    argv,
                    workspace=workspace,
                    env=env,
                    prompt=prompt,
                    stdout=projection,
                    stderr=stderr,
                    timeout=timeout,
                    on_wait=progress if self.progress else None,
                )
            except subprocess.TimeoutExpired:
                error = f"dsh wall timeout after {timeout}s; rollout remains incomplete; workspace={workspace}; trajectory={stream_path}"
                status_path.write_text(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "timeout",
                            "timeout_s": timeout,
                            "wall_s": round(time.monotonic() - started, 3),
                            "error": error,
                            "workspace": str(workspace),
                            "trajectory": str(stream_path),
                        },
                        indent=2,
                    )
                    + "\n"
                )
                notice(f"failed; wall timeout after {timeout}s; report={status_path}")
                raise RuntimeError(error) from None
        result = runtime.outcome(projection, rc)
        result["wall_s"] = round(time.monotonic() - started, 3)
        if not result["ok"]:
            reason = result["reason"]
            kind = reason.get("kind") if isinstance(reason, dict) else reason
            if kind == "max-tokens":
                try:
                    detail = runtime.output_limit_details(stream_path, result["session_id"])
                except (OSError, ValueError, TypeError, AttributeError):
                    detail = {"phase": "unknown", "pending_tools": []}
                result["output_limit"] = detail
                pending = detail.get("pending_tools", [])
                target = (
                    f" while emitting {pending[-1]['tool']} arguments"
                    if pending
                    else f" during {detail.get('phase', 'unclassified response')}"
                )
                if pending and pending[-1].get("file_path"):
                    target += f" for {pending[-1]['file_path']}"
                result["error"] = (
                    f"Output token limit exhausted{target}; no truncated call was executed"
                )
            status_path.write_text(
                json.dumps({**result, "status": "failed", "workspace": str(workspace)}, indent=2)
                + "\n"
            )
            notice(
                f"failed in {result['wall_s']:.1f}s; exit={rc}; reason={kind}; report={status_path}"
            )
            explanation = f"; {result['error']}" if result.get("error") else ""
            raise RuntimeError(
                f"dsh failed (exit={rc}, reason={result['reason']}){explanation}; see {stderr}"
            )
        if not runtime.durable_complete(stream_path, result["session_id"]):
            notice(f"failed; incomplete durable trajectory; events={stream_path}")
            status_path.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "status": "incomplete_trajectory",
                        "workspace": str(workspace),
                        "trajectory": str(stream_path),
                    },
                    indent=2,
                )
                + "\n"
            )
            raise RuntimeError(f"dsh durable trajectory is incomplete: {stream_path}")
        if handoff_max_chars:
            try:
                handoff = runtime.final_handoff(
                    stream_path, workspace, result["session_id"], handoff_max_chars
                )
            except RuntimeError as exc:
                status_path.write_text(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "invalid_handoff",
                            "reason": {"kind": "invalid-handoff"},
                            "native_reason": result["reason"],
                            "error": str(exc),
                        },
                        indent=2,
                    )
                    + "\n"
                )
                raise
            result["handoff"] = handoff
            result["text"] = handoff["summary"] + "\n\nFull report: " + handoff["report_path"]
        status_path.write_text(
            json.dumps({k: v for k, v in result.items() if k != "text"}, indent=2) + "\n"
        )
        if self.progress:
            print(
                f"  [dsh {stream_path.parent.name}] completed in {result['wall_s']:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        return result


def write_base(directory: Path, text: str) -> Path:
    return HT.write_tree(
        DSH_SURFACE,
        {
            "systemprompt.md": text,
            "guardrails.md": "",
            "LongTermMEMORY.md": "",
            "tool_descriptions/tool_guidance.md": "",
        },
        directory,
    )
