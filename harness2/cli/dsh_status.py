"""Read cycle/session status, including legacy improver traces outside the run directory."""

from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
from pathlib import Path

from .. import config
from ..substrate.dsh_runtime import read_events


def _modified(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def latest_cycle(directory: Path) -> Path | None:
    requests = list(directory.glob("*.request.json")) if directory.is_dir() else []
    if not requests:
        return None
    request = max(requests, key=_modified)
    return request.with_name(request.name.removesuffix(".request.json"))


def session_status(cycle: Path, *, temp_root: Path | None = None) -> list[dict]:
    """Read status fields only; never return model text, tool arguments or observations."""
    paths = set(cycle.glob("executions/*/*/*/*.headless.jsonl"))
    paths.update(cycle.glob("audit/dsh-*/session.headless.jsonl"))
    log = Path(str(cycle) + ".log")
    if log.is_file():
        names = re.findall(r"\[dsh (dsh-[A-Za-z0-9_.-]+)\]", log.read_text(errors="replace"))
        for name in set(names):
            path = (temp_root or Path(tempfile.gettempdir())) / name / "session.headless.jsonl"
            if path.is_file():
                paths.add(path)
    result = []
    for path in sorted(paths, key=_modified):
        try:
            events = list(read_events(path))
            modified = path.stat().st_mtime
        except OSError:
            continue
        statuses = [event for event in events if event.get("type") == "status"]
        last = statuses[-1] if statuses else {}
        reason = last.get("reason", "-")
        reason = reason.get("kind", "-") if isinstance(reason, dict) else reason
        state = "pending"
        status_file = path.with_name(path.name.removesuffix(".headless.jsonl") + ".result.json")
        try:
            record = json.loads(status_file.read_text())
            state = "ok" if record.get("ok") is True else record.get("status", "failed")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            state = "unreadable"
        result.append(
            {
                "stage": path.parent.name,
                "steps": sum(event.get("phase") == "step_end" for event in statuses),
                "phase": last.get("phase", "starting"),
                "reason": reason,
                "result": state,
                "event_age_s": max(0, time.time() - modified),
                "path": str(path),
            }
        )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycle", type=Path, help="Specific cycle directory; otherwise the latest")
    parser.add_argument("--cycles", type=Path, default=config.RUNS / "dsh-mode/cycles")
    parser.add_argument("--json", action="store_true", help="Emit metadata as JSON")
    args = parser.parse_args(argv)
    cycle = args.cycle.expanduser().resolve() if args.cycle else latest_cycle(args.cycles)
    if cycle is None:
        print("No Harness² cycle found.")
        return 0
    rows = session_status(cycle)
    if args.json:
        print(json.dumps({"cycle": str(cycle), "sessions": rows}, indent=2))
        return 0
    print(f"Cycle: {cycle}")
    try:
        report = json.loads((cycle / "result.json").read_text())
        print(
            "Cycle result: "
            + (
                "completed"
                if report.get("ok") is True
                else "cancelled"
                if report.get("cancelled")
                else "failed"
            )
        )
    except FileNotFoundError:
        print("Cycle result: not written yet (session states below come from recorded events)")
    except (OSError, ValueError, AttributeError):
        print("Cycle result: unreadable or incomplete")
    print(f"{'Stage':42} {'Steps':>5}  {'Phase':12} {'Reason':14} {'Result':10} {'Event age':>10}")
    for row in rows:
        print(
            f"{row['stage'][:42]:42} {row['steps']:>5}  {row['phase']:12} {str(row['reason']):14} {row['result']:10} {row['event_age_s']:>8.0f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
