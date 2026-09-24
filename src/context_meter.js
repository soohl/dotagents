// Open WebUI v0.11.4 loads this file from /static/loader.js.
// Its chat API reports estimated context usage after each saved turn.
(() => {
  const windowTokens = 65536;
  const labelId = 'cook-studio-context-meter';
  let chatId = null;
  let messageContainer = null;
  let messageObserver = null;
  let refreshTimer = null;
  let request = null;
  let usage = null;

  const selectedChat = () => location.pathname.match(/^\/c\/([^/]+)\/?$/)?.[1] ?? null;
  const format = (value) => value.toLocaleString();

  function show() {
    const input = document.getElementById('message-input-container');
    if (!input) return;
    let label = document.getElementById(labelId);
    if (!label) {
      label = document.createElement('div');
      label.id = labelId;
      label.setAttribute('role', 'status');
      label.setAttribute('aria-live', 'polite');
      label.style.cssText = 'padding:0 12px 4px;text-align:right;font-size:11px;opacity:.75';
    }
    if (label.nextElementSibling !== input) input.insertAdjacentElement('beforebegin', label);
    if (usage === null) {
      const text = `Context window: ${format(windowTokens)} tokens`;
      if (label.textContent !== text) label.textContent = text;
      label.title = 'Usage appears after the first saved reply.';
    } else {
      const remaining = Math.max(0, windowTokens - usage.tokens);
      const text = `Context: ≈${format(usage.tokens)} / ${format(windowTokens)} · ≈${format(remaining)} left`;
      if (label.textContent !== text) label.textContent = text;
      label.title = `Estimated tokens in this chat. Open WebUI compacts near ${format(usage.threshold)} tokens. Leave room for the next reply.`;
    }
  }

  function schedule(delay = 1500) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, delay);
  }

  async function refresh() {
    const id = selectedChat();
    const token = localStorage.getItem('token');
    if (!id || !token || document.hidden) return;
    request?.abort();
    request = new AbortController();
    try {
      const response = await fetch(`/api/v1/chats/${encodeURIComponent(id)}`, {
        headers: { Authorization: `Bearer ${token}` },
        credentials: 'same-origin',
        signal: request.signal
      });
      if (!response.ok) return;
      const result = (await response.json()).context_usage;
      if (selectedChat() !== id) return;
      const tokens = Number(result?.estimated_tokens ?? result?.tokens);
      const threshold = Number(result?.threshold);
      usage = Number.isFinite(tokens) && tokens >= 0
        ? { tokens: Math.round(tokens), threshold: Number.isFinite(threshold) && threshold > 0 ? threshold : 48000 }
        : null;
      show();
    } catch (error) {
      if (error.name !== 'AbortError') return;
    }
  }

  function sync() {
    const nextChat = selectedChat();
    if (chatId !== nextChat) {
      chatId = nextChat;
      usage = null;
      request?.abort();
      schedule(0);
    }
    show();
    const nextContainer = document.getElementById('messages-container');
    if (nextContainer !== messageContainer) {
      messageObserver?.disconnect();
      messageContainer = nextContainer;
      if (messageContainer) {
        messageObserver = new MutationObserver(() => schedule());
        messageObserver.observe(messageContainer, { childList: true, characterData: true, subtree: true });
      }
    }
  }

  const start = () => {
    sync();
    setInterval(sync, 1000);
    setInterval(() => { if (chatId) schedule(0); }, 30000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) schedule(0); });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
