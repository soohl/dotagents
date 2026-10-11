import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LiveModels, readCatalog } from '../src/harness_runtime/live-models.mjs';
const endpoint = { provider: 'dotagents-node', baseURL: 'http://node:8001/v1', protocol: 'dotagents-node' };
test('shared local API moves existing routes without changing saved models or duplicating choices', async () => {
  const endpoints = ['qwen', 'deepseek'].map(model => ({ provider: 'dotagents-' + model,
    baseURL: 'http://host:9876/v1', model }));
  let available = ['qwen', 'deepseek'];
  const live = new LiveModels({ readEndpoints: async () => endpoints,
    fetcher: async () => new Response(JSON.stringify({ data: available.map(id => ({ id })) })) });
  const saved = { 'sealedllm-qwen': { baseURL: 'http://host:8002/v1', models: [{ id: 'qwen' }] },
    'dotagents-deepseek': { baseURL: 'http://host:8001/v1', models: [{ id: 'deepseek' }] } };
  await live.refresh();
  const providers = live.providers(saved);
  assert.deepEqual(Object.keys(providers), Object.keys(saved));
  assert.deepEqual(providers['sealedllm-qwen'].models.map(m => m.id), ['qwen']);
  assert.deepEqual(providers['dotagents-deepseek'].models.map(m => m.id), ['deepseek']);
  assert.ok(Object.values(providers).every(p => p.baseURL === 'http://host:9876/v1'));
  assert.equal(saved['sealedllm-qwen'].baseURL, 'http://host:8002/v1');
  available = ['deepseek'];
  await live.refresh();
  assert.deepEqual(Object.keys(live.providers(saved)), ['dotagents-deepseek']);
});
test('existing servers and saved session provider IDs survive the rename', async () => {
  const legacy = { ...endpoint, protocol: 'sealedllm-node' };
  const body = { sealedllm: { protocol: legacy.protocol, schema_version: 1 }, data: [
    { id: 'saved-model', sealedllm: { service: 'Chat', name: 'Saved model', installed: true } },
  ] };
  const live = new LiveModels({ readEndpoints: async () => [legacy],
    fetcher: async () => new Response(JSON.stringify(body)) });
  const saved = { 'sealedllm-original': { baseURL: endpoint.baseURL, models: [{ id: 'stale' }] } };
  await live.refresh();
  const providers = live.providers(saved);
  assert.deepEqual(Object.keys(providers), ['sealedllm-original']);
  assert.deepEqual(providers['sealedllm-original'].models.map(m => m.id), ['saved-model']);
  assert.equal(providers['sealedllm-original'].apiKeyEnv, 'DOTAGENTS_LOCAL_PLACEHOLDER');
  assert.equal(saved['sealedllm-original'].models[0].id, 'stale');
});
function catalog(models) {
  return { dotagents: { protocol: 'dotagents-node', schema_version: 1 }, data: models.map(([id, service = 'Chat']) => ({
    id, dotagents: { service, name: 'Advertised ' + id, installed: true, context_window: 262144 },
  })) };
}
test('managed selectors discover additions, remove stale models, recover, and retain session routes', async () => {
  let body = catalog([['new'], ['image', 'Image']]);
  const live = new LiveModels({ readEndpoints: async () => [endpoint], fetcher: async () => {
    if (body instanceof Error) throw body;
    return new Response(JSON.stringify(body));
  } });
  const saved = { 'dotagents-old-chat': { baseURL: endpoint.baseURL, models: [{ id: 'obsolete' }] },
    custom: { models: [{ id: 'user-managed' }] } };
  assert.deepEqual(Object.keys(live.providers(saved)), ['custom']);
  assert.equal(await live.refresh(), true);
  const available = live.providers(saved);
  assert.deepEqual(available['dotagents-old-chat'].models.map(m => m.id), ['new']);
  assert.equal(available['dotagents-old-chat'].models[0].name, 'Advertised new');
  assert.equal(available['dotagents-old-chat'].models[0].contextWindow, 262144);
  assert.equal(live.providers(saved), available);
  assert.equal(await live.refresh(), false);
  body = new Error('offline');
  await live.refresh();
  assert.deepEqual(Object.keys(live.providers(saved)), ['custom']);
  body = catalog([['replacement']]);
  await live.refresh();
  assert.deepEqual(live.providers(saved)['dotagents-old-chat'].models.map(m => m.id), ['replacement']);
  assert.equal(saved['dotagents-old-chat'].models[0].id, 'obsolete');
});
test('invalid, oversized, failed and empty catalogs cannot expose configured choices', async () => {
  for (const body of [{}, { data: [{ id: '' }] }, catalog([])]) {
    const live = new LiveModels({ readEndpoints: async () => [endpoint],
      fetcher: async () => new Response(JSON.stringify(body)) });
    await live.refresh();
    assert.deepEqual(live.providers({ 'dotagents-stale': { models: [{ id: 'old' }] } }), {});
  }
  await assert.rejects(readCatalog(endpoint, async () => new Response('x'.repeat(1024 * 1024 + 1))));
  await assert.rejects(readCatalog(endpoint, async () => new Response('{}', { status: 503 })));
});
test('host catalog supplies names and vision without preconfigured model IDs', async () => {
  const models = await readCatalog({ baseURL: 'http://host:8001/v1', contextWindow: 131072, maxTokens: 16384 }, async (url, options) => {
    assert.equal(url, 'http://host:8001/v1/models');
    assert.equal(options.redirect, 'error');
    return new Response(JSON.stringify({ data: [{ id: 'new-host-model', name: 'Host model',
      info: { meta: { capabilities: { vision: true } } } }] }));
  });
  assert.equal(models[0].name, 'Host model');
  assert.equal(models[0].contextWindow, 131072);
  assert.equal(models[0].maxTokens, 16384);
  assert.deepEqual(models[0].input, ['text', 'image']);
});
