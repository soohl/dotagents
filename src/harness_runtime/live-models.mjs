// Managed model choices come from approved APIs, never from saved model lists.
import { readFile } from 'node:fs/promises';

const label = value => typeof value === 'string' && value.length > 0 && value.length <= 256 && !/[\x00-\x1f]/.test(value);
const capacity = value => Number.isSafeInteger(value) && value > 0 && value <= 2 ** 30;
// Saved sessions retain their provider IDs and model locks across project renames.
const managed = id => id.startsWith('dotagents-') || id.startsWith('sealedllm-');
const compat = { supportsStore: false, supportsDeveloperRole: false,
  supportsReasoningEffort: true, supportsStrictMode: false, maxTokensField: 'max_tokens' };

export async function readCatalog(endpoint, fetcher = fetch) {
  const response = await fetcher(endpoint.baseURL + '/models', {
    signal: AbortSignal.timeout(3000), redirect: 'error', headers: { Accept: 'application/json' },
  });
  if (!response.ok) throw new Error('Model catalog unavailable');
  let text = '', size = 0;
  const decoder = new TextDecoder();
  for await (const chunk of response.body) {
    size += chunk.byteLength;
    if (size > 1024 * 1024) throw new Error('Model catalog too large');
    text += decoder.decode(chunk, { stream: true });
  }
  const body = JSON.parse(text + decoder.decode());
  if (!Array.isArray(body.data) || body.data.length > 256) throw new Error('Invalid model catalog');
  const node = ['dotagents-node', 'sealedllm-node'].includes(endpoint.protocol);
  const field = endpoint.protocol === 'sealedllm-node' ? 'sealedllm' : 'dotagents';
  if (node && (body[field]?.protocol !== endpoint.protocol || body[field]?.schema_version !== 1)) {
    throw new Error('Invalid node catalog');
  }
  const models = new Map();
  for (const item of body.data) {
    if (!label(item?.id)) throw new Error('Invalid model ID');
    if (endpoint.model && item.id !== endpoint.model) continue;
    const meta = node ? item[field] : item;
    if (node && (!meta || !['Chat', 'Image'].includes(meta.service))) throw new Error('Invalid model capability');
    if (node && (meta.service !== 'Chat' || meta.installed !== true)) continue;
    const context = meta.context_window ?? meta.contextWindow ?? endpoint.contextWindow;
    const vision = item.info?.meta?.capabilities?.vision === true;
    models.set(item.id, { id: item.id, name: label(meta.name) ? meta.name : item.id,
      contextWindow: capacity(context) ? context : 131072,
      maxTokens: Math.min(capacity(endpoint.maxTokens) ? endpoint.maxTokens : 32768, capacity(context) ? context : 131072),
      input: vision ? ['text', 'image'] : ['text'], reasoningEfforts: { high: 'high', xhigh: 'xhigh' } });
  }
  return [...models.values()];
}

export class LiveModels {
  constructor({ readEndpoints = async () => JSON.parse(await readFile('/run/dotagents/endpoints.json', 'utf8')),
    fetcher = fetch } = {}) {
    this.readEndpoints = readEndpoints;
    this.fetcher = fetcher;
    this.catalogs = [];
    this.version = 0;
  }
  providers(raw = {}) {
    if (this.raw === raw && this.cachedVersion === this.version) return this.cached;
    const result = Object.fromEntries(Object.entries(raw).filter(([id]) => !managed(id)));
    for (const { endpoint, models } of this.catalogs) {
      if (!models.length) continue;
      // Preserve existing session routes when an endpoint's configuration ID changes.
      const existing = Object.entries(raw).filter(([id, config]) => managed(id)
        && (endpoint.model
          ? id.replace(/^sealedllm-/, 'dotagents-') === endpoint.provider
          : config.baseURL?.replace(/\/$/, '') === endpoint.baseURL));
      const routes = existing.length ? existing : [[endpoint.provider, {}]];
      for (const [id, source] of routes) {
        const { modelOverrides, ...settings } = source;
        result[id] = { api: 'openai-completions', compat, apiKeyEnv: 'DOTAGENTS_LOCAL_PLACEHOLDER',
          retryPolicy: { mode: 'normal', maxRetries: 1 }, ...settings, baseURL: endpoint.baseURL, models };
      }
    }
    this.raw = raw;
    this.cachedVersion = this.version;
    return this.cached = result;
  }
  async refresh() {
    let endpoints;
    try {
      endpoints = await this.readEndpoints();
      if (!Array.isArray(endpoints) || endpoints.length > 32) throw new Error('Invalid endpoints');
    } catch { endpoints = []; }
    const catalogs = await Promise.all(endpoints.map(async endpoint => {
      try { return { endpoint, models: await readCatalog(endpoint, this.fetcher) }; }
      catch { return { endpoint, models: [] }; }
    }));
    const signature = JSON.stringify(catalogs);
    if (signature === this.signature) return false;
    this.signature = signature;
    this.catalogs = catalogs;
    this.version++;
    return true;
  }
  start(onChange) {
    let stopped = false, timer;
    const tick = async () => {
      try { if (await this.refresh() && !stopped) onChange(); }
      catch (error) { console.error('Model catalog refresh failed:', error.message); }
      if (!stopped) timer = setTimeout(tick, 5000);
    };
    void tick();
    return () => { stopped = true; clearTimeout(timer); };
  }
}
