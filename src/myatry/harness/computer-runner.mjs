/** A small Harness composition: one host-authorized action, no secondary LLM. */
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { join, resolve } from 'node:path';
import { lstat, mkdir, open, realpath } from 'node:fs/promises';
import { spawn } from 'node:child_process';

const LIMIT = 16000;
let input = '';
try {
  for await (const chunk of process.stdin) {
    input += chunk.toString('utf8');
    if (Buffer.byteLength(input) > LIMIT) throw new Error('input_limit');
  }
  const request = JSON.parse(input);
  const keys = ['action', 'text', 'notes_root', 'task_id'];
  if (Object.keys(request).some(key => !keys.includes(key)) ||
      !['probe', 'open_notepad', 'write_note', 'read_note'].includes(request.action) ||
      typeof request.notes_root !== 'string' || !/^[a-f0-9]{64}$/.test(request.task_id) ||
      typeof request.text !== 'string' || [...request.text].length > 1000) throw new Error('invalid_request');
  // The parent chooses the installation, not tool/model arguments.
  const anchor = resolve(process.argv[2], 'package.json');
  const require = createRequire(anchor);
  const load = name => import(pathToFileURL(require.resolve(name)).href);
  const [{ Context }, { default: SystemPrompt }, { default: ToolRuntime }] = await Promise.all([
    load('@deepseek-ai/cordis'), load('@deepseek-ai/dsh-system-prompt'), load('@deepseek-ai/dsh-tools'),
  ]);
  const version = require('@deepseek-ai/dsh/package.json').version;
  if (version !== '0.2.1-rc.2') throw new Error('runtime_version_mismatch');
  const ctx = new Context();
  await ctx.plugin(SystemPrompt, { includeRuntimeContext: false });
  await ctx.plugin(ToolRuntime);
  try {
    const notes = resolve(request.notes_root);
    const result = async () => {
      if (request.action === 'probe') return { status: 'ready', harness_version: version };
      if (request.action === 'open_notepad') {
        if (process.platform !== 'win32') throw new Error('windows_required');
        const child = spawn(join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'notepad.exe'), [], {
          shell: false, detached: true, windowsHide: false, stdio: 'ignore',
        });
        await new Promise((ok, fail) => { child.once('spawn', ok); child.once('error', fail); });
        const pid = child.pid;
        child.unref();
        return { status: 'launch_submitted', application: 'notepad', pid };
      }
      // Reject symbolic links/junctions in every existing parent; no model paths.
      let current = notes;
      while (true) {
        try { if ((await lstat(current)).isSymbolicLink()) throw new Error('linked_path'); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
        const parent = resolve(current, '..');
        if (parent === current) break;
        current = parent;
      }
      await mkdir(notes, { recursive: true });
      if ((await realpath(notes)).toLowerCase() !== notes.toLowerCase()) throw new Error('linked_path');
      const file = join(notes, '便签.txt');
      try {
        const stat = await lstat(file);
        if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink > 1 || stat.size > 1000000) throw new Error('note_unavailable');
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
      if (request.action === 'write_note') {
        if (!request.text.trim()) throw new Error('empty_note');
        const handle = await open(file, 'a');
        try {
          await handle.writeFile(`${request.text}\n`, 'utf8');
          await handle.sync();
        } finally { await handle.close(); }
        return { status: 'written', file: '便签.txt', characters: [...request.text].length };
      }
      try {
        const handle = await open(file, 'r');
        try {
          const text = await handle.readFile('utf8');
          const chars = [...text];
          return { status: 'read', file: '便签.txt', text: chars.slice(-1000).join(''), truncated: chars.length > 1000 };
        } finally { await handle.close(); }
      } catch (error) {
        if (error.code === 'ENOENT') return { status: 'empty', file: '便签.txt', text: '', truncated: false };
        throw error;
      }
    };
    ctx.tools.register({
      name: 'myatri_computer_action', description: 'Execute the single action bound by the trusted host.',
      parameters: { type: 'object', properties: {}, additionalProperties: false },
      output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      execute: result,
    });
    const outcome = await ctx.tools.execute({ callId: request.task_id, name: 'myatri_computer_action',
      arguments: {}, signal: AbortSignal.timeout(10000) });
    if (outcome.isError) throw new Error('tool_failed');
    const value = JSON.parse(outcome.content.find(block => block.type === 'text').text);
    process.stdout.write(JSON.stringify({ ok: true, harness_version: version, result: value }));
  } finally { await ctx.fiber.dispose(); }
} catch {
  // No raw request, filesystem names or exception traces cross the IPC boundary.
  process.stdout.write(JSON.stringify({ ok: false, error: 'computer_execution_failed' }));
  process.exitCode = 1;
}
