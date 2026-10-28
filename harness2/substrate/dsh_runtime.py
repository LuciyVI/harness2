"""Standalone dsh launch helpers, also shipped to the WorkBuddy runner.

Only the standard library is used here. Each invocation owns its profile, settings,
sessions and skill roots. Source profiles and installed npm packages are read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

_cancelled = threading.Event()
_process_lock = threading.RLock()
_processes: set[subprocess.Popen] = set()


def cancel_processes() -> None:
    """Stop this worker's owned dsh groups, and refuse further launches."""
    _cancelled.set()
    with _process_lock:
        for proc in tuple(_processes):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

APP_BUNDLES = {
    "@deepseek-ai/dsh-web-app",
    "@deepseek-ai/dsh-headless",
    "@deepseek-ai/dsh-sdk-app",
    "@deepseek-ai/dsh-sdk-minimal",
    "@deepseek-ai/dsh-acp-app",
}

# Record the durable events before headless's 8 KiB projection truncates tool results.
# The harness contribution deliberately disables template interpolation: code and
# documents may contain literal {{braces}}.
BRIDGE = """import { appendFileSync } from 'node:fs';
export default function harness2(ctx, config) {
  ctx.inject(['systemPrompt'], child => {
    child.systemPrompt.section({name: 'harness2:instructions', order: 10210,
      text: config.text || '', interpolate: false});
  });
  if (config.tools) ctx.on('agent/created', ({agent}) => {
    agent.ctx.tools.restrict({allow: config.tools});
  });
  ctx.on('session/event', (session, event) => {
    appendFileSync(process.env.HARNESS2_DSH_EVENTS,
      JSON.stringify({sessionId: session.id, ...event}) + '\\n');
  });
}
"""


def command_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    binary = shutil.which(str(Path(argv[0]).expanduser())) if argv else None
    if not binary:
        raise RuntimeError(
            "dsh launcher missing; set HARNESS2_DSH_COMMAND to dsh or 'node /path/to/dsh/lib/bin.js'"
        )
    argv[0] = binary
    for index, argument in enumerate(argv[1:], 1):
        path = Path(argument).expanduser()
        if not argument.startswith("-") and path.is_file():
            argv[index] = str(path.resolve())
    return argv


