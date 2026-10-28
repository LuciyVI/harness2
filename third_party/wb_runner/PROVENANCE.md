# workbuddy-bench runner assets

These runners derive from the workbuddy-bench integration and retain its
[upstream license](LICENSE). See the [pinned revision and patches](../../../dependencies/PINNED.md).

The integration adds editable harness installation and configurable models,
paths, and endpoints. Local model-provider names, environment variables, and
log paths use the `harness2` namespace. Setup installs these agents and harness configurations into the WorkBuddy checkout.

The local dsh extension (2026-10-24) adds `agents/dsh_harness_agent.py` and
`configs/harnesses/dsh-harness/`. The runtime patch registers `dsh-harness`
alongside the existing agents. Setup also copies the original Harness²
`harness2/substrate/dsh_runtime.py` helper into the benchmark's agents package.

[ASSETS.json](../../../dependencies/ASSETS.json) records the shipped file checksums.
Verify them with `python scripts/check_provenance.py`.
