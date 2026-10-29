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
BRIDGE = """import { appendFileSync, lstatSync } from 'node:fs';
import { resolve, relative, isAbsolute, sep } from 'node:path';
export default function harness2(ctx, config) {
  ctx.inject(['systemPrompt'], child => {
    child.systemPrompt.section({name: 'harness2:instructions', order: 10210,
      text: config.text || '', interpolate: false});
  });
  ctx.on('agent/created', ({agent}) => {
    if (config.tools) agent.ctx.tools.restrict({allow: config.handoffMaxChars
      ? [...config.tools, 'harness2_finish'] : config.tools});
    if (!config.handoffMaxChars) return;
    let closed = false;
    let pending;
    agent.ctx.on('tools/result', (exec, result) => {
      if (exec.callId !== pending) return;
      pending = undefined;
      if (!result.isError && result.concludesTurn === true) closed = true;
    });
    agent.ctx.on('tools/pre-execute', (exec, next) => {
      if (closed) throw new Error('This task already submitted its final handoff.');
      return next();
    });
    agent.ctx.tools.register({
      name: 'harness2_finish',
      description: 'Finish this project-copy task. Save the complete deliverable and verification '
        + 'report in a workspace file first, then call this tool with a short truthful summary '
        + 'and its relative report path. This ends the turn; do not write a final chat essay.',
      parameters: {type: 'object', additionalProperties: false,
        properties: {
          summary: {type: 'string', description: `Outcome, verification and limitations; at most ${config.handoffMaxChars} characters.`},
          report_path: {type: 'string', description: 'Relative path of the existing full report in this workspace; no symbolic links.'},
        }, required: ['summary', 'report_path']},
      output: {
        schema: {type: 'object', additionalProperties: false,
          properties: {version: {type: 'integer'}, summary: {type: 'string'}, report_path: {type: 'string'}},
          required: ['version', 'summary', 'report_path']},
        render: (_args, value) => [{type: 'text', text: JSON.stringify(value)}],
      },
      async execute(args, exec) {
        if (!args || typeof args !== 'object' || Array.isArray(args)
          || Object.keys(args).sort().join(',') !== 'report_path,summary'
          || typeof args.summary !== 'string' || !args.summary.trim()
          || Array.from(args.summary).length > config.handoffMaxChars
          || typeof args.report_path !== 'string' || !args.report_path.trim()) {
          throw new Error(`Provide a nonempty summary of at most ${config.handoffMaxChars} characters and a report_path.`);
        }
        const cwd = exec.agent?.session.header.cwd;
        if (!cwd || isAbsolute(args.report_path)) throw new Error('report_path must be relative to the task workspace.');
        const target = resolve(cwd, args.report_path);
        const rel = relative(cwd, target);
        if (!rel || rel === '..' || rel.startsWith('..' + sep) || isAbsolute(rel)) {
          throw new Error('report_path must remain inside the task workspace.');
        }
        let current = cwd;
        let info;
        for (const part of rel.split(sep)) {
          current = resolve(current, part);
          info = lstatSync(current);
          if (info.isSymbolicLink()) throw new Error('report_path cannot contain symbolic links.');
        }
        if (!info.isFile() || info.size === 0) throw new Error('The full report must be an existing nonempty regular file.');
        exec.signal.throwIfAborted();
        exec.concludeTurn();
        pending = exec.callId;
        return {version: 1, summary: args.summary, report_path: rel};
      },
    });
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
    handoff_max_chars: int | None = None,
) -> dict[str, str]:
    """Create a private base+headless profile, retaining source provider/plugin patches.

    `target_state` permits packaging the state for a container at another path.
    UI-specific overrides in a web profile must be removed in the source profile
    chosen for automation; required plugin failures remain fatal.
    """
    if handoff_max_chars is not None and (
        type(handoff_max_chars) is not int or handoff_max_chars < 1
    ):
        raise ValueError("handoff_max_chars must be a positive integer")
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
                        **({"handoffMaxChars": handoff_max_chars} if handoff_max_chars else {}),
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


def final_handoff(path: Path, workspace: Path, session_id: str, max_chars: int) -> dict:
    """Read a successfully committed final tool result, never partial assistant text."""
    calls = {}
    handoff = None
    completed = False
    for event in read_events(path):
        if event.get("sessionId") != session_id:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if event.get("type") == "turn/start":
            calls, handoff, completed = {}, None, False
        elif event.get("type") == "turn/end":
            reason = data.get("reason")
            completed = isinstance(reason, dict) and reason.get("kind") == "completed"
        elif event.get("type") == "tool/call" and data.get("name") == "harness2_finish":
            calls[data.get("callId")] = True
        elif event.get("type") == "tool/result":
            message = data.get("message") or {}
            if not isinstance(message, dict):
                continue
            if message.get("toolCallId") not in calls or message.get("isError") is not False:
                continue
            content = message.get("content", [])
            if (
                not isinstance(content, list)
                or len(content) != 1
                or not isinstance(content[0], dict)
                or content[0].get("type") != "text"
            ):
                continue
            try:
                value = json.loads(content[0].get("text", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and set(value) == {"version", "summary", "report_path"}:
                handoff = value
    if handoff is None or not completed:
        raise RuntimeError(
            "Missing successful harness2_finish handoff; plain final text is not a completed project-copy result"
        )
    summary, report = handoff["summary"], handoff["report_path"]
    if (
        type(handoff["version"]) is not int
        or handoff["version"] != 1
        or not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > max_chars
        or not isinstance(report, str)
        or not report.strip()
    ):
        raise RuntimeError("Invalid harness2_finish handoff")
    relative = Path(report)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("Handoff report must remain inside the task workspace")
    target = workspace
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise RuntimeError("Handoff report cannot contain symbolic links")
    if not target.is_file() or not target.stat().st_size:
        raise RuntimeError("Handoff report is missing or empty")
    return handoff


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
