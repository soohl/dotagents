import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

export function runBrowser({ config, validate, filter }) {
  const backend = spawn(process.execPath, ['/app/node_modules/@playwright/mcp/cli.js',
    '--config', config, '--no-webmcp'], { stdio: ['pipe', 'pipe', 'inherit'] });
  const pending = new Map();
  const send = message => process.stdout.write(JSON.stringify(message) + '\n');
  const fail = (id, message) => send({ jsonrpc: '2.0', id: id ?? null, error: { code: -32602, message } });
  createInterface({ input: process.stdin }).on('line', line => {
    let request;
    try {
      request = JSON.parse(line);
      const { method, id } = request;
      if (method === 'tools/call') {
        try {
          const args = validate(request.params?.name, request.params?.arguments);
          if (id !== undefined) pending.set(id, { method, params: request.params });
          request = { ...request, params: { ...request.params, arguments: args } };
        } catch (error) {
          send({ jsonrpc: '2.0', id, result: { isError: true, content: [{ type: 'text', text: error.message }] } });
          return;
        }
      } else if (!['initialize', 'tools/list', 'ping', 'notifications/initialized', 'notifications/cancelled'].includes(method)) {
        if (id !== undefined) fail(id, 'This MCP method is not available.');
        return;
      } else if (id !== undefined) pending.set(id, { method, params: request.params });
      backend.stdin.write(JSON.stringify(request) + '\n');
    } catch { fail(request?.id, 'Invalid MCP request.'); }
  }).on('close', () => backend.kill('SIGTERM'));
  createInterface({ input: backend.stdout }).on('line', async line => {
    try {
      const response = JSON.parse(line);
      const request = pending.get(response.id);
      pending.delete(response.id);
      if (response.result && request) response.result = await filter(request.method, response.result, request.params);
      send(response);
    } catch (error) { console.error(error.message); backend.kill('SIGTERM'); process.exitCode = 1; }
  });
  backend.stdin.on('error', () => backend.kill('SIGTERM'));
  backend.on('error', () => { console.error('Browser failed to start.'); process.exit(1); });
  backend.on('exit', code => process.exit(process.exitCode || code || 0));
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => backend.kill(signal));
}
