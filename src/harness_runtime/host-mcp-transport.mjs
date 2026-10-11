// Fixed local MCP relays. Authenticate before forwarding stdio.
import net from 'node:net';
import { readFile } from 'node:fs/promises';
import { Transform } from 'node:stream';
import { harnessSchema } from './things-schema.mjs';

function lines(transform) {
  let pending = '';
  return new Transform({ decodeStrings: false, transform(chunk, _, done) {
    pending += chunk;
    if (Buffer.byteLength(pending) > 16 * 1024 * 1024) return done(new Error('MCP message too large'));
    try {
      let end;
      while ((end = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0,end); pending = pending.slice(end+1);
        if (line.trim()) this.push(JSON.stringify(transform(JSON.parse(line)))+'\n');
      }
      done();
    } catch(error) { done(error); }
  }});
}
export function relay(name) {
const endpoints = {things: 8766, calendar: 8767};
if (!Object.hasOwn(endpoints, name)) throw new Error('Unknown local MCP');
const catalogs = new Set();
const requests = lines(message => {
  if (message.method === 'tools/list') catalogs.add(message.id);
  if (catalogs.size > 128) throw new Error('Too many pending MCP catalogs');
  return message;
});
const responses = lines(message => {
  if (catalogs.delete(message.id) && message.result?.tools) {
    message.result.tools = message.result.tools.map(tool => ({...tool,inputSchema:harnessSchema(tool.inputSchema)}));
  }
  return message;
});

return readFile(`/tmp/dotagents-${name}-token`, 'utf8').then(token => {
const socket = net.createConnection({ host: 'host.docker.internal', port: endpoints[name] });
const deadline = setTimeout(() => socket.destroy(new Error('Local MCP connection timed out')), 5000);
socket.once('connect', () => socket.write(JSON.stringify({token, mode:'stdio'}) + '\n'));
let greeting = Buffer.alloc(0);
function authenticate(chunk) {
  greeting = Buffer.concat([greeting, chunk]);
  const end = greeting.indexOf(10);
  if (end < 0 && greeting.length < 64) return;
  if (end !== 2 || greeting.subarray(0, end).toString() !== 'OK') {
    socket.destroy(new Error('Local MCP authentication failed')); return;
  }
  clearTimeout(deadline); socket.off('data', authenticate);
  if (greeting.length > end + 1) responses.write(greeting.subarray(end + 1).toString('utf8'));
  socket.setEncoding('utf8'); process.stdin.setEncoding('utf8');
  socket.pipe(responses).pipe(process.stdout); process.stdin.pipe(requests).pipe(socket);
}
socket.on('data', authenticate);
socket.on('error', () => { clearTimeout(deadline); console.error('Local MCP is unavailable. Check its Mac bridge.'); process.exit(1); });
socket.on('close', () => { clearTimeout(deadline); process.exit(0); });
for (const stream of [requests,responses]) stream.on('error', () => socket.destroy(new Error('MCP schema or transport error')));

});
}
