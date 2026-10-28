"""Run a full Harness² cycle on copies of a dsh project workspace."""

from __future__ import annotations

import argparse
import json
import signal
from pathlib import Path

from .. import config
from ..core.library import Library
from ..core.proposer import Proposer
from ..core.schedule import run_task
from ..dsh_task import DshTaskAdapter, selected_result, snapshot
from ..substrate import dsh_runtime


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--request", required=True, type=Path, help="JSON: workspace, prompt, output"
    )
    args = parser.parse_args(argv)
    request = json.loads(args.request.read_text())
    root = Path(request["output"]).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        parser.error("output must be a new or empty directory")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cancelled = False

    def stop(signum, frame):
        nonlocal cancelled
        cancelled = True
        dsh_runtime.cancel_processes()

    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    report = {"ok": False, "output": str(root)}
    try:
        prompt = request["prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty text")
        k, timeout = int(request.get("k", 1)), int(request.get("timeout", 3600))
        if k < 1 or timeout < 1:
            raise ValueError("k and timeout must be positive")
        config.PHASE_WIDTH = 1
        config.PROPOSER_CONCURRENCY = 1
        source = Path(request["workspace"]).expanduser().resolve()
        manifest = snapshot(source, root / "source")
        (root / "snapshot.json").write_text(json.dumps(manifest, indent=2) + "\n")
        adapter = DshTaskAdapter(
            root, prompt, timeout=timeout, base_harness=request.get("base_harness") or None
        )
        prop = Proposer(adapter.spec.channels["proposer"])
        result = run_task(
            "task",
            k,
            adapter=adapter,
            prop=prop,
            runs_root=adapter.spec.runs_root,
            modes=("agg",),
            library=Library(root / "library", "project", adapter.substrate.surface()),
        )
        report = selected_result(adapter, result, k)
    except Exception as exc:
        report.update(error=f"{type(exc).__name__}: {exc}")
    finally:
        if cancelled:
            report.update(ok=False, error="Harness² cycle cancelled", cancelled=True)
        (root / "result.json").write_text(json.dumps(report, indent=2) + "\n")
        for s, handler in previous.items():
            signal.signal(s, handler)
    print(json.dumps(report))
    return 130 if cancelled else int(not report["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
