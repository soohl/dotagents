// Read the system clock only when the model calls the tool.
export const inject = ['tools'];

export function currentTime({ time_zone = 'UTC' } = {}, now = new Date()) {
  if (typeof time_zone !== 'string' || !time_zone.trim() || time_zone.length > 100) {
    throw new Error('Provide an IANA time zone, such as America/New_York, or UTC.');
  }
  let formatter;
  try {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: time_zone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
      timeZoneName: 'longOffset',
    });
  } catch {
    throw new Error('Unknown time zone. Use an IANA name, such as America/New_York, or UTC.');
  }
  const parts = Object.fromEntries(formatter.formatToParts(now).map(part => [part.type, part.value]));
  const offset = parts.timeZoneName === 'GMT' ? '+00:00' : parts.timeZoneName.slice(3);
  return {
    utc: now.toISOString(),
    local: `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}:${parts.second}${offset}`,
    time_zone: formatter.resolvedOptions().timeZone,
  };
}

export async function apply(ctx) {
  const { defineTool } = await import('@deepseek-ai/dsh-tools');
  ctx.tools.register(defineTool({
    name: 'current_time',
    description: 'Get the current date and time from the local system clock, without internet access. Call only when current time is needed. Defaults to UTC; specify an IANA time zone for local time.',
    parameters: { time_zone: { type: 'string', description: 'IANA time zone, such as America/New_York. Defaults to UTC.' } },
    output: {
      schema: { type: 'object', additionalProperties: false, properties: {
        utc: { type: 'string', required: true },
        local: { type: 'string', required: true },
        time_zone: { type: 'string', required: true },
      } },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    isConcurrencySafe: () => true,
    execute: args => currentTime(args),
  }));
}
