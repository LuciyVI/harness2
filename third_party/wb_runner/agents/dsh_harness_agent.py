"""WorkBuddy/Harbor dsh agent: private headless profile and durable event capture."""

from __future__ import annotations

import json
import shlex
import shutil
import tarfile
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from workbuddy_bench.agents import dsh_runtime as runtime
from workbuddy_bench.agents._agent_user import ensure_agent_user


class DshHarnessAgent(BaseInstalledAgent):
    SUPPORTS_ATIF = False

    def __init__(self, logs_dir: Path, *args, **kwargs):
        self._source_home = Path(kwargs.pop("DSH_SOURCE_HOME"))
        self._source_profile = kwargs.pop("DSH_SOURCE_PROFILE", "headless")
        self._model = kwargs.pop("DSH_MODEL", "profile/default")
        self._permission = kwargs.pop("DSH_PERMISSION_MODE", "workspace-write")
        self._patches = tuple(
            Path(p).expanduser().resolve()
            for p in (kwargs.pop("DSH_PATCH", ""), kwargs.pop("DSH_CONTAINER_PATCH", ""))
            if p
        )
        ws = kwargs.pop("WB_HARNESS_WS", None)
        self._harness = Path(ws) if ws else None
        version = kwargs.pop("DSH_VERSION", None)
        for key in (
            "connection",
            "model_params",
            "context_window",
            "context_compact_pct",
            "settings_preset",
            "models_preset",
            "mount_path",
            "instance_id",
        ):
            kwargs.pop(key, None)
        super().__init__(logs_dir, *args, version=version, **kwargs)

    @staticmethod
    def name() -> str:
        return "dsh-harness"

    def get_version_command(self) -> str:
        return "/opt/dsh/node /opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js --version"

    async def install(self, environment: BaseEnvironment) -> None:
        result = await self.exec_as_root(environment, command=self.get_version_command())
        if result.return_code:
            raise RuntimeError(
                "Mounted dsh runtime is unavailable; rerun harness2-setup wb --substrate dsh"
            )
        await ensure_agent_user(self, environment)

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        target = Path("/tmp/harness2-dsh")
        with tempfile.TemporaryDirectory(prefix="harness2-wb-dsh-") as tmp:
            state = Path(tmp) / "state"
            text = ""
            if self._harness:
                text = "\n\n".join(
                    (self._harness / p).read_text()
                    for p in (
                        "systemprompt.md",
                        "guardrails.md",
                        "LongTermMEMORY.md",
                        "tool_descriptions/tool_guidance.md",
                    )
                    if (self._harness / p).is_file()
                )
            env = runtime.prepare_session(
                state,
                source_home=self._source_home,
                source_profile=self._source_profile,
                text=text,
                model=self._model,
                target_state=target,
                copy_plugins=True,
                patch_files=self._patches,
            )
            if self._harness:
                for entry in ("skills", "scripts"):
                    if (self._harness / entry).is_dir():
                        shutil.copytree(self._harness / entry, state / entry)
            (state / "task.txt").write_text(self.render_instruction(instruction))
            archive = Path(tmp) / "state.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(state, arcname=".")
            await environment.upload_file(
                source_path=archive, target_path="/tmp/harness2-dsh.tar.gz"
            )
            result = await self.exec_as_root(
                environment, command="chmod 644 /tmp/harness2-dsh.tar.gz"
            )
            if result.return_code:
                raise RuntimeError("Cannot prepare dsh state archive")
            exports = {
                **env,
                "HARNESS2_DSH_EVENTS": "/logs/agent/dsh-output.jsonl",
                "DSH_PERMISSION_MODE": self._permission,
            }
            export_cmd = " ".join(f"{key}={shlex.quote(value)}" for key, value in exports.items())
            patches = " ".join(
                f"--patch {shlex.quote(str(target / f'extra-{i}.yml'))}"
                for i in range(len(self._patches))
            )
            command = (
                "set -e; umask 077; mkdir -p /tmp/harness2-dsh && "
                "tar -xzf /tmp/harness2-dsh.tar.gz -C /tmp/harness2-dsh && "
                "rm /tmp/harness2-dsh.tar.gz && "
                "if [ -d /tmp/harness2-dsh/scripts ]; then "
                "mkdir -p /workspace/scripts && cp -r /tmp/harness2-dsh/scripts/. /workspace/scripts/; fi; "
                "cd /workspace && "
                "if git rev-parse --git-dir >/dev/null 2>&1; then "
                "git add -A && { git diff --cached --quiet || "
                "git -c user.email=harness@bench -c user.name=harness commit -q -m 'harness install (pre-agent)'; }; fi; "
                f"env {export_cmd} /opt/dsh/node /opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js "
                f"--profile harness2 {patches} --patch /tmp/harness2-dsh/overlay.json --json "
                "< /tmp/harness2-dsh/task.txt > /logs/agent/dsh-headless.jsonl "
                "2> /logs/agent/dsh-stderr.log"
            )
            result = await self.exec_as_agent(environment, command=command)
            # Validate on the host after downloading; a final event alone can describe
            # a failed turn, and must never be treated as successful execution.
            local_projection = Path(tmp) / "headless.jsonl"
            await environment.download_file(
                source_path="/logs/agent/dsh-headless.jsonl", target_path=local_projection
            )
            outcome = runtime.outcome(local_projection, result.return_code)
            if not outcome["ok"]:
                raise RuntimeError(
                    "dsh trial failed; see agent/dsh-stderr.log and dsh-headless.jsonl"
                )
            local_events = Path(tmp) / "events.jsonl"
            await environment.download_file(
                source_path="/logs/agent/dsh-output.jsonl", target_path=local_events
            )
            if not runtime.durable_complete(local_events, outcome["session_id"]):
                raise RuntimeError("dsh durable trajectory is incomplete")
            complete = Path(tmp) / "complete.json"
            complete.write_text(json.dumps({"ok": True, "session_id": outcome["session_id"]}))
            await environment.upload_file(
                source_path=complete, target_path="/logs/agent/dsh-complete.json"
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        samples = [
            event["usage"]
            for event in runtime.read_events(self.logs_dir / "dsh-headless.jsonl")
            if event.get("type") == "status"
            and event.get("phase") == "step_end"
            and "usage" in event
        ]
        if samples:
            context.n_input_tokens = sum(sample.get("inputTokens", 0) for sample in samples)
            context.n_output_tokens = sum(sample.get("outputTokens", 0) for sample in samples)
