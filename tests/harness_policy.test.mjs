import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { applyPolicy } from '../src/harness_runtime/apply-policy.mjs';
import { serviceProfile } from '../src/harness_runtime/service-profile.mjs';
import { pageResult } from '../src/harness_runtime/browser-output.mjs';
import { createFilter } from '../src/harness_runtime/agent-browser.mjs';
import { filterResult, validateCall } from '../src/harness_runtime/chat-browser.mjs';

test('calendar connection is opt-in, idempotent, and restricted to Chat', () => {
  for (const role of ['chat', 'agent']) {
    const rows = [{id: 'preset-' + role, config: {plugins: []}}];
    const configured = serviceProfile(structuredClone(rows), role, false, true);
    assert.equal(configured[0].config.plugins.some(p => p.id === 'mcp-calendar'), role === 'chat');
    assert.deepEqual(serviceProfile(structuredClone(configured), role, false, true), configured);
    assert.deepEqual(serviceProfile(structuredClone(configured), role, false, false),
      serviceProfile(structuredClone(rows), role));
  }
});
test('Chat gets a fresh browser-local clock at every model step', () => {
  const rows = serviceProfile([], 'chat');
  assert.equal(rows[0].insert[0].name, '@deepseek-ai/dsh-time-context');
  assert.equal(rows[0].insert[0].config.refreshIntervalMs, 0);
  assert.deepEqual(serviceProfile(rows, 'chat'), rows);
  assert.deepEqual(serviceProfile(rows, 'agent'), []);
});
const policy = JSON.parse(await readFile(new URL('../config/harness-policy.json', import.meta.url)));
test('the project rename updates integrations without changing saved model selections', () => {
  const rows = [
    { id: 'llm-pi-ai', config: { providers: { 'sealedllm-qwen': { apiKeyEnv: 'SEALEDLLM_LOCAL_PLACEHOLDER' } } } },
    { id: 'agent-default-model', config: { provider: 'sealedllm-qwen', model: 'saved-model' } },
    { id: 'web', config: { searchProvider: 'sealedllm-brave' } },
    { insert: [{ id: 'sealedllm-bridge' }, { id: 'sealedllm-web-search' }] },
  ];
  applyPolicy(rows, policy);
  assert.equal(rows[0].config.providers['sealedllm-qwen'].apiKeyEnv, 'DOTAGENTS_LOCAL_PLACEHOLDER');
  assert.deepEqual(rows[1].config, { provider: 'sealedllm-qwen', model: 'saved-model' });
  assert.equal(rows[2].config.searchProvider, 'dotagents-brave');
  assert.deepEqual(rows[3].insert.map(row => row.id), ['dotagents-bridge', 'dotagents-web-search']);
  assert.deepEqual(applyPolicy(structuredClone(rows), policy), rows);
});

test('independent services retain only their preset and keep saved model choices', () => {
  const saved = [{ id: 'agent-default-model', config: { provider: 'saved', model: 'saved' } },
    { id: 'agent-preset-registry', config: { default: 'chat' } },
    { insert: [{ id: 'preset-chat' }, { id: 'preset-agent' }, { id: 'dotagents-bridge' }] }];
  for (const role of ['chat', 'agent']) {
    const rows = serviceProfile(structuredClone(saved), role);
    assert.deepEqual(rows[0], saved[0]);
    assert.equal(rows[1].config.default, role);
    assert.deepEqual(rows[2].insert.map(row => row.id), ['preset-' + role, 'dotagents-bridge']);
    assert.deepEqual(serviceProfile(structuredClone(rows), role), rows);
  }
  assert.throws(() => serviceProfile([], 'other'));
});

test('Things MCP is registered only in Chat when its private connection is configured', () => {
  const rows = [{insert:['chat','agent'].map(role => ({id:'preset-'+role,config:{plugins:[]}}))}];
  for (const role of ['chat','agent']) {
    const configured = serviceProfile(structuredClone(rows),role,true);
    assert.equal(configured[0].insert[0].config.plugins.some(p=>p.id==='mcp-things'),role==='chat');
    assert.deepEqual(serviceProfile(structuredClone(configured),role,true),configured);
    assert.equal(serviceProfile(structuredClone(configured),role,false)[0].insert[0].config.plugins.length,0);
  }
});

