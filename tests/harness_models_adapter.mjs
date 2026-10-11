// Run in the built Harness image with an isolated endpoint manifest.
import assert from 'node:assert/strict';
import http from 'node:http';
import { apply } from '/app/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js';
let body;
const catalog = id => ({ dotagents: { protocol: 'dotagents-node', schema_version: 1 }, data: [
  { id, dotagents: { name: 'Advertised ' + id, service: 'Chat', installed: true, context_window: 262144 } },
  { id: 'image', dotagents: { name: 'Image', service: 'Image', installed: true } },
] });
body = catalog('first');
const server = http.createServer((req, res) => {
  res.writeHead(body ? 200 : 503, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body ?? {}));
}).listen(8888, '127.0.0.1');
const saved = { 'dotagents-original-route': { api: 'openai-completions', baseURL: 'http://127.0.0.1:8888/v1',
  models: [{ id: 'obsolete', name: 'Old entry', contextWindow: 131072, maxTokens: 1024, input: ['text'] }] } };
let adapter, routes = [], wake;
const disposers = [];
const update = next => { routes = next; wake?.(); };
const context = { fiber: { entry: { options: { id: 'llm-pi-ai' } } }, inject() {}, get() {}, logger: console,
  on(event, callback) { if (event === 'dispose') disposers.push(callback); },
  llm: { registerModelDiscovery() {}, registerConfigurableProviders() { return { replace() {} }; },
    registerAdapter(next, value) { adapter = value; update(next); return { replace: update }; } } };
async function until(predicate) {
  if (predicate()) return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Catalog update timed out')), 10000);
    wake = () => { if (predicate()) { clearTimeout(timeout); wake = undefined; resolve(); } };
  });
}
try {
  apply(context, { providers: { get: () => saved } });
  assert.deepEqual(routes, []);
  await until(() => routes.length === 1);
  assert.deepEqual(routes, ['dotagents-original-route']);
  assert.deepEqual((await adapter.listModels(routes[0])).map(m => m.id), ['first']);
  assert.equal((await adapter.resolveModel(routes[0], 'first')).context.contextWindow, 262144);
  body = undefined;
  await until(() => routes.length === 0);
  await assert.rejects(adapter.resolveModel('dotagents-original-route', 'first'));
  body = catalog('replacement');
  await until(() => routes.length === 1);
  assert.deepEqual((await adapter.listModels(routes[0])).map(m => m.id), ['replacement']);
  assert.equal(saved['dotagents-original-route'].models[0].id, 'obsolete');
  console.log('PASS: pinned Harness adapter discovers, removes and restores live models without rewriting saved routes.');
} finally {
  for (const dispose of disposers) dispose();
  server.closeAllConnections(); server.close();
}
