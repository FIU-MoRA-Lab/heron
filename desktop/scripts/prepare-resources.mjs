import { cp, mkdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktop = dirname(dirname(fileURLToPath(import.meta.url)));
const staging = join(desktop, '.bundle-resources');
// This directory contains only generated copies. Never package interpreter
// bytecode or local cache files from the developer's source tree.
await rm(staging, { recursive: true, force: true });
await mkdir(staging, { recursive: true });
await cp(join(desktop, '../backend/heron'), join(staging, 'heron'), {
  recursive: true,
  filter: source => !source.split(/[\\/]/).includes('__pycache__') && !source.endsWith('.pyc') && !source.endsWith('.DS_Store'),
});
