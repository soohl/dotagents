// Run inside the Harness image on the Compose network. Opening the UI may create
// its initial blank session. No prompts are sent and no inference is requested.
const { chromium } = require('/app/node_modules/playwright');
const http = require('node:http');
const assert = require('node:assert/strict');
// Preserve real localhost URLs and Host/Origin headers while reaching Docker's gateway.
const gateways = [[3000,'127.0.0.1',8080],[3002,'chat',3081],[3003,'agent',3081]].map(([port,host,target])=>{
const gateway=http.createServer((req,res) => {
  const upstream = http.request({host,port:target,path:req.url,method:req.method,headers:req.headers}, response => {
    res.writeHead(response.statusCode,response.headers); response.pipe(res);
  });
  upstream.on('error',()=>{res.writeHead(502);res.end()});
  req.on('aborted',()=>upstream.destroy());
  res.on('close',()=>upstream.destroy());
  req.pipe(upstream);
});
gateway.on('upgrade',(req,socket,head)=>{
  const upstream=http.request({host,port:target,path:req.url,headers:req.headers});
  upstream.on('upgrade',(response,remote,remoteHead)=>{
    socket.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(response.headers).map(([k,v])=>`${k}: ${v}`).join('\r\n')}\r\n\r\n`);
    if(remoteHead.length) socket.write(remoteHead);
    if(head.length) remote.write(head);
    socket.pipe(remote).pipe(socket);
    socket.on('error',()=>remote.destroy());remote.on('error',()=>socket.destroy());
  });
  upstream.on('response',r=>{socket.end(`HTTP/1.1 ${r.statusCode} Rejected\r\n\r\n`);r.resume()});
  upstream.on('error',()=>socket.destroy());upstream.end();
});
return gateway.listen(port);
});
const deadline=setTimeout(()=>{console.error('FAIL: dashboard deadline');process.exit(1)},90000);
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  try {
    const context=await browser.newContext({viewport:{width:1440,height:1000},colorScheme:'dark'});
    const page=await context.newPage();page.setDefaultTimeout(15000);
    const failures=[];
    page.on('response',r=>{if(r.status()>=400 && !['/images/api/options','/images/api/health'].includes(new URL(r.url()).pathname)) failures.push(r.status()+' '+new URL(r.url()).pathname)});
    for(const hostname of ['127.0.0.1','localhost']) {
      const origin='http://'+hostname+':3000';
      await page.goto(origin);
      await page.getByRole('heading',{name:'Services',exact:true}).waitFor();
      assert.equal(await page.locator('nav[aria-label=Services] a').count(),3);
      for(const [name,path] of [['Chat','/chat/'],['Agent','/agent/'],['Image generation','/images/']]) {
        const card=page.getByRole('link',{name:'Open '+name,exact:true});
        assert.equal(await card.getAttribute('href'),path);
        assert.equal(await card.locator('.url').innerText(),origin+path);
      }
    }
    await page.goto('http://localhost:3000');
    console.log('SCREENSHOT desktop '+(await page.screenshot({fullPage:true})).toString('base64'));
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    console.log('SCREENSHOT mobile '+(await page.screenshot({fullPage:true})).toString('base64'));
    await page.setViewportSize({width:1440,height:1000});
    await page.emulateMedia({colorScheme:'light'});
    console.log('SCREENSHOT light '+(await page.screenshot({fullPage:true})).toString('base64'));
    console.log('PASS: both local hostnames, service URLs, and desktop/mobile layout');
    for(const name of ['Chat','Agent']) {
      await page.goto('http://localhost:3000');
      await page.getByRole('link',{name:'Open '+name,exact:true}).click();
      await page.getByText(name,{exact:true}).waitFor();
      await page.locator('[data-conversation-session]').waitFor();
      assert.equal(await page.getByText(/Unable to create default workspace/).count(),0);
      assert.equal(await page.getByText('Choose a workspace to start',{exact:true}).count(),0);
      assert.equal(new URL(page.url()).pathname,'/'+name.toLowerCase()+'/');
      await page.reload();
      await page.getByText(name,{exact:true}).waitFor();
      await page.locator('[data-conversation-session]').waitFor();
      assert.equal(await page.getByText(/Unable to create default workspace/).count(),0);
    }
    await page.goto('http://localhost:3000/images/');
    await page.getByRole('textbox',{name:'Prompt',exact:true}).waitFor();
    assert.equal((await context.request.get('http://localhost:3000/images/api/sessions')).status(),200);
    assert.deepEqual(failures,[]);
    console.log('PASS: Chat and Agent presets, refresh, Image assets, and saved-session API');
    const catalogs = {};
    for (const role of ['chat','agent']) {
      const status = await context.request.get('http://localhost:3000/'+role+'/_dotagents/status');
      assert.equal((await status.json()).role, role);
      for (const method of ['session/list','agentPresets/list','workspace/initializeDefault']) {
        const response = await context.request.post('http://localhost:3000/'+role+'/api/'+method, {
          data: {type:'client-request',rpcId:crypto.randomUUID(),method,
                 payload:{args:method==='session/list'?{_request:{}}:{}}}
        });
        assert.equal(response.status(),200);
        const {result} = await response.json();
        assert.equal(result.ok,true,role+' '+method+': '+JSON.stringify(result.error));
        if (method==='session/list') catalogs[role]=result.value.items.map(item=>item.sessionId);
        else if (method==='agentPresets/list') assert.deepEqual(result.value.presets.map(p=>p.id),[role]);
        else assert.equal(result.value.workspace.path,'/workspace');
      }
    }
    assert.ok(catalogs.chat.length>0 && catalogs.agent.length>0);
    assert.ok(catalogs.chat.every(id=>!catalogs.agent.includes(id)));
    const cookies=(await context.cookies()).filter(c=>c.name.startsWith('dsh-auth-'));
    const chatCookie=cookies.find(c=>c.path==='/chat/');
    const agentCookie=cookies.find(c=>c.path==='/agent/');
    assert.ok(chatCookie && agentCookie);
    assert.notEqual(chatCookie.name,agentCookie.name);
    const wrong=await browser.newContext();
    try {
      await wrong.addCookies([{...chatCookie,path:'/agent/'}]);
      assert.equal((await wrong.request.post('http://localhost:3000/agent/api/session/list',{
        data:{type:'client-request',rpcId:crypto.randomUUID(),method:'session/list',payload:{args:{_request:{}}}}
      })).status(),401);
    } finally { await wrong.close(); }
    console.log('PASS: automatic mounted workspaces, separate session catalogs, one preset per service, and independent browser cookies');
    for (const [role,port] of [['chat',3002],['agent',3003]]) {
      const origin='http://localhost:'+port;
      await page.goto(origin);
      await page.locator('[data-conversation-session]').waitFor();
      assert.equal(new URL(page.url()).pathname,'/'+role+'/');
      assert.equal(await page.getByText(/Unable to create default workspace/).count(),0);
      const response=await context.request.get(origin+'/'+role+'/_dotagents/status');
      assert.equal((await response.json()).role,role);
      assert.equal((await context.request.get(origin+'/',{headers:{Host:'foreign.invalid'}})).status(),403);
      assert.equal((await context.request.get(origin+'/'+role+'/_dotagents/status',{
        headers:{Origin:'http://localhost:3000'}})).status(),403);
    }
    console.log('PASS: distinct local Chat/Agent ports, mounted workspaces, and direct-access request boundaries');
    const rejected=await context.request.post('http://localhost:3000/images/api/generate',{headers:{Origin:'https://foreign.invalid'},data:{}});
    assert.equal(rejected.status(),403);
    assert.equal((await context.request.get('http://localhost:3000/',{headers:{Host:'foreign.invalid'}})).status(),403);
    console.log('PASS: foreign origins and hostnames remain blocked');
  } finally {await browser.close();for(const gateway of gateways){gateway.closeAllConnections();gateway.close();}clearTimeout(deadline)}
})().catch(e=>{console.error('FAIL:',e.message);process.exitCode=1});
