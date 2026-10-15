// Run in a disposable Harness container with /data, /workspace and
// /run/dotagents writable, and seed profiles mounted read-only at /seeds.
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { readFile, writeFile } from 'node:fs/promises';
import { chromium } from '/app/node_modules/playwright/index.mjs';

await writeFile('/run/dotagents/endpoints.json', JSON.stringify([
  { provider: 'dotagents-test', baseURL: 'http://127.0.0.1:8888/v1' },
]));
for (const role of ['chat', 'agent']) {
  const profile = JSON.parse(await readFile(`/seeds/${role}-profile.json`, 'utf8'));
  function configure(rows) { for (const row of rows) {
    if (row.id === 'agent-default-model') row.config = {provider:'dotagents-test',model:'first'};
    if (row.insert) configure(row.insert);
  } }
  configure(profile);
  await writeFile(`/run/dotagents/${role}-profile.json`, JSON.stringify(profile));
}
const calls = [];
let inferenceRunning = false;
let observeRequest;
let browserCheck;
let clockCheck;
let clockEvidence;
let fileCheck;
let fileEvidence;
function nextRequest() {
  return new Promise(resolve => { observeRequest = resolve; });
}
function checkNoTime(request) {
  const text = JSON.stringify(request.messages);
  assert.doesNotMatch(text, /Browser time zone for this request|Time sampled while preparing turn/);
}
const inference = http.createServer(async (req, res) => {
  if (req.url === '/v1/models') {
    if (!inferenceRunning) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end('{}');
      return;
    }
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ data: ['first','second'].map(id => ({ id, name: `${id} test model` })) }));
    return;
  }
  let body = ''; for await (const chunk of req) body += chunk;
  const request = JSON.parse(body); calls.push(request.model);
  observeRequest?.(request); observeRequest = undefined;
  let message = { role: 'assistant', content: 'OK' };
  let finishReason = 'stop';
  if (fileCheck) {
    const last = request.messages.at(-1);
    if (last.role === 'tool') {
      fileEvidence = JSON.stringify(last.content);
      message.content = 'FILE_READ_OK';
      fileCheck = undefined;
    } else {
      const text = request.messages.filter(m => m.role === 'user').map(m =>
        typeof m.content === 'string' ? m.content : m.content.filter(p => p.type === 'text').map(p => p.text).join('\n')).join('\n');
      const path = fileCheck.path ?? text.match(/verbatim read-only copy saved at "([^"\n]+)"/)?.[1];
      assert.ok(path, 'Uploaded Markdown must expose its readable path.');
      const names = request.tools.map(t => t.function.name);
      for (const name of ['read', 'read_image', 'glob', 'grep']) assert.ok(names.includes(name), name);
      for (const name of ['write', 'edit', 'bash']) assert.ok(!names.includes(name), name);
      message = {role:'assistant',content:null,tool_calls:[{
        id:'read-test-'+crypto.randomUUID(),type:'function',function:{name:'read',arguments:JSON.stringify({file_path:path})},
      }]};
      finishReason = 'tool_calls';
    }
  }
  if (clockCheck) {
    if (request.messages.at(-1).role === 'tool') {
      clockEvidence = JSON.stringify(request.messages.at(-1).content);
      message.content = 'CLOCK_OK';
      clockCheck = false;
    } else {
      assert.ok(request.tools.some(tool => tool.function.name === 'current_time'));
      message = {role:'assistant',content:null,tool_calls:[{
        id:'clock-test',type:'function',function:{name:'current_time',arguments:JSON.stringify({time_zone:'America/New_York'})},
      }]};
      finishReason = 'tool_calls';
    }
  }
  if (browserCheck) {
    const prefix = `mcp__${browserCheck.server}__`;
    const names = request.tools.map(tool => tool.function.name);
    for (const name of ['browser_navigate','browser_snapshot','browser_click','browser_type']) {
      assert.ok(names.includes(prefix + name), 'Browser tool missing from model request: ' + name);
    }
    if (browserCheck.step === 0) {
      message = {role:'assistant',content:null,tool_calls:[{
        id:'browser-navigate',type:'function',function:{name:prefix+'browser_navigate',arguments:JSON.stringify({url:'http://127.0.0.1:8892'})},
      }]};
      browserCheck.step++;
      finishReason = 'tool_calls';
    } else if (browserCheck.step === 1) {
      assert.equal(request.messages.at(-1).role,'tool');
      assert.match(JSON.stringify(request.messages.at(-1).content),/Browser integration fixture/);
      message = {role:'assistant',content:null,tool_calls:[{
        id:'browser-snapshot',type:'function',function:{name:prefix+'browser_snapshot',arguments:'{}'},
      }]};
      browserCheck.step++;
      finishReason = 'tool_calls';
    } else {
      assert.equal(request.messages.at(-1).role,'tool');
      assert.match(JSON.stringify(request.messages.at(-1).content),/browser-evidence-93d7/);
      message.content = 'BROWSER_OK';
      browserCheck = undefined;
    }
  }
  const common = { id: 'test-completion', created: 1, model: request.model };
  if (request.stream) {
    res.setHeader('Content-Type', 'text/event-stream');
    res.end(`data: ${JSON.stringify({ ...common, object: 'chat.completion.chunk', choices: [{ index: 0, delta: { ...message, ...(message.tool_calls ? {tool_calls:message.tool_calls.map((call,index)=>({...call,index}))} : {}) }, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ ...common, object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: finishReason }], usage: { prompt_tokens: 10, completion_tokens: 1, total_tokens: 11 } })}\n\ndata: [DONE]\n\n`);
  } else {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ...common, object: 'chat.completion', choices: [{ index: 0, message, finish_reason: finishReason }] }));
  }
}).listen(8888, '127.0.0.1');
const browserFixture = http.createServer((_req,res) => {
  res.setHeader('Content-Type','text/html');
  res.end('<title>Browser integration fixture</title><p>browser-evidence-93d7</p><input aria-label="Search"><button>Search</button>');
}).listen(8892,'127.0.0.1');
let child;
let logs = '';
async function stop() {
  if (!child || child.exitCode !== null) return;
  const done = new Promise(resolve => child.once('exit', resolve));
  child.kill('SIGTERM'); await done;
}
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const deadline = setTimeout(() => { console.error(logs); process.exit(1); }, 110000);
try {
  for (const role of ['chat', 'agent']) {
    inferenceRunning = false;
    const port = role === 'chat' ? 3002 : 3003;
    const gateway = http.createServer((req,res) => {
      const upstream = http.request({host:'127.0.0.1',port:3081,path:req.url,method:req.method,headers:req.headers}, response => {
        res.writeHead(response.statusCode,response.headers); response.pipe(res);
      }); upstream.on('error',()=>{res.writeHead(502);res.end()}); req.pipe(upstream);
    }).listen(port);
    gateway.on('upgrade', (req,socket,head) => {
      const upstream = http.request({host:'127.0.0.1',port:3081,path:req.url,headers:req.headers});
      upstream.on('upgrade',(response,remote,remoteHead)=>{
        socket.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(response.headers).map(([k,v])=>`${k}: ${v}`).join('\r\n')}\r\n\r\n`);
        if(remoteHead.length) socket.write(remoteHead); if(head.length) remote.write(head);
        socket.pipe(remote).pipe(socket); socket.on('error',()=>remote.destroy());remote.on('error',()=>socket.destroy());
      }); upstream.on('error',()=>socket.destroy()); upstream.end();
    });
    async function start() {
      child = spawn('node', ['/app/runtime/entry.mjs'], { env: {...process.env, HARNESS_ROLE:role, DOTAGENTS_LOCAL_PLACEHOLDER:'unused', DSH_HOME:`/data/${role}`} });
      child.stdout.on('data', chunk => { logs += chunk; }); child.stderr.on('data', chunk => { logs += chunk; });
      for (let n=0;n<100;n++) {
        try { if ((await fetch(`http://127.0.0.1:3081/_dotagents/status`)).ok) return; } catch {}
        await new Promise(resolve => setTimeout(resolve,100));
      }
      throw new Error('Harness startup timed out: '+logs);
    }
    await start();
    const context = await browser.newContext();
    const page = await context.newPage(); page.setDefaultTimeout(15000);
    page.on('pageerror', error => console.error('Browser error:', error.message));
    page.on('console', message => { if (message.type() === 'error') console.error('Browser console:', message.text()); });
    page.on('requestfailed', request => console.error('Request failed:', new URL(request.url()).pathname, request.failure()));
    const base = `http://localhost:${port}/${role}`;
    const rpc = async (method,request) => {
      const response = await context.request.post(`${base}/api/${method}`, {data:{type:'client-request',rpcId:crypto.randomUUID(),method,payload:{args:{request}}}});
      return (await response.json()).result;
    };
    await page.goto(base+'/');
    await page.getByRole('button', { name: 'Continue', exact: true }).click();
    const conversation = page.locator('[data-conversation-session]');
    await conversation.waitFor().catch(async error => { console.error(await page.locator('body').innerText()); throw error; });
    const sessionId = await conversation.getAttribute('data-conversation-session');
    const pick = model => rpc('session/selectModel',{sessionId,provider:'dotagents-test',model});
    const offline = page.getByRole('button', { name: 'Inference engine is not running', exact: true });
    async function expectOffline() {
      await offline.waitFor();
      assert.equal(await offline.isDisabled(), true);
      assert.equal(await offline.innerText(), 'Inference engine is not running');
      assert.equal(await offline.getAttribute('title'), 'Inference engine is not running');
      assert.equal(await page.getByRole('menuitemradio').count(), 0);
    }
    // A saved default must not appear before inference starts.
    await expectOffline();
    inferenceRunning = true;
    await page.getByTitle(/^first test model(?: ·.*)?$/).waitFor();
    assert.equal((await pick('first')).ok,true);
    await page.getByTitle(/^first test model(?: ·.*)?$/).waitFor();
    assert.equal(await page.getByTitle(/^first test model(?: ·.*)?$/).isEnabled(),true);
    assert.equal((await pick('second')).ok,true);
    const second = page.getByTitle(/^second test model(?: ·.*)?$/);
    await second.click();
    inferenceRunning = false;
    await expectOffline();
    inferenceRunning = true;
    await second.waitFor();
    assert.equal(await second.isEnabled(), true);
    assert.equal((await pick('first')).ok,true);
    const firstRequest = nextRequest();
    const prompt = await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',clientTimeZone:'America/New_York',content:[{type:'text',text:'Reply OK.'}]});
    assert.equal(prompt.ok,true,JSON.stringify(prompt));
    const changed = await pick('second');
    assert.equal(changed.ok,false); assert.equal(changed.error.code,'session/model-locked');
    const locked = page.getByTitle('Start a new conversation to use another model.',{exact:true});
    await locked.waitFor(); assert.equal(await locked.isDisabled(),true);
    await page.getByText('OK',{exact:true}).first().waitFor();
    inferenceRunning = false;
    await expectOffline();
    await page.reload();
    await expectOffline();
    inferenceRunning = true;
    await locked.waitFor();
    assert.match(await locked.innerText(), /first test model/);
    assert.equal(await locked.isDisabled(), true);
    console.log(`PASS: ${role} hides offline models on startup, live outages and reload, then restores the saved choice and lock.`);
    const first = await firstRequest;
    checkNoTime(first);
    if (role === 'agent') {
      const names = first.tools.map(t => t.function.name);
      for (const name of ['read', 'write', 'edit']) assert.ok(names.includes(name), name);
    }
    assert.ok(calls.includes('first'));
    await page.reload(); await locked.waitFor(); assert.equal(await locked.isDisabled(),true);
    await stop(); await start(); await page.reload(); await locked.waitFor();
    assert.equal(await locked.isDisabled(),true);
    assert.equal((await pick('second')).error.code,'session/model-locked');
    if (role === 'chat') {
      const resumedRequest = nextRequest();
      assert.equal((await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',
        clientTimeZone:'Asia/Seoul',content:[{type:'text',text:'Reply OK again.'}]})).ok,true);
      checkNoTime(await resumedRequest);
      await page.getByText('OK',{exact:true}).nth(1).waitFor();
      console.log('PASS: Chat does not inject time or timezone on initial and resumed turns.');
      const markdown = '# Attached notes\n\nEvidence marker: attachment-47c2.\n';
      const uploaded = await context.request.post(`${base}/api/session/uploadFileBinary?sessionId=${sessionId}&name=notes.md`, {
        data: Buffer.from(markdown), headers: {'Content-Type':'application/octet-stream'},
      });
      const receipt = await uploaded.json();
      assert.equal(receipt.ok,true,JSON.stringify(receipt));
      fileCheck = {};
      assert.equal((await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',content:[
        {type:'text',text:'Read this attached Markdown file.'}, {type:'file',receiptId:receipt.value.receiptId},
      ]})).ok,true);
      await page.getByText('FILE_READ_OK',{exact:true}).first().waitFor();
      assert.match(fileEvidence,/attachment-47c2/);
      await writeFile('/workspace/notes.custom-extension', 'workspace-marker-68e9');
      fileCheck = {path:'/workspace/notes.custom-extension'};
      assert.equal((await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',content:[
        {type:'text',text:'Read /workspace/notes.custom-extension.'},
      ]})).ok,true);
      await page.getByText('FILE_READ_OK',{exact:true}).nth(1).waitFor();
      assert.match(fileEvidence,/workspace-marker-68e9/);
      assert.equal(await readFile('/workspace/notes.custom-extension','utf8'),'workspace-marker-68e9');
      console.log('PASS: Chat reads uploaded Markdown and arbitrary-extension workspace files; no write/edit/shell tools.');
      const beforeClock = Date.now();
      clockCheck = true;
      assert.equal((await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',content:[
        {type:'text',text:'What is the current time in New York? Use the local clock.'},
      ]})).ok,true);
      await page.getByText('CLOCK_OK',{exact:true}).waitFor();
      assert.match(clockEvidence,/America\/New_York/);
      const sampled = clockEvidence.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z/);
      assert.ok(sampled,clockEvidence);
      assert.ok(Date.parse(sampled[0]) >= beforeClock && Date.parse(sampled[0]) <= Date.now());
      console.log('PASS: Chat calls the local clock on demand and receives the current time in the requested zone.');


    }
    browserCheck = {server:role === 'chat' ? 'web' : 'playwright',step:0};
    assert.equal((await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',content:[
      {type:'text',text:'Use the browser to open and read the local research fixture.'},
    ]})).ok,true);
    await page.getByText('BROWSER_OK',{exact:true}).waitFor();
    console.log(`PASS: ${role} advertises browser tools to the model and executes navigation and snapshot through Harness.`);
    const fresh = await rpc('session/create',{cwd:'/workspace',agentPreset:role});
    assert.equal(fresh.ok,true);
    await page.waitForTimeout(5500);
    assert.equal((await rpc('session/selectModel',{sessionId:fresh.value.sessionId,provider:'dotagents-test',model:'second'})).ok,true);
    console.log(`PASS: ${role}: new-session choice, first-message lock, RPC rejection, reload and process restart`);
    await context.close(); await stop(); gateway.closeAllConnections(); gateway.close();
  }
} catch(error) {
  console.error(logs); throw error;
} finally {
  clearTimeout(deadline); await stop(); await browser.close(); inference.closeAllConnections(); inference.close(); browserFixture.closeAllConnections(); browserFixture.close();
}
