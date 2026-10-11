import { readFile, writeFile, copyFile, rename, constants } from 'node:fs/promises';
import { serviceProfile } from './service-profile.mjs';

export function applyPolicy(rows, policy) {
  // Rename managed integration settings. Provider IDs belong to saved sessions.
  const renamed = new Map([
    ['sealedllm-bridge', 'dotagents-bridge'],
    ['sealedllm-web-search', 'dotagents-web-search'],
    ['sealedllm-brave', 'dotagents-brave'],
    ['SEALEDLLM_LOCAL_PLACEHOLDER', 'DOTAGENTS_LOCAL_PLACEHOLDER'],
  ]);
  function renameSettings(value) {
    for (const [key, child] of Object.entries(value)) {
      if (typeof child === 'string' && renamed.has(child)) value[key] = renamed.get(child);
      else if (child && typeof child === 'object') renameSettings(child);
    }
  }
  renameSettings(rows);
  const plugin = (id, config) => ({ id, name: '@deepseek-ai/dsh-' + id, config });
  function visit(items) {
    for (const row of items) {
      if (row.insert) visit(row.insert);
      if (!['preset-chat', 'preset-agent'].includes(row.id)) continue;
      const plugins = row.config?.plugins;
      if (!Array.isArray(plugins)) throw new Error('Managed preset has no plugin list.');
      for (const entry of plugins) {
        if (entry.id === 'compaction') {
          entry.isolate = { ...entry.isolate, toolResultPruner: true };
          const children = entry.config;
          if (!Array.isArray(children)) throw new Error('Managed compaction group changed.');
          const basic = children.find(p => p.id === 'compaction-basic');
          if (!basic) throw new Error('Managed compaction backend is missing.');
          basic.config = { ...basic.config, ...policy.compaction };
          let pruner = children.find(p => p.id === 'compaction-tool-result-pruner');
          if (!pruner) { pruner = plugin('compaction-tool-result-pruner', {}); children.unshift(pruner); }
          pruner.config = { ...pruner.config, ...policy.toolResultPruner };
        }
        if (entry.id === 'tool-web') entry.config = { ...entry.config, fetchMaxOutputChars: policy.fetchMaxOutputChars };
        if (row.id === 'preset-agent' && entry.id === 'mcp-client' && entry.config?.serverName === 'playwright') {
          entry.config.args = ['/app/runtime/agent-browser.mjs'];
        }
      }
    }
  }
  visit(rows);
  return rows;
}

export async function updateProfile(path, policyPath = '/app/policy.json', role) {
  const { parse, stringify } = await import('yaml');
  const original = await readFile(path, 'utf8');
  let rows = parse(original);
  const before = JSON.stringify(rows);
  const policy = JSON.parse(await readFile(policyPath, 'utf8'));
  applyPolicy(rows, policy);
  if (role !== undefined) rows = serviceProfile(rows, role, Boolean(process.env.DOTAGENTS_THINGS_TOKEN),
    process.env.DOTAGENTS_CALENDAR_ENABLED === 'true' && Boolean(process.env.DOTAGENTS_CALENDAR_TOKEN));
  if (JSON.stringify(rows) === before) return;
  await copyFile(path, path + '.before-context-policy', constants.COPYFILE_EXCL)
    .catch(error => { if (error.code !== 'EEXIST') throw error; });
  const temporary = path + '.policy.tmp';
  await writeFile(temporary, stringify(rows), { mode: 0o600 });
  await rename(temporary, path);
}
