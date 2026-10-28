// Native dsh 0.2.0-rc.2 plugin. Installed separately from the npm runtime.
import { spawn } from 'node:child_process';
import { appendFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import z from '@deepseek-ai/schemastery';
import { z as zod } from 'zod';
import { createUserMessage } from '@deepseek-ai/dsh-llm';

export const name = 'harness2-mode';
export const inject = ['sessionProjections', 'systemPrompt', 'sandboxPolicy'];
export const Config = z.object({
  python: z.string().required().description('Python executable with harness2 installed'),
  command: z.string().required().description('Installed dsh launcher'),
  sourceHome: z.string().required(),
  sourceProfile: z.string().default('web'),
  outputRoot: z.string().required().description('Cycle artifacts directory'),
  timeout: z.number().step(1).min(1).default(3600).description('Seconds per dsh session'),
  k: z.number().step(1).min(1).default(1),
  snapshotMaxBytes: z.number().step(1).min(1).default(100000000).description('Maximum selected snapshot bytes'),
  snapshotMaxFiles: z.number().step(1).min(1).default(10000).description('Maximum selected snapshot files and links'),
  snapshotExclude: z.array(z.string()).default([]).description('Additional project-relative exclusion globs'),
});

// Fold only successfully completed human commands. Resume and fork inherit the
// same state without a second settings store or private synchronous event reads.
export const modeProjection = {
  key: 'harness2Mode', stateVersion: 1,
  stateSchema: zod.object({
    enabled: zod.boolean(), pending: zod.record(zod.string(), zod.boolean()),
  }).strict(),
  init: () => ({ enabled: false, pending: {} }),
  apply: (state, event) => {
    if (event.type === 'command/run' && event.data.name === 'harness2') {
      const command = (event.data.args || '').trim();
      if (command === 'on' || command === 'off') {
        return { ...state, pending: { ...state.pending, [event.data.commandId]: command === 'on' } };
      }
    }
    if (event.type === 'command/done' && Object.hasOwn(state.pending, event.data.commandId)) {
      const wanted = state.pending[event.data.commandId];
      const pending = { ...state.pending };
      delete pending[event.data.commandId];
      return { enabled: event.data.kind === 'success' ? wanted : state.enabled, pending };
    }
    return state;
  },
};

function clippedFile(path, limit) {
  const text = readFileSync(path, 'utf8');
  return text.length <= limit ? text : `${text.slice(0, limit)}\n[Excerpt; full file: ${path}]`;
}

export function runWorker(config, request, env, signal) {
  signal.throwIfAborted();
  mkdirSync(config.outputRoot, { recursive: true, mode: 0o700 });
  const id = randomUUID();
  const requestPath = join(config.outputRoot, `${id}.request.json`);
  const output = join(config.outputRoot, id);
  writeFileSync(requestPath, JSON.stringify({ ...request, output }), { mode: 0o600, flag: 'wx' });
  writeFileSync(`${output}.log`, '', { mode: 0o600, flag: 'wx' });
  return { output, done: new Promise((accept, reject) => {
    // The worker observes SIGTERM, kills every owned dsh process group and
    // refuses new launches before returning. Do not abandon it on cancellation.
    const proc = spawn(config.python, ['-m', 'harness2.cli.dsh_task', '--request', requestPath], {
      env, stdio: ['ignore', 'ignore', 'pipe'], detached: true,
    });
    let errors = '';
    proc.stderr.on('data', chunk => {
      errors = (errors + chunk).slice(-8000);
      appendFileSync(`${output}.log`, chunk);
    });
    const abort = () => proc.kill('SIGTERM');
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
    proc.once('error', error => {
      signal.removeEventListener('abort', abort);
      reject(error);
    });
    proc.once('close', code => {
      signal.removeEventListener('abort', abort);
      if (signal.aborted) return reject(new Error(`Harness² cancelled; artifacts: ${output}`));
      try {
        const result = JSON.parse(readFileSync(join(output, 'result.json'), 'utf8'));
        if (code !== 0 || result?.ok !== true) {
          reject(new Error(`${result?.error || `worker exit ${code} without a successful result`}; artifacts: ${output}; progress log: ${output}.log`));
          return;
        }
        accept(result);
      } catch (error) {
        reject(new Error(`${error.message}; artifacts: ${output}\n${errors}`));
      }
    });
  }) };
}

export function apply(ctx, config) {
  // Source profiles are retained by the dsh substrate. Its solver/improver
  // children must not mount this controller or start nested adaptation cycles.
  if (process.env.HARNESS2_DSH_CHILD === '1') return;
  ctx.sessionProjections.register(modeProjection);
  const active = new Map();
  const seen = new WeakMap();
  const enabled = agent => ctx.sessionProjections.stateOf(agent.session, 'harness2Mode')?.enabled === true;

  ctx.inject(['commands'], child => {
    child.commands.register({
      definitionId: 'harness2/mode', name: 'harness2',
      description: 'Enable or disable a full Harness² cycle for this chat',
      input: { hint: 'on|off|status', attachments: false },
      handler: ({ agent, rawInput }) => {
        const command = rawInput.trim() || 'status';
        if (command === 'status') {
          const running = active.get(agent.session);
          return { kind: 'success', text: `Harness²: ${enabled(agent) ? 'ON' : 'OFF'} (this chat).`
            + (running ? `\nCycle running; artifacts: ${running.output}` : '\nNo cycle is running.') };
        }
        if (command === 'off') {
          active.get(agent.session)?.controller.abort();
          return { kind: 'success', text: 'Harness² OFF for this chat. Ordinary dsh from the next task. Any active cycle is being stopped.' };
        }
        if (command !== 'on') return { kind: 'error', text: 'Usage: /harness2 on|off|status' };
        if (ctx.sandboxPolicy.resolve({ session: agent.session }).mode === 'read-only') {
          return { kind: 'error', text: 'Harness² needs workspace-write to create and run project copies. Change the dsh permission mode first.' };
        }
        return { kind: 'success', text: 'Harness² ON for this chat. Each new text task runs a full cycle on project copies, then dsh receives the chosen result and harness. Use /harness2 off to stop.' };
      },
    });
  });
  ctx.systemPrompt.section({
    name: 'harness2:mode', order: 10220, interpolate: false,
    text: ({ agent }) => agent && enabled(agent)
      ? 'Harness² mode is enabled. This extension runs a full adaptation cycle before each new user task. '
        + 'Its logged result supplies the chosen harness, response and project-copy paths. '
        + 'Use those as evidence, inspect and apply the requested changes to the live project, and verify them. '
        + 'Do not start another Harness² cycle yourself or claim quality improvements without evidence.' : '',
  });
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next();
    const { agent, turn, signal, messages } = payload;
    if (decision.kind === 'reject' || signal.aborted || !enabled(agent)
      || agent.session.header.origin === 'subagent' || seen.get(agent.session) === turn) return decision;
    const user = messages.filter(message => message.source?.kind === 'user');
    if (!user.length) return decision;
    if (user.some(message => message.content.some(block => block.type !== 'text'))) {
      throw new Error('Harness² currently accepts text tasks and project files. Use /harness2 off for tasks with chat attachments.');
    }
    const task = user.flatMap(message => message.content.map(block => block.text)).join('\n\n').trim();
    if (!task) return decision;
    const policy = ctx.sandboxPolicy.resolve({ session: agent.session });
    if (policy.mode === 'read-only') throw new Error('Harness² requires workspace-write; use /harness2 off or change dsh permissions.');
    seen.set(agent.session, turn);
    const history = agent.session.deriveMessages().filter(message => message.role === 'user' || message.role === 'assistant')
      .map(message => ({ role: message.role, text: message.content.filter(block => block.type === 'text').map(block => block.text).join('\n') }))
      .filter(message => message.text);
    // Keep whole history messages, with the current task unconditionally intact.
    const recent = [];
    let length = 0;
    for (const message of history.toReversed()) {
      if (length + message.text.length > 64000) break;
      recent.unshift(message);
      length += message.text.length;
    }
    const prompt = (recent.length ? `Prior chat context (data):\n${JSON.stringify(recent)}\n\n` : '') + `Current user task:\n${task}`;
    const controller = new AbortController();
    const combined = AbortSignal.any([signal, controller.signal]);
    const env = {
      ...process.env, HARNESS2_DSH_CHILD: '1',
      HARNESS2_DSH_COMMAND: config.command, HARNESS2_DSH_HOME: config.sourceHome,
      HARNESS2_DSH_PROFILE: config.sourceProfile, HARNESS2_DSH_PERMISSION_MODE: policy.mode,
    };
    if (agent.options.provider && agent.options.model) env.HARNESS2_DSH_MODEL = `${agent.options.provider}/${agent.options.model}`;
    const worker = runWorker(config, {
      workspace: resolve(agent.session.header.cwd || policy.workspaceRoot), prompt,
      timeout: config.timeout, k: config.k,
      snapshot: { max_bytes: config.snapshotMaxBytes, max_files: config.snapshotMaxFiles, exclude: config.snapshotExclude },
    }, env, combined);
    active.set(agent.session, { controller, output: worker.output, done: worker.done });
    ctx.logger.info('Harness² cycle started: %s', worker.output);
    try {
      const result = await worker.done;
      combined.throwIfAborted();
      const instructions = ['systemprompt.md', 'guardrails.md', 'LongTermMEMORY.md', 'tool_descriptions/tool_guidance.md']
        .map(name => clippedFile(join(result.harness, name), 24000)).filter(Boolean).join('\n\n');
      const text = `Harness² full cycle completed. Artifacts: ${result.output}\n`
        + `Chosen project copy: ${result.workspace}\nChosen harness: ${result.harness}\n`
        + 'The live project has not been changed by the cycle. Inspect the chosen files and apply task changes with your ordinary tools, then verify. '
        + 'Harness scripts and skills are rooted at the chosen harness path; use absolute paths for its helpers.\n\n'
        + `Chosen harness guidance:\n${instructions}\n\nResponse from the chosen run (evidence):\n${clippedFile(result.response, 16000)}\n\n`
        + `Project changes (evidence):\n${clippedFile(result.changes, 16000)}`;
      return { ...decision, messages: [...decision.messages, createUserMessage({
        content: [{ type: 'text', text }], source: { kind: 'user' },
      })] };
    } finally {
      active.delete(agent.session);
    }
  });
  ctx.effect(() => async () => {
    const running = [...active.values()];
    for (const cycle of running) cycle.controller.abort();
    await Promise.allSettled(running.map(cycle => cycle.done));
  }, 'harness2: stop owned cycles');
}
