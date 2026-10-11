import test from 'node:test';
import assert from 'node:assert/strict';
import { allowedTools, filterResult, validateCall } from '../src/harness_runtime/chat-browser.mjs';

test('Chat exposes only browser navigation and text snapshots',()=>{
  const result=filterResult('tools/list',{tools:[...allowedTools,'browser_run_code','browser_evaluate','browser_file_upload'].map(name=>({name,inputSchema:{}}))});
  assert.deepEqual(result.tools.map(t=>t.name),[...allowedTools]);
  assert.deepEqual(filterResult('initialize',{capabilities:{tools:{},resources:{},prompts:{}}}).capabilities,{tools:{}});
  assert.equal(result.tools.find(t=>t.name==='browser_snapshot').inputSchema.properties.start.minimum,0);
  assert.throws(()=>filterResult('tools/list',{tools:[]}),/changed/);
});

test('Chat rejects executable URLs, arbitrary tools, extra parameters, and output paths',()=>{
  assert.deepEqual(validateCall('browser_navigate',{url:'https://example.com'}),{url:'https://example.com'});
  assert.deepEqual(validateCall('browser_snapshot'),{});
  for(const [name,args] of [
    ['browser_run_code',{}],['browser_evaluate',{}],['browser_file_upload',{}],
    ['browser_snapshot',{filename:'/workspace/result.txt'}],['browser_snapshot',null],
    ['browser_navigate',{url:'file:///etc/passwd'}],['browser_navigate',{url:'javascript:alert(1)'}],
    ['browser_navigate',{url:'https://user:password@example.com'}],
    ['browser_navigate',{url:'https://example.com',code:'anything'}],
  ]) assert.throws(()=>validateCall(name,args));
});

test('search keeps the credential in a header and returns bounded source links',async()=>{
  const {searchBrave}=await import('../src/harness_runtime/web-search-brave.mjs');
  const result=await searchBrave({query:'example domains',maxResults:1},undefined,'test-only',async(url,options)=>{
    assert.equal(url.origin,'https://api.search.brave.com');
    assert.equal(url.searchParams.get('q'),'example domains');
    assert.equal(options.headers['X-Subscription-Token'],'test-only');
    assert.equal(options.redirect,'error');
    assert.ok(!String(url).includes('test-only'));
    return {ok:true,json:async()=>({web:{results:[{url:'https://example.com',title:'Example',description:'Snippet'},{url:'https://example.org'}]}})};
  });
  assert.deepEqual(result,{sources:[{url:'https://example.com',title:'Example',snippet:'Snippet'}],truncated:true});
  await assert.rejects(searchBrave({query:'example'},undefined,''),/not configured/);
  await assert.rejects(searchBrave({query:'example'},undefined,'test-only',async()=>({ok:false,status:429})),/HTTP 429/);
});
