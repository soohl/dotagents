import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { once } from 'node:events';
import { createBridge } from '../src/harness_runtime/bridge.mjs';

test('browser bootstrap and local management have separate admission rules', async () => {
  const calls = [];
  const events = {};
  const upstream = http.createServer((req, res) => {
    if (req.url === '/?token=test-only') {
      res.writeHead(303, { Location: './', 'Set-Cookie': 'dsh-auth-test=fixture; HttpOnly' });
    } else res.writeHead(401);
    res.end();
  }).listen(0, '127.0.0.1');
  await once(upstream, 'listening');
  const ctx = {
    on: (name, fn) => { events[name] = fn; },
    connection: { authenticatedUrl: base => base + '?token=test-only' },
    workspaceController: { create: async () => ({ workspace: { workspaceId: 'workspace-1' } }) },
    sessionController: {
      create: async input => { calls.push(input); return { sessionId: 'dashboard-only' }; },
      rename: async () => {}, prompt: async input => calls.push(input),
      cancel: input => calls.push(input),
    },
  };
  const previous = process.env.HARNESS_PUBLIC_HOST;
  process.env.HARNESS_PUBLIC_HOST = 'remote.example';
  const server = createBridge(ctx, { port: 0, upstreamPort: upstream.address().port });
  await once(server, 'listening');
  const request = (path, headers = {}, data) => new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port: server.address().port, path,
      method: data === undefined ? 'GET' : 'POST',
      headers: { Host: '127.0.0.1:3000', ...headers } }, res => {
      let body = ''; res.on('data', c => body += c);
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject); req.end(data === undefined ? undefined : JSON.stringify(data));
  });
  try {
    const root = await request('/');
    assert.equal(root.status, 303);
    assert.equal(root.headers.location, './');
    assert.ok(!JSON.stringify(root).includes('test-only'));
    const direct = await request('/', { Host: 'localhost:3003' });
    assert.equal(direct.status, 308);
    assert.equal(direct.headers.location, '/agent/');
    assert.equal((await request('/agent/', { Host: 'localhost:3003' })).status, 303);
    assert.equal((await request('/agent/_dotagents/status', { Host: 'localhost:3003' })).status, 200);
    assert.equal((await request('/', { Host: 'localhost:3002' })).status, 403);
    assert.equal((await request('/agent/_dotagents/status', {
      Host: 'localhost:3003', Origin: 'http://localhost:3000' })).status, 403);
    assert.equal((await request('/api/session')).status, 401);
    assert.equal((await request('/_dotagents/status', { Host: 'remote.example' })).status, 403);
    assert.equal((await request('/_dotagents/task', { Origin: 'https://evil.example' }, { task: 'x' })).status, 403);
    assert.equal((await request('/_dotagents/task', { 'Sec-Fetch-Site': 'cross-site' }, { task: 'x' })).status, 403);
    assert.equal((await request('/_dotagents/task', {}, { task: '' })).status, 400);
    assert.equal(calls.length, 0);
    assert.equal((await request('/_dotagents/task', {}, { task: 'fixture' })).status, 200);
    assert.deepEqual(calls[0], { workspaceId: 'workspace-1', agentPreset: 'agent' });
    assert.equal(calls[1].sessionId, 'dashboard-only');
    assert.equal((await request('/_dotagents/stop', {}, {})).status, 200);
    assert.deepEqual(calls.at(-1), { sessionId: 'dashboard-only' });
  } finally {
    events.dispose();
    upstream.closeAllConnections(); upstream.close();
    if (previous === undefined) delete process.env.HARNESS_PUBLIC_HOST;
    else process.env.HARNESS_PUBLIC_HOST = previous;
  }
});

test('Chat rejects dashboard execution while reporting its own status', async () => {
  let dispose;
  const ctx = { on(name, fn) { if (name === 'dispose') dispose = fn; } };
  const server = createBridge(ctx, { port: 0, role: 'chat' });
  await once(server, 'listening');
  const request = (path, method) => new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port: server.address().port, path, method,
      headers: { Host: 'localhost:3000' } }, res => {
      let body = ''; res.on('data', chunk => body += chunk);
      res.on('end', () => resolve({ status: res.statusCode, body: JSON.parse(body) }));
    });
    req.on('error', reject); req.end();
  });
  try {
    assert.equal((await request('/_dotagents/status', 'GET')).body.role, 'chat');
    assert.equal((await request('/_dotagents/task', 'POST')).status, 404);
    assert.equal((await request('/_dotagents/stop', 'POST')).status, 404);
  } finally { dispose(); }
});
