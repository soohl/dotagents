import test from 'node:test';
import assert from 'node:assert/strict';
import { lockModelProjection, assertSessionModel } from '../src/harness_runtime/session-model-lock.mjs';

const pair = { provider: 'node', model: 'first' };
class RemoteError extends Error { constructor(code, message) { super(message); this.code = code; } }

test('new conversations can change models; admitted prompts lock even before inference', () => {
  let state = { locked: false };
  assertSessionModel(pair, { ...pair, model: 'second' }, state.locked, RemoteError);
  state = lockModelProjection(state, {}, { type: 'agent/inbox/spliced', data: { inserted: [{}] } });
  assert.equal(state.locked, true);
  assert.throws(() => assertSessionModel(pair, { ...pair, model: 'second' }, state.locked, RemoteError),
    error => error.code === 'session/model-locked');
  assert.throws(() => assertSessionModel(pair, { ...pair, provider: 'other' }, state.locked, RemoteError));
  assert.doesNotThrow(() => assertSessionModel(pair, { ...pair, reasoningEffort: 'high' }, true, RemoteError));
});

test('replay restores locks for old sessions, including failures, and clearing the inbox does not unlock', () => {
  for (const type of ['user/message', 'request/header']) {
    let state = lockModelProjection({}, {}, { type, data: {} });
    state = lockModelProjection(state, {}, { type: 'agent/inbox/spliced', data: { inserted: [] } });
    state = lockModelProjection(state, {}, { type: 'model/selection', data: pair });
    assert.equal(state.locked, true);
  }
  assert.equal(lockModelProjection({}, {}, { type: 'model/selection', data: pair }).locked, false);
});
