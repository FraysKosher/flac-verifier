import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { decodeFlac32 } from '../flac32.js';

const fixture = new URL('../.fixtures/flac32.flac', import.meta.url);
const load = () => { const b = readFileSync(fixture); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); };

test('decodes 32-bit FLAC to int32', { skip: !existsSync(fixture) }, async () => {
  const r = await decodeFlac32(load());
  assert.equal(r.channels, 2);
  assert.equal(r.sampleRate, 44100);
  assert.ok(r.interleaved instanceof Int32Array);
  assert.equal(r.interleaved.length, 5292000);
});

test('rejects truncated input', { skip: !existsSync(fixture) }, async () => {
  await assert.rejects(decodeFlac32(load().slice(0, 5000)), /FLAC decoding failed/);
});
