// Use Harness's native web_search tool with the existing Brave credential.
export const inject = ['web'];

export async function searchBrave(request, signal, apiKey = process.env.BRAVE_SEARCH_API_KEY, fetcher = fetch) {
  if (!apiKey) throw new Error('Brave Search is not configured.');
  if (typeof request.query !== 'string' || !request.query.trim() || request.query.length > 600) throw new Error('Provide a search query of 1–600 characters.');
  const limit = Math.max(1, Math.min(10, request.maxResults ?? 5));
  const url = new URL('https://api.search.brave.com/res/v1/web/search');
  url.searchParams.set('q', request.query);
  url.searchParams.set('count', String(limit));
  const timeout = AbortSignal.timeout(20000);
  const response = await fetcher(url, {redirect:'error', headers:{Accept:'application/json','X-Subscription-Token':apiKey},
    signal:signal ? AbortSignal.any([signal,timeout]) : timeout});
  if (!response.ok) throw new Error(`Brave Search returned HTTP ${response.status}.`);
  const body = await response.json();
  const results = body.web?.results ?? [];
  return {sources:results.slice(0,limit).map(row=>({url:row.url,title:row.title,snippet:row.description})),
    truncated:results.length > limit};
}

export function apply(ctx) {
  ctx.web.registerSearchProvider({id:'dotagents-brave',available:()=>Boolean(process.env.BRAVE_SEARCH_API_KEY),search:searchBrave});
}
