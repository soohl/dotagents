// Run inside Chat after enabling Calendar. Never print calendar or event content.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parse } from '/app/node_modules/yaml/dist/index.js';
import { Client } from '/app/node_modules/@modelcontextprotocol/client/dist/index.mjs';
import { StdioClientTransport } from '/app/node_modules/@modelcontextprotocol/client/dist/stdio.mjs';
import { createMcpToolDefinition } from '/app/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js';
import { assertSupportedJsonSchema } from '/app/node_modules/@deepseek-ai/dsh-tools/lib/index.js';

assert.equal(process.env.HARNESS_ROLE, 'chat');
const rows = parse(await readFile('/data/harness/profiles/web/cordis.patch.yml', 'utf8'));
const preset = rows.flatMap(row => row.insert ?? []).find(row => row.id === 'preset-chat');
assert.ok(preset.config.plugins.some(p => p.id === 'mcp-calendar'));
const client = new Client({name: 'dotagents-calendar-check', version: '1'});
try {
  await client.connect(new StdioClientTransport({command: 'node', args: ['/app/runtime/calendar-transport.mjs']}));
  const {tools} = await client.listTools();
  assert.equal(tools.length, 4);
  for (const tool of tools) {
    assert.equal(tool.annotations.readOnlyHint, true);
    assertSupportedJsonSchema(tool.inputSchema);
    createMcpToolDefinition({get: () => undefined}, {name: 'mcp__calendar__' + tool.name,
      rawName: tool.name, description: tool.description, inputSchema: tool.inputSchema,
      outputSchema: tool.outputSchema, call: args => client.callTool({name: tool.name, arguments: args})});
  }
  const health = await client.callTool({name: 'calendar_health', arguments: {}});
  assert.notEqual(health.isError, true);
  assert.equal(health.structuredContent.read_only, true);
  assert.equal(health.structuredContent.readable, true);
  console.log('PASS: Chat discovers four read-only Calendar tools; EventKit read access is available.');
} finally { await client.close(); }
