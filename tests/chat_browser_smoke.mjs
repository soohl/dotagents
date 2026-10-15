// Run in the built image; exercise actual browser actions against a local fixture.
import assert from 'node:assert/strict';
import http from 'node:http';
import { Client } from '/app/node_modules/@modelcontextprotocol/client/dist/index.mjs';
import { StdioClientTransport } from '/app/node_modules/@modelcontextprotocol/client/dist/stdio.mjs';
import { allowedTools } from '/app/runtime/chat-browser.mjs';

const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end(`<html><title>Research fixture</title><body>
    <label>Query<input aria-label="Query"></label>
    <label>Ranking<select aria-label="Ranking"><option value="revenue">Revenue</option><option value="cap">Market cap</option></select></label>
    <button onclick="document.querySelector('output').textContent='Results: '+document.querySelector('input').value+' '+document.querySelector('select').value">Search</button>
    <output></output><a href="/source" target="_blank">Source</a></body></html>`);
}).listen(8891, '127.0.0.1');
const client = new Client({ name: 'chat-browser-check', version: '1.0.0' });
const call = async (name, args = {}) => {
  const result = await client.callTool({ name, arguments: args });
  assert.notEqual(result.isError, true, JSON.stringify(result));
  const text = result.content.filter(p => p.type === 'text').map(p => p.text).join('\n');
  return ['browser_navigate', 'browser_type', 'browser_select_option', 'browser_click', 'browser_wait_for'].includes(name) ? call('browser_snapshot') : text;
};
const ref = (snapshot, label) => {
  const line = snapshot.split('\n').find(line => line.includes(label) && line.includes('[ref='));
  assert.ok(line, snapshot);
  return line.match(/\[ref=(\w+)\]/)[1];
};
try {
  await client.connect(new StdioClientTransport({ command: 'node', args: ['/app/runtime/chat-browser.mjs'],
    env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: '/opt/playwright' }, stderr: 'pipe' }));
  const { tools } = await client.listTools();
  assert.deepEqual(tools.map(t => t.name).sort(), [...allowedTools].sort());
  for (const [name, args] of [
    ['browser_run_code', { code: 'return process.env' }],
    ['browser_evaluate', { function: '()=>document.cookie' }],
    ['browser_file_upload', { paths: ['/data/file'] }],
    ['browser_snapshot', { filename: '/tmp/disallowed.txt' }],
    ['browser_navigate', { url: 'file:///etc/passwd' }],
    ['browser_tabs', { action: 'new', url: 'javascript:alert(1)' }],
  ]) assert.equal((await client.callTool({ name, arguments: args })).isError, true);
  let snapshot = await call('browser_navigate', { url: 'http://127.0.0.1:8891' });
  snapshot = await call('browser_type', { target: ref(snapshot, 'textbox "Query"'), text: 'company' });
  snapshot = await call('browser_select_option', { target: ref(snapshot, 'combobox "Ranking"'), values: ['cap'] });
  await call('browser_press_key', { key: 'Tab' });
  await call('browser_click', { target: ref(snapshot, 'button "Search"') });
  snapshot = await call('browser_wait_for', { text: 'Results: company cap' });
  assert.match(snapshot, /Results: company cap/);
  await call('browser_click', { target: ref(snapshot, 'link "Source"') });
  const tabs = await call('browser_tabs', { action: 'list' });
  assert.match(tabs, /\/source/);
  await call('browser_tabs', { action: 'select', index: 1 });
  assert.match(await call('browser_snapshot'), /Research fixture/);
  await call('browser_tabs', { action: 'close', index: 1 });
  await call('browser_tabs', { action: 'new', url: 'http://127.0.0.1:8891/new' });
  await call('browser_close');
  console.log('PASS: Chat types, selects, clicks, waits, uses keyboard and tabs; code, uploads and output paths are rejected.');
} finally { await client.close(); server.closeAllConnections(); server.close(); }
