// Run with the built Harness image. Exercise the actual MCP boundary and public browser.
import assert from 'node:assert/strict';
import { Client } from '/app/node_modules/@modelcontextprotocol/client/dist/index.mjs';
import { StdioClientTransport } from '/app/node_modules/@modelcontextprotocol/client/dist/stdio.mjs';

const client = new Client({name:'chat-browser-check',version:'1.0.0'});
try {
  await client.connect(new StdioClientTransport({command:'node',args:['/app/runtime/chat-browser.mjs'],
    env:{...process.env,PLAYWRIGHT_BROWSERS_PATH:'/opt/playwright'},stderr:'pipe'}));
  const {tools}=await client.listTools();
  assert.deepEqual(tools.map(t=>t.name).sort(),['browser_close','browser_navigate','browser_navigate_back','browser_snapshot']);
  for(const [name,args] of [
    ['browser_run_code',{code:'return process.env'}],
    ['browser_evaluate',{function:'()=>document.cookie'}],
    ['browser_file_upload',{paths:['/data/file']}],
    ['browser_snapshot',{filename:'/tmp/disallowed.txt'}],
    ['browser_navigate',{url:'file:///etc/passwd'}],
    ['browser_navigate',{url:'javascript:alert(1)'}],
  ]) assert.equal((await client.callTool({name,arguments:args})).isError,true);
  console.log('PASS: only browsing tools are exposed; code, file access, and writes are rejected');
  const page = await client.callTool({name:'browser_navigate',arguments:{url:'https://example.com'}});
  assert.notEqual(page.isError,true,JSON.stringify(page));
  assert.match(JSON.stringify(page),/Example Domain/);
  const snapshot=await client.callTool({name:'browser_snapshot',arguments:{}});
  assert.notEqual(snapshot.isError,true);
  assert.match(JSON.stringify(snapshot),/Example Domain/);
  console.log('PASS: Chat can navigate and read a public HTTPS page');
} finally {await client.close()}
