import { readFile, mkdir, writeFile, lstat } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { runBrowser } from './browser-transport.mjs';
import { pageResult } from './browser-output.mjs';

export function createFilter(policy, spill) {
  const allowed = new Set(policy.agentBrowserTools);
  return {
    validate(name, args = {}) {
      if (!allowed.has(name)) throw new Error('Use the available browser tools; browser_run_code_unsafe supports advanced interactions.');
      return args;
    },
    async filter(method, result) {
      if (method === 'tools/list') {
        const tools = result.tools.filter(t => allowed.has(t.name));
        if (tools.length !== allowed.size) throw new Error('Pinned Agent browser tools changed.');
        return { tools };
      }
      if (method === 'tools/call') {
        const text = (result.content ?? []).filter(p => p.type === 'text').map(p => p.text).join('\n');
        if (Array.from(text).length > policy.browserMaxOutputChars) {
          return pageResult(result, { limit: policy.browserMaxOutputChars, file: await spill(text) });
        }
      }
      return result;
    },
  };
}

async function main() {
  const policy = JSON.parse(await readFile('/app/policy.json', 'utf8'));
  const adapter = createFilter(policy, async text => {
    const directory = '/workspace/.browser-artifacts';
    await mkdir(directory, { recursive: true });
    if ((await lstat(directory)).isSymbolicLink()) throw new Error('Browser output directory must not be a symlink.');
    const path = `${directory}/result-${randomUUID()}.txt`;
    await writeFile(path, text, { flag: 'wx', mode: 0o600 });
    return path;
  });
  runBrowser({ config: '/app/browser.json', ...adapter });
}
if (process.argv[1] && import.meta.url === new URL(process.argv[1], 'file:').href) main();
