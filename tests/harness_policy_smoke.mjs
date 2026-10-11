// Run inside the built image. No live application data or inference is used.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import http from 'node:http';
import { Client } from '/app/node_modules/@modelcontextprotocol/client/dist/index.mjs';
import { StdioClientTransport } from '/app/node_modules/@modelcontextprotocol/client/dist/stdio.mjs';
import { BasicCompactionEngine } from '/app/node_modules/@deepseek-ai/dsh-compaction-basic/lib/index.js';
const policy = JSON.parse(await readFile('/app/policy.json', 'utf8'));
assert.deepEqual(BasicCompactionEngine.Config(policy.compaction), { ...policy.compaction, modelPolicies: [] });
const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end('<html><title>Browser fixture</title><body>' + '<p>Bounded browser output fixture.</p>'.repeat(1800) + '</body></html>');
}).listen(8890, '127.0.0.1');
try {
  for (const kind of ['chat', 'agent']) {
    const client = new Client({ name: 'policy-check', version: '1' });
    try {
      await client.connect(new StdioClientTransport({ command: 'node', args: ['/app/runtime/' + kind + '-browser.mjs'],
        env: { ...process.env, PLAYWRIGHT_BROWSERS_PATH: '/opt/playwright' }, stderr: 'inherit' }));
      const { tools } = await client.listTools();
      assert.equal(tools.length, kind === 'chat' ? 4 : 12);
      const result = await client.callTool({ name: 'browser_navigate', arguments: { url: 'http://127.0.0.1:8890' } });
      assert.notEqual(result.isError, true, JSON.stringify(result));
      assert.ok(Array.from(result.content.filter(p => p.type === 'text').map(p => p.text).join('\n')).length <= 12000);
      const snapshot = await client.callTool({ name: 'browser_snapshot', arguments: {} });
      assert.notEqual(snapshot.isError, true, JSON.stringify(snapshot));
      const text = snapshot.content.filter(p => p.type === 'text').map(p => p.text).join('\n');
      assert.ok(Array.from(text).length <= 12000);
      if (kind === 'chat') {
        assert.match(text, /Continue with browser_snapshot/);
        const more = await client.callTool({ name: 'browser_snapshot', arguments: { start: 11600 } });
        assert.notEqual(more.isError, true);
        assert.match(more.content[0].text, /Showing characters 11600-/);
      } else {
        const path = text.match(/Full output: (.+?)\. Read only/)[1];
        assert.ok((await readFile(path, 'utf8')).length > 12000);
      }
      console.log('PASS: ' + kind + ' tool count, real browser, bounded output and complete follow-up access.');
    } finally { await client.close(); }
  }
} finally { server.closeAllConnections(); server.close(); }