def profile_identity(home: Path, profile: str) -> str:
    """Hash configuration, without exposing credentials in run metadata."""
    digest = hashlib.sha256()
    for name in (
        "cordis.patch.yml",
        f"profiles/{profile}/package.json",
        f"profiles/{profile}/cordis.patch.yml",
    ):
        path = home / name
        digest.update(name.encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def portable_plugin_links(modules: Path) -> list[Path]:
    """Refuse host-specific links before packaging profile plugins for a container."""
    root = modules.resolve()
    links = [path for path in modules.rglob("*") if path.is_symlink()]
    for path in links:
        target = path.resolve()
        if not target.is_relative_to(root):
            raise RuntimeError(
                "dsh container profile has an external plugin link: "
                f"{path}. Select a portable source profile with self-contained dependencies."
            )
    return links


def prepare_session(
    state: Path,
    *,
    source_home: Path,
    source_profile: str,
    text: str,
    model: str = "",
    target_state: Path | None = None,
    copy_plugins: bool = False,
    patch_files: tuple[Path, ...] = (),
    tools: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Create a private base+headless profile, retaining source provider/plugin patches.

    `target_state` permits packaging the state for a container at another path.
    UI-specific overrides in a web profile must be removed in the source profile
    chosen for automation; required plugin failures remain fatal.
    """
    if (
        not source_profile
        or source_profile in (".", "..")
        or any(c in source_profile for c in "/\\")
    ):
        raise ValueError("HARNESS2_DSH_PROFILE must be a profile name")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    target = target_state or state
    source = source_home / "profiles" / source_profile
    if not source.is_dir() and source_profile != "headless":
        raise RuntimeError(f"dsh source profile is missing: {source}")
    profile = state / "home" / "profiles" / "harness2"
    profile.mkdir(parents=True, mode=0o700)
    manifest = {}
    if (source / "package.json").is_file():
        manifest = json.loads((source / "package.json").read_text())
    bundles = manifest.get("dsh", {}).get("profile", {}).get("bundles", [])
    if not isinstance(bundles, list) or not all(isinstance(b, str) for b in bundles):
        raise ValueError("dsh profile bundles must be a list of package names")
    extras = [b for b in bundles if b not in APP_BUNDLES and b != "@deepseek-ai/dsh-base"]
    manifest["name"] = "harness2-private-profile"
    manifest["private"] = True
    manifest["dsh"] = {
        "profile": {
            "bundles": [
                "@deepseek-ai/dsh-base",
                "@deepseek-ai/dsh-headless",
                *extras,
            ]
        }
    }
    (profile / "package.json").write_text(json.dumps(manifest))
    for src, dst in (
        (source / "cordis.patch.yml", profile / "cordis.patch.yml"),
        (source_home / "cordis.patch.yml", state / "home" / "cordis.patch.yml"),
    ):
        if src.is_file():
            shutil.copy2(src, dst)
            dst.chmod(0o600)
    modules = source / "node_modules"
    if modules.is_dir():
        if copy_plugins:
            links = portable_plugin_links(modules)
            shutil.copytree(modules, profile / "node_modules", symlinks=True)
            for link in links:
                copied = profile / "node_modules" / link.relative_to(modules)
                dest = profile / "node_modules" / link.resolve().relative_to(modules.resolve())
                copied.unlink()
                copied.symlink_to(os.path.relpath(dest, copied.parent))
        else:
            (profile / "node_modules").symlink_to(modules.resolve(), target_is_directory=True)
    (state / "bridge.mjs").write_text(BRIDGE)
    patches = [
        {"id": "hmr", "disabled": True},
        {"id": "session-title-llm", "disabled": True},
        {
            "id": "skill-filesystem",
            "config": {
                "includeDefaultRoots": False,
                "customSkillDirs": [str(target / "skills")],
                "watch": False,
            },
        },
        {
            "insert": [
                {
                    "id": "harness2-bridge",
                    "name": str(target / "bridge.mjs"),
                    "config": {
                        "text": text,
                        **({"tools": list(tools)} if tools is not None else {}),
                    },
                }
            ]
        },
    ]
    if model and model != "profile/default":
        provider, sep, model_id = model.partition("/")
        if not sep or not provider or not model_id:
            raise ValueError("dsh model must be provider/model, or profile/default")
        patches.append(
            {"id": "agent-default-model", "config": {"provider": provider, "model": model_id}}
        )
    (state / "overlay.json").write_text(json.dumps(patches))
    for index, patch in enumerate(patch_files):
        shutil.copy2(patch, state / f"extra-{index}.yml")
        (state / f"extra-{index}.yml").chmod(0o600)
    return {
        "DSH_HOME": str(target / "home"),
        "DSH_AGENTS_HOME": str(target / "agents"),
        "DSH_TELEMETRY": "0",
        # A source profile may contain the interactive Harness² switch. Children
        # already belong to a cycle and must never recursively start another one.
        "HARNESS2_DSH_CHILD": "1",
    }


def read_events(path: Path):
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict):
                yield event


def outcome(path: Path, returncode: int) -> dict:
    events = list(read_events(path))
    final = next((e for e in reversed(events) if e.get("type") == "final"), None)
    ended = next(
        (e for e in reversed(events) if e.get("type") == "status" and e.get("phase") == "turn_end"),
        None,
    )
    errors = [
        str(e.get("message", e.get("code", "dsh error")))
        for e in events
        if e.get("type") == "error"
    ]
    reason = (ended or {}).get("reason")
    reason_kind = reason.get("kind") if isinstance(reason, dict) else reason
    ok = (
        returncode == 0
        and final is not None
        and ended is not None
        and any(e.get("type") == "session" for e in events)
        and not errors
        and reason_kind == "completed"
    )
    return {
        "ok": ok,
        "returncode": returncode,
        "text": str((final or {}).get("text", "")),
        "reason": (ended or {}).get("reason", "missing turn_end"),
        "errors": errors,
        "session_id": next(
            (e.get("sessionId") for e in events if e.get("type") == "session"), None
        ),
    }


def durable_complete(path: Path, session_id: str) -> bool:
    last = None
    for event in read_events(path):
        if event.get("sessionId") == session_id and event.get("type") == "turn/end":
            last = (event.get("data") or {}).get("reason")
    return isinstance(last, dict) and last.get("kind") == "completed"


def run_process(
    command: list[str],
    *,
    workspace: Path,
    env: dict[str, str],
    prompt: str,
    stdout: Path,
    stderr: Path,
    timeout: int,
    on_wait: Callable[[float], None] | None = None,
) -> int:
    """Bound the entire process group, including shell tools spawned by dsh."""
    stdout.parent.mkdir(parents=True, exist_ok=True)
    with stdout.open("w") as out, stderr.open("w") as err:
        with _process_lock:
            if _cancelled.is_set():
                raise RuntimeError("dsh worker was cancelled")
            proc = subprocess.Popen(
                command,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=out,
                stderr=err,
                text=True,
                start_new_session=True,
            )
            _processes.add(proc)
            if _cancelled.is_set():
                os.killpg(proc.pid, signal.SIGKILL)
        try:
            deadline = time.monotonic() + timeout
            started = time.monotonic()
            pending_input = prompt
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    proc.communicate(
                        input=pending_input, timeout=min(30, remaining) if on_wait else remaining
                    )
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout) from None
                    if on_wait:
                        on_wait(time.monotonic() - started)
        except BaseException:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise
        finally:
            with _process_lock:
                _processes.discard(proc)
    return proc.returncode


def render_events(path: Path, *, obs_trunc: int, max_chars: int, args_trunc: int = 2000) -> str:
    """Render full durable events or headless JSON, explicitly labelling bounded evidence."""
    parts = []
    used = 0
    for event in read_events(path):
        kind = event.get("type")
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        block = ""
        if kind == "tool/call":
            block = (
                f"[ACTION] {data.get('name', '')}\n{str(data.get('arguments', ''))[:args_trunc]}"
            )
        elif kind == "tool/result" and event.get("surfaceOp", "append") == "append":
            message = data.get("message") or {}
            content = message.get("content") or []
            body = "".join(str(b.get("text", "")) for b in content if b.get("type") == "text")
            block = f"[OBSERVATION] call={message.get('toolCallId', '')}\n{body[:obs_trunc]}"
            if len(body) > obs_trunc:
                block += "\n[observation truncated by configured limit]"
        elif kind == "assistant/message":
            content = (data.get("message") or {}).get("content") or []
            block = "\n".join(
                f"[{'REASONING' if b.get('type') == 'reasoning' else 'ASSISTANT'}]\n{b.get('text', '')}"
                for b in content
                if b.get("type") in ("text", "reasoning")
            )
        elif kind == "tool_call":
            block = f"[ACTION] {event.get('tool', '')}\n{json.dumps(event.get('input'), ensure_ascii=False)[:args_trunc]}"
        elif kind == "tool_result":
            block = f"[OBSERVATION] call={event.get('callId', '')}\n{str(event.get('result', ''))[:obs_trunc]}"
        elif kind in ("text", "thinking", "final"):
            block = (
                f"[{'REASONING' if kind == 'thinking' else 'ASSISTANT'}]\n{event.get('text', '')}"
            )
        elif kind == "error":
            block = f"[ERROR] {event.get('message', '')}"
        elif kind == "turn/end":
            block = f"[TURN END] {data.get('reason', '')}"
        if event.get("truncated"):
            block += "\n[dsh projection truncated this event; content is incomplete]"
        if block:
            remaining = max_chars - used
            if remaining <= 0:
                break
            parts.append(block[:remaining])
            used += len(block) + 2
    return "\n\n".join(parts)
