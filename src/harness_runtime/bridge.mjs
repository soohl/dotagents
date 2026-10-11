// The application owns sessions. This bridge keeps only live observations.
import http from 'node:http';
import { randomUUID } from 'node:crypto';

export const inject = ['connection', 'sessionController', 'workspaceController', 'workspaceRegistry', 'agents', 'llm'];

export async function apply(ctx) {
  // Containers have a mounted workspace, not a desktop Documents directory.
  // The registry initializes only a fresh home and preserves existing choices.
  await ctx.workspaceRegistry.initializeDefault(async () => '/workspace');
  createBridge(ctx);
}

export function createBridge(ctx, { port = 3081, upstreamPort = 3080, role = process.env.HARNESS_ROLE || 'agent' } = {}) {
  const browserPort = role === 'chat' ? 3002 : 3003;
  const directHosts = [`localhost:${browserPort}`, `127.0.0.1:${browserPort}`];
  const localHosts = ['localhost:3000', '127.0.0.1:3000', '127.0.0.1:3081', ...directHosts];
  const servicePath = path => path.startsWith(`/${role}/`) ? path.slice(role.length + 1) : path;
  const active = new Set();
  let dashboardSession;
  let creating;
  ctx.on('agent/status', ({ agent, status }) => {
    if (status === 'running') active.add(agent.id); else active.delete(agent.id);
    console.log(`Agent ${agent.id}: ${status}`);
  });
  ctx.on('agent/disposed', ({ agent }) => active.delete(agent.id));
  ctx.on('agent/error', ({ agent, error }) => console.error(`Agent ${agent.id}: ${error.message}`));
  ctx.on('session/event', (session, event) => {
    if (session.id !== dashboardSession) return;
    if (event.type === 'assistant/message') {
      const message = event.data?.message ?? event.data;
      for (const part of message?.content ?? []) {
        if (part.type === 'text') console.log(part.text);
      }
    }
    if (event.type === 'turn/end' && event.data?.reason?.kind === 'error') {
      console.error(`Agent: ${event.data.reason.error?.message ?? 'Request failed'}`);
    }
  });
  const reply = (res, code, body) => {
    res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(body));
  };
  async function command(req, res) {
    if (req.url === '/_dotagents/status' && req.method === 'GET') {
      return reply(res, 200, { ready: true, role, active: active.size, session: dashboardSession });
    }
    if (req.url === '/_dotagents/models' && req.method === 'GET') {
      const providers = ctx.llm.listProviders();
      const models = await Promise.all(providers.map(p => ctx.llm.listModels(p.id)));
      return reply(res, 200, { models: models.flat() });
    }
    if (role !== 'agent' || req.method !== 'POST' || !['/_dotagents/task', '/_dotagents/stop'].includes(req.url)) {
      return reply(res, 404, { error: 'Not found' });
    }
    let body = '';
    for await (const chunk of req) {
      body += chunk;
      if (Buffer.byteLength(body) > 98304) return reply(res, 413, { error: 'Task too large' });
    }
    if (req.url === '/_dotagents/stop') {
      if (dashboardSession) ctx.sessionController.cancel({ sessionId: dashboardSession });
      return reply(res, 200, { accepted: true });
    }
    const { task } = JSON.parse(body);
    if (typeof task !== 'string' || !task.trim() || task.length > 24000) {
      return reply(res, 400, { error: 'Provide a task between 1 and 24000 characters.' });
    }
    if (!dashboardSession) {
      creating ??= (async () => {
        const { workspace } = await ctx.workspaceController.create({ path: '/workspace' });
        const session = await ctx.sessionController.create({ workspaceId: workspace.workspaceId, agentPreset: 'agent' });
        dashboardSession = session.sessionId;
        await ctx.sessionController.rename({ sessionId: dashboardSession, title: 'Dashboard' });
      })().finally(() => { creating = undefined; });
      await creating;
    }
    await ctx.sessionController.prompt({ sessionId: dashboardSession, requestId: randomUUID(),
      mode: 'queue', content: [{ type: 'text', text: task }] }, AbortSignal.timeout(10000));
    return reply(res, 200, { accepted: true, session: dashboardSession });
  }
  function forward(req, res, path = req.url, retry = true) {
    const upstream = http.request({ host: '127.0.0.1', port: upstreamPort, method: req.method,
      path, headers: req.headers }, response => {
      // Local access or the passkey proxy admits the browser first. Exchange
      // the upstream launch token internally; never print it or send it in a URL.
      if (retry && req.method === 'GET' && req.url === '/' && response.statusCode === 401) {
        response.resume();
        const url = new URL(ctx.connection.authenticatedUrl(`http://${req.headers.host}/`));
        return forward(req, res, url.pathname + url.search, false);
      }
      res.writeHead(response.statusCode, response.headers);
      response.pipe(res);
    });
    upstream.on('error', () => { if (!res.headersSent) reply(res, 502, { error: 'Harness unavailable' }); else res.destroy(); });
    req.on('aborted', () => upstream.destroy());
    if (req.readableEnded) upstream.end(); else req.pipe(upstream);
  }
  const server = http.createServer((req, res) => {
    const host = req.headers.host ?? '';
    const local = localHosts.includes(host);
    const external = process.env.HARNESS_PUBLIC_HOST;
    if (!local && (!external || host !== external)) return reply(res, 403, { error: 'Invalid host' });
    if (req.headers.origin && ![ `http://${host}`, `https://${host}` ].includes(req.headers.origin)) {
      return reply(res, 403, { error: 'Invalid origin' });
    }
    if (req.headers['sec-fetch-site'] === 'cross-site') return reply(res, 403, { error: 'Cross-site request' });
    if (directHosts.includes(host)) {
      if (req.url === '/' || req.url === `/${role}`) {
        res.writeHead(308, { Location: `/${role}/` }); res.end(); return;
      }
      req.url = servicePath(req.url);
    }
    if (req.url.startsWith('/_dotagents/')) {
      if (!local) return reply(res, 403, { error: 'Local management only' });
      command(req, res).catch(error => reply(res, 400, { error: error.message }));
    } else forward(req, res);
  });
  // DSH currently uses HTTP streaming. Preserve WebSocket upgrades too.
  server.on('upgrade', (req, socket, head) => {
    const host = req.headers.host ?? '';
    if (![...localHosts, process.env.HARNESS_PUBLIC_HOST].filter(Boolean).includes(host)
        || (req.headers.origin && ![`http://${host}`, `https://${host}`].includes(req.headers.origin))) {
      socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return;
    }
    if (directHosts.includes(host)) req.url = servicePath(req.url);
    const upstream = http.request({ host: '127.0.0.1', port: upstreamPort, path: req.url, headers: req.headers });
    upstream.on('upgrade', (response, remote, remoteHead) => {
      socket.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(response.headers).map(([k,v]) => `${k}: ${v}`).join('\r\n')}\r\n\r\n`);
      if (remoteHead.length) socket.write(remoteHead);
      if (head.length) remote.write(head);
      socket.pipe(remote).pipe(socket);
      socket.on('error', () => remote.destroy());
      remote.on('error', () => socket.destroy());
    });
    upstream.on('response', response => { socket.end(`HTTP/1.1 ${response.statusCode} Rejected\r\n\r\n`); response.resume(); });
    upstream.on('error', () => socket.destroy());
    upstream.end();
  });
  server.requestTimeout = 30000;
  server.headersTimeout = 10000;
  server.listen(port, '0.0.0.0');
  ctx.on('dispose', () => { server.closeAllConnections(); server.close(); });
  return server;
}
