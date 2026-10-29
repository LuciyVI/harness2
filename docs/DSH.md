# DeepSeek Harness executor and improver

Harness² supports `--substrate dsh` for JobBench, LAB, and WorkBuddy-Bench.
Set `HARNESS2_IMPROVER_CHANNEL=dsh` to run the editor, composer, selector, and
experience reflector through dsh as well. The default improver remains OpenCode.

This integration targets `@deepseek-ai/dsh` 0.2.0-rc.2 with its headless profile.
It calls the installed CLI directly. Local setup reuses that installation,
including its transport fixes, instead of installing or updating it with npx.

## Configure

Install the Python package from the repository root:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .
cp .env.dsh.example .env   # when you do not already have a .env
```

Edit `.env` for your installation. For example:

```dotenv
HARNESS2_DSH_COMMAND="node /absolute/path/to/node_modules/@deepseek-ai/dsh/lib/bin.js"
HARNESS2_DSH_HOME=~/.dsh
HARNESS2_DSH_PROFILE=web
HARNESS2_DSH_MODEL=ds4-local/deepseek-v4-pro
HARNESS2_IMPROVER_CHANNEL=dsh
HARNESS2_IMPROVER_MODEL=ds4-local/deepseek-v4-pro
HARNESS2_DSH_PERMISSION_MODE=workspace-write
HARNESS2_RUNS_DIR=runs/deepseek
SEARXNG_BASE_URL=http://127.0.0.1:8888
```

`HARNESS2_DSH_COMMAND` is parsed into arguments without a shell. Use an absolute
entry path to select a specific installation. `dsh` on PATH also works.
`HARNESS2_DSH_HOME` selects the **source** configuration root, and
`HARNESS2_DSH_PROFILE` selects a source profile. The adapter never edits either.
`HARNESS2_DSH_MODEL` and the improver model use dsh's `provider/model` names,
not OpenCode provider names. `profile/default` retains the profile's selection.

Set the credential environment variable named by your provider's `apiKeyEnv`.
For an unauthenticated local server a placeholder may be sufficient if the
client requires a nonempty key. Keep actual credentials out of tracked files.
The configured model endpoint and any tool endpoints must already be reachable.

`HARNESS2_DSH_PATCH` optionally names an extra provider/plugin patch. It is copied
into the private profile and applied before the generated model selection and
Harness² instructions.

## Toggle full cycles from a dsh chat

Install the native command into your existing profile once, using the Python
environment where Harness² is installed:

```bash
source .venv/bin/activate
harness2-dsh-install --profile web
```

Restart dsh after installation, then use these commands in the chat:

```text
/harness2 on
/harness2 status
/harness2 off
```

The mode starts **off** in a new chat. Commands run locally without a model
request. The selection belongs to the current chat and survives resume; a
fork inherits the selection recorded in its history.

With the mode on, each new text task starts the actual Harness² recursion
before dsh's next model request: baseline execution, invariant/domain edits
and probes, composition, final execution and experience reflection. The
default is parallel recursion with `k=1` and a **3600-second limit per
session**; the whole cycle can take longer. At `k=1` the single composed final
is adopted without a separate comparison call. This does not establish that
the result is better than the baseline.

Trial executions use copies of the chat's project directory. The chosen
response, file changes and harness return to the ordinary dsh agent, which
inspects and applies the requested changes to the live project using its
normal tools. `/harness2 off` also cancels an active cycle and stops its owned
dsh processes. A failed cycle keeps its artifacts and reports failure; it does
not silently become a successful ordinary execution. Child solver/improver
sessions cannot start nested cycles.

Choose a project directory in dsh and use `workspace-write` permission mode.
This version accepts text tasks and project files; chat image/file attachments
require ordinary dsh mode. Git projects select tracked files and non-ignored
untracked inputs. Git submodules and nested repositories are excluded without
traversing their contents. Plain directories use the runtime and credential
filters. The snapshot excludes `.git`, virtual environments, `node_modules`,
Erlang `_build`, runtime directories and `.env` credentials.
`snapshot.json` records selection, exclusions, copied links, byte/file counts
and the largest selected files. It is retained when planning exceeds a limit.
Internal symbolic links are relocated into each project copy; links outside
the project and broken links are rejected explicitly. Default limits remain
100 MB and 10,000 entries. Configure `snapshotMaxBytes`, `snapshotMaxFiles`
and project-relative `snapshotExclude` globs in the native `harness2-mode`
settings for a different workspace policy. Whole recent text messages provide up to 64,000
characters of prior chat context; the current task is kept in full.

Artifacts, full trajectories, a library and a result report are under
`$HARNESS2_RUNS_DIR/dsh-mode/cycles/`. `/harness2 status` shows the active cycle
path; the adjacent `<cycle-id>.log` contains progress. Timeout and recursion
budget are configurable in the `harness2-mode` entry's native dsh settings.

Watch all session states without interrupting a cycle:

```bash
watch -n 2 harness2-dsh-status
# Or select the directory shown by /harness2 status:
watch -n 2 harness2-dsh-status --cycle /absolute/path/to/cycles/<cycle-id>
```

In an existing editable checkout, `python -m harness2.cli.dsh_status` also works
before reinstalling the command entry point. The viewer reports completed
steps, the last recorded phase, termination reason, and event age. It includes
legacy improver sessions linked from the cycle log under `/tmp`. New cycles
keep improver recordings in `audit/` alongside execution recordings in
`executions/`. It displays status fields without model text or tool payloads.

`Deep diving` in the outer chat means it is waiting for the full cycle. A
`step_start` heartbeat with an unchanged step count can be waiting for a model
response or a tool; event age measures the recording, not model inactivity.
`max-tokens` means an attempt exhausted the configured model output budget,
even if its session wall timeout has not expired. Failed attempts and retries,
and the terminal cycle result, are printed explicitly in the cycle log.
The viewer includes backend error messages. Failed cycle reports preserve the
recursion summary and execution failure reports; the chat error names the
failed final execution and its backend cause. Progress remains in the linked
log instead of filling the failure card with heartbeat lines.

Project-copy executions finish through the native `harness2_finish` tool,
which concludes the turn without a further model reply. Write the complete
deliverable and verification report in workspace files, then submit a brief
truthful `summary` and a relative `report_path`. The summary limit is
`HARNESS2_DSH_HANDOFF_MAX_CHARS` (default 2000 Unicode characters). The tool
requires an existing nonempty regular report inside the workspace and rejects
symbolic links and outside paths. Later tool calls are blocked after the
successful handoff. Baseline, refined and custom harness executions require a
recorded successful handoff, completed durable turn and zero process exit.
Plain final text and `max-tokens` terminations remain failures. Output token
budgets remain unchanged.

The installer adds a marked block to the profile patch and backs up the
original with mode `0600`. It does not modify the installed npm runtime or
transport fixes. Remove the command with:

```bash
harness2-dsh-install --profile web --uninstall
```

## Run a small complete cycle

This runs real inference through dsh, without downloading a benchmark or
configuring a judge:

```bash
harness2-dsh-demo --output runs/dsh-demo --k 1 --mode parallel --max-tasks 1 --timeout 3600
```

The task reconciles a small CSV ledger. The baseline helper contains an error,
so the model can propose and test improvements to the helper and instructions.
The demonstration exercises the same recursion, selection, and experience
library used by the benchmarks. Success establishes that the integration runs;
it does not establish a benchmark quality gain.

Use `--mode sequential` for sequential recursion. Repeating the same command
resumes completed work. Use a new output directory after changing the model or
profile configuration. The output contains `demo.json`, `audit/`, `executions/`,
`runs/demo-dsh-agg1/`, and the experience `library/`.

The demo uses only `bash` and `skill` by default. Its solver runs the supplied
helper in one shell call and then returns a final response, reducing the schema
and round-trip cost on a slow model server. Add `--full-tools` to compare with
the full source-profile tool set. Benchmark executors keep their full tool set.
The selected tool set participates in run identity.

For the small demo, improvement sessions receive a bounded snapshot of their
context files in the initial message. Files are included whole; paths that do
not fit or are not text are listed as omitted and remain available through
tools. This avoids repeated directory listings and read-only model turns.

The timeout is a wall-clock limit for a complete dsh session, including every
model request and tool call; it does not bound the entire multi-session demo.
The default is 3600 seconds. On a slow local server, a 600-second session budget
can expire after the agent has written a file but before its final response.
Progress is printed every 30 seconds. Each demo
execution retains its `workspace/`, including files created before a timeout;
`dsh_session.result.json` records the failure. On retry, incomplete attempts
are moved to `failed_attempts/`. Such artifacts are not adopted as successful
executions until dsh finishes cleanly.

For a single-worker model server, dsh improvement uses one concurrent session
and one rollout per phase by default. `HARNESS2_PHASE_WIDTH` and
`HARNESS2_PROPOSER_CONCURRENCY` can raise these limits for a server with more
capacity. The recursion mode still determines which candidates are explored.

The separate `harness2-demo` command remains deterministic and uses no model.

## Run benchmarks

For JobBench, configure the judge separately using `HARNESS2_JUDGE_MODEL`,
`HARNESS2_JUDGE_BASE_URL`, and `HARNESS2_JUDGE_API_KEY` (or the existing Vertex
configuration). Then:

```bash
harness2-setup jb --substrate dsh
harness2-check --bench jb --substrate dsh
harness2 --bench jb --substrate dsh --k 1 --mode parallel --max-tasks 1
harness2-judge --bench jb --substrate dsh --k 1 --mode parallel
harness2-report --bench jb --substrate dsh --k 1 --mode parallel
```

LAB uses the same selectors with `--bench lab`; its existing document-reader and
judge dependencies still apply. Every LAB dsh workspace includes the runtime
`scripts/read_doc.py`, which takes precedence over editable helpers. LAB's
output rules remain required in the editable harness.

The benchmark checks still require judge configuration before refinement.
With dsh as both executor and improver, setup and preflight do not require
OpenCode, Bun, or Google credentials for the agent sessions. A chosen judge can
still require its own credentials and infrastructure.

### WorkBuddy containers

WorkBuddy uses the `dsh-harness` Harbor agent. Setup stages a **separate pinned
npm runtime** and the host Node executable under `dependencies/dsh/runtime/`;
jobs mount it read-only at `/opt/dsh`. This runtime needs a compatible Linux
container architecture and libc. Local installed-package fixes are not copied
into the separately installed container runtime.

Container profile dependencies must be self-contained: links to packages outside
the profile's `node_modules/` are rejected during preflight. For example, a Web
profile using a plugin linked to a host development checkout needs a separate
portable source profile for WorkBuddy. Local JobBench and LAB runs can retain
those linked plugins.

Set `HARNESS2_DSH_CONTAINER_PATCH` to a provider patch with model and tool URLs
reachable **from inside the containers**. A host loopback URL such as
`127.0.0.1:3083` addresses the container itself. Add endpoint hostnames or IPs to
`HARNESS2_DSH_CONTAINER_ALLOWED_HOSTS` (comma-separated; the default is
`host.docker.internal`). The provider patch must include whatever provider
configuration your dsh installation requires.

`DS4_API_KEY`, `DEEPSEEK_API_KEY`, and `SEARXNG_BASE_URL`, when set, are forwarded
to the container. Their endpoint values must also make sense inside containers.

WorkBuddy's existing Gemini judge routes and Google credentials are still
required. Its solver proxy route is metadata: the dsh agent calls its own model
provider directly.

```bash
harness2-setup wb --substrate dsh --subset code
harness2-check --bench wb --substrate dsh --subset code
harness2 --bench wb --substrate dsh --subset code --k 1 --mode parallel --max-tasks 1
```

## Editable components and evidence

The dsh surface includes:

- `systemprompt.md`, `guardrails.md`, `LongTermMEMORY.md`, and
  `tool_descriptions/tool_guidance.md`, contributed literally to the system
  prompt; template braces in code are preserved.
- `skills/<name>/SKILL.md` and helpers under `skills/<name>/scripts/`, loaded by dsh's native skill
  provider. Valid `name` and `description` frontmatter is required.
- `scripts/*.py`, installed into the task workspace.

Editable subagent definitions and runtime plugins/hooks are not part of this
surface. Source-profile plugins remain available. OpenCode TypeScript plugins
and Codex hooks are not installed into dsh.

Each call gets a private `DSH_HOME`, fresh session, isolated settings and skill
roots, and its own working directory. Base and headless bundles replace the
source profile's application bundle; additional plugin bundles and the provider
patches are retained. Web-only patch entries may be irrelevant to headless and
produce dsh's ordinary configuration diagnostics. For a tailored composition,
select a dedicated source profile.

The bridge records full durable `session/event` payloads before the headless
JSON projection's 8 KiB tool-result limit. Both the raw stream and headless
projection are retained alongside stderr. Completion requires a zero exit code,
a successful `turn_end`, a terminal `final`, and a matching completed durable
trajectory. Timeouts kill the process group. Incomplete executions and failed
improver replies do not become successful cached evidence.

Profile and extra-patch hashes participate in run identity, including dsh
improver caches. Configuration changes require a new run directory. The source
profile, existing sessions, and global skill directories are not modified.

dsh does not report monetary cost. A recorded `cost_usd: 0` means that the
integration has no monetary cost report, rather than that inference is free.

## Validation

Run the deterministic regression suite:

```bash
python -m unittest discover -s tests -v
```

To also test the installed dsh against a local fake inference server:

```bash
HARNESS2_TEST_DSH_COMMAND="node /absolute/path/to/dsh/lib/bin.js" \
  python -m unittest discover -s tests -p test_dsh_installed.py -v
```

This optional test creates a localhost HTTP server, checks literal system
instructions and native skill discovery, exercises a real dsh filesystem tool,
and verifies the complete durable trajectory. It makes no external model calls.
