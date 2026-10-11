// Run with docker exec in Chat. Read-only automation; no task content is printed.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parse } from '/app/node_modules/yaml/dist/index.js';
import { Client } from '/app/node_modules/@modelcontextprotocol/client/dist/index.mjs';
import { StdioClientTransport } from '/app/node_modules/@modelcontextprotocol/client/dist/stdio.mjs';
import { createMcpToolDefinition } from '/app/node_modules/@deepseek-ai/dsh-mcp-client/lib/index.js';
import { assertSupportedJsonSchema } from '/app/node_modules/@deepseek-ai/dsh-tools/lib/index.js';

assert.equal(process.env.HARNESS_ROLE,'chat');
const rows=parse(await readFile('/data/harness/profiles/web/cordis.patch.yml','utf8'));
const preset=rows.flatMap(row=>row.insert??[]).find(row=>row.id==='preset-chat');
assert.ok(preset.config.plugins.some(p=>p.id==='mcp-things' && p.config.serverName==='things'));
const client=new Client({name:'dotagents-things-check',version:'1'});
try {
  await client.connect(new StdioClientTransport({command:'node',args:['/app/runtime/things-transport.mjs']}));
  const {tools}=await client.listTools();
  for(const tool of tools) {
    assertSupportedJsonSchema(tool.inputSchema);
    createMcpToolDefinition({get:()=>undefined},{name:'mcp__things__'+tool.name,
    rawName:tool.name,description:tool.description,inputSchema:tool.inputSchema,outputSchema:tool.outputSchema,
    call:args=>client.callTool({name:tool.name,arguments:args})});
  }
  const health=await client.callTool({name:'things_health',arguments:{}});
  assert.notEqual(health.isError,true);
  assert.ok(health.structuredContent.version);
  console.log(`PASS: Chat discovers ${tools.length} compatible Things tools; macOS automation reports Things ${health.structuredContent.version}`);
} finally {await client.close();}
