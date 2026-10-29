"""Install the native /harness2 on|off|status command into a dsh profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config
from ..substrate.dsh_runtime import command_argv

BEGIN = "# BEGIN harness2-mode (managed by harness2-dsh-install)"
END = "# END harness2-mode"


def install(
    *,
    home: Path,
    profile: str,
    state: Path,
    command: str,
    python: str,
    uninstall=False,
    plugin_source: Path | None = None,
    timeout: int = 10800,
):
    if type(timeout) is not int or timeout < 1:
        raise ValueError("timeout must be a positive integer")
    if not profile or profile in (".", "..") or any(c in profile for c in "/\\"):
        raise ValueError("profile must be a profile name")
    patch = home / "profiles" / profile / "cordis.patch.yml"
    if not patch.is_file():
        raise ValueError(f"Existing dsh profile patch required: {patch}")
    original = patch.read_text()
    updated = original
    if BEGIN in original:
        if original.count(BEGIN) != 1 or original.count(END) != 1:
            raise ValueError("Malformed harness2 managed block; profile left unchanged")
        first, separator, rest = original.partition(BEGIN)
        managed, separator, last = rest.partition(END)
        if not separator or BEGIN in last or END in last:
            raise ValueError("Malformed harness2 managed block; profile left unchanged")
        updated = first.rstrip() + last
    elif END in original or "id: harness2-mode" in original or '"id": "harness2-mode"' in original:
        raise ValueError("An unmanaged harness2-mode entry exists; profile left unchanged")
    if not uninstall:
        argv = command_argv(command)
        modules = None
        for argument in argv:
            path = Path(argument)
            if path.is_file():
                for parent in path.resolve().parents:
                    if parent.name == "node_modules" and (parent / "@deepseek-ai/dsh-llm").is_dir():
                        modules = parent
                        break
        if modules is None:
            raise ValueError(
                "Cannot locate installed dsh modules; use 'node /absolute/path/to/dsh/lib/bin.js'"
            )
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = state / "node_modules"
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            raise ValueError(f"Refusing to replace existing directory: {target}")
        target.symlink_to(modules, target_is_directory=True)
        source = plugin_source or Path(__file__).resolve().parents[1] / "dsh_plugin.mjs"
        shutil.copy2(source, state / "plugin.mjs")
        overlay = [
            {
                "insert": [
                    {
                        "id": "harness2-mode",
                        "name": str(state / "plugin.mjs"),
                        "config": {
                            "python": python,
                            "command": command,
                            "sourceHome": str(home),
                            "sourceProfile": profile,
                            "outputRoot": str(state / "cycles"),
                            "timeout": timeout,
                            "k": 1,
                        },
                    }
                ]
            }
        ]
        # JSON objects are YAML flow nodes. Preserve every byte outside our block.
        body = updated.rstrip()
        if re.match(r"\s*(?:#[^\n]*\n\s*)*\[", body):
            closing = body.rfind("]")
            head = body[:closing].rstrip()
            separator = "" if head.endswith("[") else ", "
            updated = (
                head
                + "\n"
                + BEGIN
                + "\n"
                + separator
                + json.dumps(overlay[0])
                + "\n"
                + END
                + "\n"
                + body[closing:]
                + "\n"
            )
        else:
            updated = body + "\n\n" + BEGIN + "\n- " + json.dumps(overlay[0]) + "\n" + END + "\n"
        # Validate the complete document with the same parser bundled by dsh,
        # without printing provider credentials from the source configuration.
        check = "const yaml=require('js-yaml'); const fs=require('fs'); const doc=yaml.load(fs.readFileSync(0,'utf8')); if(!Array.isArray(doc)) process.exit(2);"
        parsed = subprocess.run(
            [shutil.which("node") or "node", "-e", check],
            input=updated,
            text=True,
            cwd=state,
            capture_output=True,
        )
        if parsed.returncode:
            raise ValueError("Profile must be a YAML patch list; source profile left unchanged")
        subprocess.run(
            [
                shutil.which("node") or "node",
                "--input-type=module",
                "-e",
                f"await import({json.dumps((state / 'plugin.mjs').as_uri())})",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    if updated != original:
        backup = patch.with_name(f"{patch.name}.before-harness2-{time.time_ns()}")
        shutil.copy2(patch, backup)
        backup.chmod(0o600)
        temporary = patch.with_name(f".{patch.name}.harness2-{os.getpid()}")
        try:
            temporary.write_text(updated)
            temporary.chmod(0o600)
            temporary.replace(patch)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "installed": not uninstall,
        "profile": profile,
        "patch": str(patch),
        "state": str(state),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=config.DSH_HOME)
    parser.add_argument("--profile", default=config.DSH_PROFILE)
    parser.add_argument("--state", type=Path, default=config.RUNS / "dsh-mode")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument(
        "--timeout", type=int, default=10800, help="Seconds per child dsh session (default: 10800)"
    )
    parser.add_argument(
        "--plugin-source", type=Path, help="Use a bridge from a dsh source checkout"
    )
    args = parser.parse_args(argv)
    result = install(
        home=args.home.expanduser().resolve(),
        profile=args.profile,
        state=args.state.expanduser().resolve(),
        command=config.DSH_COMMAND,
        python=sys.executable,
        uninstall=args.uninstall,
        plugin_source=args.plugin_source.expanduser().resolve() if args.plugin_source else None,
        timeout=args.timeout,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
