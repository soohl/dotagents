// Each process owns one preset and one independent Harness home.
export function serviceProfile(rows, role, thingsEnabled = false, calendarEnabled = false) {
  if (!['chat', 'agent'].includes(role)) throw new Error('HARNESS_ROLE must be chat or agent.');
  const visit = items => items.filter(row => !['preset-chat', 'preset-agent'].includes(row.id)
    || row.id === 'preset-' + role).map(row => {
      if (row.insert) row.insert = visit(row.insert);
      if (row.id === 'agent-preset-registry') row.config = { ...row.config, default: role };
      if (row.id === 'preset-' + role && Array.isArray(row.config?.plugins)) {
        row.config.plugins = row.config.plugins.filter(p => !['mcp-things','mcp-calendar'].includes(p.id));
        if (role === 'chat' && thingsEnabled) row.config.plugins.push({
          id: 'mcp-things', name: '@deepseek-ai/dsh-mcp-client', config: {
            serverName: 'things', transport: 'stdio', command: 'node',
            args: ['/app/runtime/things-transport.mjs'], failOnStartupError: false,
          },
        });
        if (role === 'chat' && calendarEnabled) row.config.plugins.push({
          id: 'mcp-calendar', name: '@deepseek-ai/dsh-mcp-client', config: {
            serverName: 'calendar', transport: 'stdio', command: 'node',
            args: ['/app/runtime/calendar-transport.mjs'], failOnStartupError: false,
          },
        });
      }
      return row;
    });
  const configured = visit(rows).filter(row => row.id !== 'dotagents-time-context'
    && !(row.insert?.length === 1 && row.insert[0].id === 'dotagents-time-context'));
  // Profile patches add plugins through insert; a root row only updates an existing plugin.
  if (role === 'chat') configured.push({ insert: [{
    id: 'dotagents-time-context', name: '@deepseek-ai/dsh-time-context',
    config: { refreshIntervalMs: 0, timeZone: 'UTC' },
  }] });
  return configured;
}
