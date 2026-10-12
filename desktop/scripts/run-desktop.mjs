import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktop = dirname(dirname(fileURLToPath(import.meta.url)));
const ownDirectory = process.env.HERON_QUIT_REQUEST ? null : mkdtempSync(join(tmpdir(), 'heron-desktop-'));
const requestPath = process.env.HERON_QUIT_REQUEST ?? join(ownDirectory, 'quit.request');
const requestQuit = () => writeFileSync(requestPath, randomUUID(), 'utf8');

// Isolate the Tauri CLI/Vite/cargo process tree from the terminal process
// group. Ctrl+C asks the native app to quit and waits for its decision.
process.on('SIGINT', requestQuit);
process.on('SIGTERM', requestQuit);
const runner = spawn(process.execPath, [
  join(desktop, 'node_modules/@tauri-apps/cli/tauri.js'), 'dev', ...process.argv.slice(2),
], {
  cwd: desktop,
  env: { ...process.env, HERON_QUIT_REQUEST: requestPath },
  detached: process.platform !== 'win32',
  stdio: 'inherit',
});
function cleanup() {
  process.removeListener('SIGINT', requestQuit);
  process.removeListener('SIGTERM', requestQuit);
  if (ownDirectory) rmSync(ownDirectory, { recursive: true, force: true });
}
runner.on('error', error => { cleanup(); console.error(error.message); process.exitCode = 1; });
runner.on('exit', code => { cleanup(); process.exitCode = code ?? 1; });