test('saved policy updates are idempotent and preserve identity, model routes and unrelated presets', () => {
  const rows = [{ id: 'llm-pi-ai', config: { providers: { saved: {} } } }, { id: 'custom', config: { keep: true } },
    { insert: ['chat', 'agent'].map(name => ({ id: 'preset-' + name, config: { plugins: [
      { id: 'compaction', config: [{ id: 'compaction-basic' }, { id: 'command-compact' }] },
      { id: 'tool-web', config: { searchMaxResults: 5 } },
      { id: 'mcp-client', config: { serverName: name === 'agent' ? 'playwright' : 'web', args: ['existing'] } },
    ] } })) }];
  const untouched = structuredClone(rows.slice(0, 2));
  applyPolicy(rows, policy);
  const first = structuredClone(rows);
  applyPolicy(rows, policy);
  assert.deepEqual(rows, first);
  assert.deepEqual(rows.slice(0, 2), untouched);
  assert.deepEqual(rows[2].insert[0].config.plugins[2].config.args, ['existing']);
  assert.deepEqual(rows[2].insert[1].config.plugins[2].config.args, ['/app/runtime/agent-browser.mjs']);
  for (const preset of rows[2].insert) {
    assert.equal(preset.config.plugins[0].isolate.toolResultPruner, true);
    const children = preset.config.plugins[0].config;
    assert.equal(children.filter(p => p.id === 'compaction-tool-result-pruner').length, 1);
    const c = children.find(p => p.id === 'compaction-basic').config;
    assert.equal(Math.floor(Math.min(policy.hostContextWindow * c.thresholdRatio,
      policy.hostContextWindow - policy.hostMaxTokens - c.headroomTokens)), 104857);
    assert.equal(c.maxTokens, 4096);
    assert.equal(preset.config.plugins[1].config.fetchMaxOutputChars, 16000);
  }
});
test('Chat pages Unicode snapshots without losing text or exposing filesystem parameters', () => {
  const text = '😀abcdef'.repeat(4000);
  let start = 0, reconstructed = '';
  while (start < Array.from(text).length) {
    const result = filterResult('tools/call', { content: [{ type: 'text', text }] },
      { name: 'browser_snapshot', arguments: { start } });
    const page = result.content[0].text;
    assert.ok(Array.from(page).length <= policy.browserMaxOutputChars);
    const chunk = page.split('\n[Showing characters ')[0];
    reconstructed += chunk;
    start += Array.from(chunk).length;
  }
  assert.equal(reconstructed, text);
  assert.deepEqual(validateCall('browser_snapshot', { start: 12000 }), {});
  assert.throws(() => validateCall('browser_snapshot', { start: -1 }));
  assert.throws(() => validateCall('browser_snapshot', { start: 0, filename: '/data/file' }));
});
test('Agent browser retains full large output in a spill and keeps advanced actions', async () => {
  const text = 'x'.repeat(50000);
  let full;
  const adapter = createFilter(policy, async value => { full = value; return '/workspace/.browser-artifacts/result.txt'; });
  const tools = [...policy.agentBrowserTools, 'browser_pdf_save'].map(name => ({ name }));
  assert.equal((await adapter.filter('tools/list', { tools })).tools.length, 12);
  assert.deepEqual(adapter.validate('browser_run_code_unsafe', { code: 'example' }), { code: 'example' });
  assert.throws(() => adapter.validate('browser_pdf_save'));
  const result = await adapter.filter('tools/call', { content: [{ type: 'text', text }], structuredContent: { text } });
  assert.equal(full, text);
  assert.ok(result.content[0].text.length <= policy.browserMaxOutputChars);
  assert.match(result.content[0].text, /Full output:/);
  assert.equal(result.structuredContent, undefined);
});
