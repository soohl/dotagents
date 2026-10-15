import test from 'node:test';
import assert from 'node:assert/strict';
import { currentTime } from '../src/harness_runtime/local-clock.mjs';

test('the local clock defaults to UTC and returns a fresh system timestamp', () => {
  const before = Date.now();
  const result = currentTime();
  assert.ok(Date.parse(result.utc) >= before && Date.parse(result.utc) <= Date.now());
  assert.equal(result.time_zone, 'UTC');
  assert.equal(result.local, result.utc.slice(0, 19) + '+00:00');
});

test('local dates, daylight saving transitions, and fractional offsets are correct', () => {
  for (const [utc, zone, local] of [
    ['2026-01-01T02:00:00Z', 'America/New_York', '2025-12-31T21:00:00-05:00'],
    ['2026-03-08T06:59:59Z', 'America/New_York', '2026-03-08T01:59:59-05:00'],
    ['2026-03-08T07:00:00Z', 'America/New_York', '2026-03-08T03:00:00-04:00'],
    ['2026-11-01T06:00:00Z', 'America/New_York', '2026-11-01T01:00:00-05:00'],
    ['2026-01-01T00:00:00Z', 'Asia/Kathmandu', '2026-01-01T05:45:00+05:45'],
  ]) {
    const result = currentTime({ time_zone: zone }, new Date(utc));
    assert.equal(result.local, local);
    assert.equal(Date.parse(result.local), Date.parse(utc));
  }
});

test('invalid time zones fail rather than silently using a different zone', () => {
  for (const time_zone of ['', null, 42, 'Mars/Olympus']) {
    assert.throws(() => currentTime({ time_zone }), /time zone/i);
  }
});
