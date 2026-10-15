// Add a read-only mode to the pinned upstream file tools; Agent keeps the defaults.
import { readFile, writeFile } from 'node:fs/promises';

const path = '/app/node_modules/@deepseek-ai/dsh-tool-fs/lib/index.js';
let source = await readFile(path, 'utf8');
for (const [before, after] of [
  ['const Config = z.object({\n', 'const Config = z.object({\n\treadOnly: z.boolean().default(false),\n'],
  ['\tconst sandbox = new FsSandboxController(ctx);\n', '\tif (resolved.readOnly) return;\n\tconst sandbox = new FsSandboxController(ctx);\n'],
]) {
  if (source.split(before).length !== 2) throw new Error('Pinned Harness file tools changed; review read-only mode.');
  source = source.replace(before, after);
}
await writeFile(path, source);
