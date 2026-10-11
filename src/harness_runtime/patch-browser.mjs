// Adapt the pinned Harness client without changing its stored presets or sessions.
import { readFile, writeFile } from 'node:fs/promises';

const path = '/app/node_modules/@deepseek-ai/dsh-client-ui-agent-preset/lib/client.js';
const source = await readFile(path, 'utf8');
const original = '\t\t\tconst staged = {\n\t\t\t\tid: void 0,\n\t\t\t\tintroduce: false\n\t\t\t};';
const replacement = `\t\t\t// The gateway entry selects the next new session only. Existing sessions keep their preset.
\t\t\tconst entryPreset = window.location.pathname.split('/')[1];
\t\t\tconst staged = {
\t\t\t\tid: ['chat', 'agent'].includes(entryPreset) ? entryPreset : void 0,
\t\t\t\tintroduce: false
\t\t\t};`;
if (source.split(original).length !== 2) throw new Error('Pinned Harness preset entry changed; review the browser adapter.');
await writeFile(path, source.replace(original, replacement));

// Same-origin applications must not restore each other's selected session.
const workspacePath = '/app/node_modules/@deepseek-ai/dsh-client-ui-workspace/lib/client.js';
let workspace = await readFile(workspacePath, 'utf8');
for (const key of ['dsh.sessions.current', 'dsh.workspace.view.v5']) {
  const match = JSON.stringify(key);
  if (workspace.split(match).length !== 2) throw new Error('Pinned Harness workspace persistence changed.');
  workspace = workspace.replace(match, `(${match} + ':' + window.location.pathname.split('/')[1])`);
}
await writeFile(workspacePath, workspace);

// Each Harness process signs its own cookie. Give the cookies distinct names
// and paths so visiting one application cannot replace the other's cookie.
const authPath = '/app/node_modules/@deepseek-ai/dsh-client-connection/lib/index.js';
let auth = await readFile(authPath, 'utf8');
const nameMatch = 'function cookieName(authority) {\n';
const pathMatch = '; Path=/; Expires=';
if (auth.split(nameMatch).length !== 2 || auth.split(pathMatch).length !== 2) {
  throw new Error('Pinned Harness browser authentication changed.');
}
auth = auth.replace(nameMatch, nameMatch + '\tauthority += ":" + process.env.HARNESS_ROLE;\n');
auth = auth.replace(pathMatch, '; Path=/${process.env.HARNESS_ROLE}/; Expires=');
await writeFile(authPath, auth);
