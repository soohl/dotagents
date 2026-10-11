// Bound model-facing browser text. Chat can page snapshots; Agent can read spills.
export function pageResult(result, { start = 0, snapshot = false, limit = 12000, file } = {}) {
  const text = (result.content ?? []).filter(p => p.type === 'text').map(p => p.text).join('\n');
  const points = Array.from(text);
  if (start === 0 && points.length <= limit) return result;
  if (start >= points.length) return { isError: true, content: [{ type: 'text', text: 'Snapshot offset exceeds the available text.' }] };
  const room = limit - 400;
  const end = Math.min(start + room, points.length);
  const hint = file ? `Full output: ${file}. Read only the sections needed.`
    : end === points.length ? 'End of snapshot.'
    : snapshot ? `Continue with browser_snapshot {"start":${end}}.`
    : 'Read the page with browser_snapshot {"start":0}; request later pages as needed.';
  const { structuredContent, ...rest } = result;
  return { ...rest, content: [{ type: 'text', text: points.slice(start, end).join('')
    + `\n[Showing characters ${start}-${end} of ${points.length}. ${hint}]` },
    ...(result.content ?? []).filter(p => p.type !== 'text')] };
}
