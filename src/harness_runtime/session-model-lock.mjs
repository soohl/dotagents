// Fold existing session events. The application log owns this policy's state.
export function lockModelProjection(previous, next, event) {
  const locked = previous.locked === true || event.type === 'request/header'
    || event.type === 'user/message'
    || (event.type === 'agent/inbox/spliced' && event.data.inserted?.length > 0);
  return next.locked === locked ? next : { ...next, locked };
}

export function assertSessionModel(current, selected, locked, RemoteError) {
  if (locked && (current.provider !== selected.provider || current.model !== selected.model)) {
    throw new RemoteError('session/model-locked',
      'This conversation keeps its model. Start a new conversation to use another model.',
      { provider: current.provider, model: current.model });
  }
}
