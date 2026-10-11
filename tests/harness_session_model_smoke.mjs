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
let observeRequest;
function nextRequest() {
  return new Promise(resolve => { observeRequest = resolve; });
}
function checkLocalTime(request, zone) {
  const text = JSON.stringify(request.messages);
  assert.ok(text.includes(`Browser time zone for this request: ${zone}.`));
  assert.match(text, /Time sampled while preparing turn/);
  assert.ok(text.includes(`[${zone}]`));
}
const inference = http.createServer(async (req, res) => {
  if (req.url === '/v1/models') {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ data: ['first','second'].map(id => ({ id, name: `${id} test model` })) }));
    return;
  }
  let body = ''; for await (const chunk of req) body += chunk;
  const request = JSON.parse(body); calls.push(request.model);
  observeRequest?.(request); observeRequest = undefined;
  const common = { id: 'test-completion', created: 1, model: request.model };
  if (request.stream) {
    res.setHeader('Content-Type', 'text/event-stream');
    res.end(`data: ${JSON.stringify({ ...common, object: 'chat.completion.chunk', choices: [{ index: 0, delta: { role: 'assistant', content: 'OK' }, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ ...common, object: 'chat.completion.chunk', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 10, completion_tokens: 1, total_tokens: 11 } })}\n\ndata: [DONE]\n\n`);
  } else {
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ...common, object: 'chat.completion', choices: [{ index: 0, message: { role: 'assistant', content: 'OK' }, finish_reason: 'stop' }] }));
  }
}).listen(8888, '127.0.0.1');
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
      child = spawn('node', ['/app/runtime/entry.mjs'], { env: {...process.env, HARNESS_ROLE:role, DSH_HOME:`/data/${role}`} });
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
    const conversation = page.locator('[data-conversation-session]');
    await conversation.waitFor().catch(async error => { console.error(await page.locator('body').innerText()); throw error; });
    const sessionId = await conversation.getAttribute('data-conversation-session');
    const pick = model => rpc('session/selectModel',{sessionId,provider:'dotagents-test',model});
    // Discovery is asynchronous, so wait for the model catalog to populate.
    await page.waitForTimeout(5500);
    assert.equal((await pick('first')).ok,true);
    await page.getByTitle(/^first test model(?: ·.*)?$/).waitFor();
    assert.equal(await page.getByTitle(/^first test model(?: ·.*)?$/).isEnabled(),true);
    assert.equal((await pick('second')).ok,true);
    assert.equal((await pick('first')).ok,true);
    const firstRequest = nextRequest();
    const prompt = await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',clientTimeZone:'America/New_York',content:[{type:'text',text:'Reply OK.'}]});
    assert.equal(prompt.ok,true,JSON.stringify(prompt));
    const changed = await pick('second');
    assert.equal(changed.ok,false); assert.equal(changed.error.code,'session/model-locked');
    const locked = page.getByTitle('Start a new conversation to use another model.',{exact:true});
    await locked.waitFor(); assert.equal(await locked.isDisabled(),true);
    await page.getByText('OK',{exact:true}).first().waitFor();
    if (role === 'chat') checkLocalTime(await firstRequest, 'America/New_York');
    assert.ok(calls.includes('first'));
    await page.reload(); await locked.waitFor(); assert.equal(await locked.isDisabled(),true);
    await stop(); await start(); await page.reload(); await locked.waitFor();
    assert.equal(await locked.isDisabled(),true);
    assert.equal((await pick('second')).error.code,'session/model-locked');
    if (role === 'chat') {
      const resumedRequest = nextRequest();
      assert.equal((await rpc('session/prompt',{sessionId,requestId:crypto.randomUUID(),mode:'queue',
        clientTimeZone:'Asia/Seoul',content:[{type:'text',text:'Reply OK again.'}]})).ok,true);
      checkLocalTime(await resumedRequest, 'Asia/Seoul');
      await page.getByText('OK',{exact:true}).nth(1).waitFor();
      console.log('PASS: Chat sends fresh browser-local time on initial and resumed turns.');
    }
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
  clearTimeout(deadline); await stop(); await browser.close(); inference.closeAllConnections(); inference.close();
}
