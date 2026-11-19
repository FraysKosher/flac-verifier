import test from 'node:test';
import assert from 'node:assert/strict';
import { createScheduler } from '../scheduler.js';
import { openCache, memoryBackend } from '../cache.js';

const verdict = (v, ruta) => JSON.stringify({ ruta, veredicto: v });

// Fake engine: `script` maps vpath -> {json, deferred, error, crash}.
function makeEnv(script, { coherence } = {}) {
  const env = { workers: [], analyzed: [], coherenceInputs: [], factoryCalls: 0 };
  env.factory = () => {
    env.factoryCalls++;
    const w = {
      terminated: false,
      terminate() { this.terminated = true; },
      postMessage(m) {
        if (this.terminated) return;
        const reply = (r) => setTimeout(() => !this.terminated && this.onmessage({ data: r }), 1);
        if (m.type === 'init') return reply({ type: 'ready', engineVersion: 'v1' });
        if (m.type === 'analyze') {
          env.analyzed.push(m.vpath);
          const s = script[m.vpath];
          if (s.crash) { setTimeout(() => this.onerror({ message: 'oom' }), 1); return; }
          if (s.error) return reply({ type: 'error', id: m.id, message: s.error });
          return reply({ type: 'result', id: m.id, json: s.json, png: null });
        }
        if (m.fn === 'sort_names') {
          return reply({ type: 'return', id: m.id, value: JSON.stringify(JSON.parse(m.args[0]).sort()) });
        }
        if (m.fn === 'disposition') {
          const r = JSON.parse(m.args[0]);
          return reply({ type: 'return', id: m.id, value: script[r.ruta].deferred ? 'deferred' : 'immediate' });
        }
        if (m.fn === 'album_coherence') {
          env.coherenceInputs.push(JSON.parse(m.args[0]));
          const out = (coherence || ((x) => x))(JSON.parse(m.args[0]));
          return reply({ type: 'return', id: m.id, value: JSON.stringify(out) });
        }
      },
    };
    env.workers.push(w);
    return w;
  };
  return env;
}

const album = (key, names) => ({
  key, label: key,
  tracks: names.map((n) => ({
    file: { size: 1, lastModified: 1, name: n }, relPath: `${key}/${n}`, vpath: `/albums/${key}/${n}`,
  })),
});
const V = (key, n) => `/albums/${key}/${n}`;

function setup(script, opts = {}) {
  const env = makeEnv(script, opts);
  const events = [];
  const cache = openCache(memoryBackend());
  const sched = createScheduler({
    workerFactory: env.factory, poolSize: opts.poolSize ?? 2, cache, base: '/',
    onEvent: (e) => events.push(e),
  });
  return { env, events, cache, sched };
}
const done = (events) => new Promise((resolve) => {
  const t = setInterval(() => {
    if (events.some((e) => e.type === 'done' || e.type === 'fatal')) { clearInterval(t); resolve(); }
  }, 2);
});
const opts = { mode: 'full', seconds: 15 };

test('immediate results stream before album completes', async () => {
  const script = {
    [V('A', 'a.flac')]: { json: verdict('ok', V('A', 'a.flac')) },
    [V('A', 'b.flac')]: { json: verdict('ok', V('A', 'b.flac')), deferred: true },
  };
  const { events, sched } = setup(script);
  sched.run([album('A', ['b.flac', 'a.flac'])], opts);
  await done(events);
  const types = events.map((e) => e.type);
  assert.ok(types.indexOf('track') < types.indexOf('album-done'));
  assert.equal(events[0].total, 2);
  assert.equal(events.find((e) => e.type === 'track').relPath, 'A/a.flac');
});

test('deferred results released after coherence with coherence output', async () => {
  const p = V('A', 'b.flac');
  const script = {
    [V('A', 'a.flac')]: { json: verdict('ok', V('A', 'a.flac')) },
    [p]: { json: verdict('ok', p), deferred: true },
  };
  const flip = (list) => list.map((r) => (r.ruta === p ? { ...r, veredicto: 'flipped' } : r));
  const { events, sched } = setup(script, { coherence: flip });
  sched.run([album('A', ['a.flac', 'b.flac'])], opts);
  await done(events);
  const held = events.find((e) => e.type === 'track' && e.relPath === 'A/b.flac');
  assert.equal(held.result.veredicto, 'flipped');
  const types = events.map((e) => e.type);
  assert.ok(types.indexOf('album-done') > events.indexOf(held));
});

test('cache stores pre-coherence result', async () => {
  const p = V('A', 'b.flac');
  const script = { [p]: { json: verdict('ok', p), deferred: true } };
  const { events, sched, cache } = setup(script, {
    coherence: (l) => l.map((r) => ({ ...r, veredicto: 'flipped' })),
  });
  sched.run([album('A', ['b.flac'])], opts);
  await done(events);
  const { cacheKey } = await import('../cache.js');
  const key = cacheKey({ relPath: 'A/b.flac', size: 1, lastModified: 1, mode: 'full', seconds: 15, engineVersion: 'v1' });
  assert.equal(JSON.parse((await cache.get(key)).json).veredicto, 'ok');
});

test('cache hit skips analyze; ignoreCache forces it', async () => {
  const p = V('A', 'a.flac');
  const script = { [p]: { json: verdict('ok', p) } };
  const { env, events, sched } = setup(script);
  const albums = [album('A', ['a.flac'])];
  sched.run(albums, opts); await done(events);
  assert.equal(env.analyzed.length, 1);
  events.length = 0;
  sched.run(albums, opts); await done(events);
  assert.equal(env.analyzed.length, 1);
  assert.equal(events.find((e) => e.type === 'track').fromCache, true);
  events.length = 0;
  sched.run(albums, { ...opts, ignoreCache: true }); await done(events);
  assert.equal(env.analyzed.length, 2);
});

test('error result does not block album', async () => {
  const [a, b, c] = ['a', 'b', 'c'].map((n) => V('A', `${n}.flac`));
  const script = {
    [a]: { json: verdict('ok', a), deferred: true },
    [b]: { error: 'bad file' },
    [c]: { json: verdict('ok', c), deferred: true },
  };
  const { env, events, sched } = setup(script);
  sched.run([album('A', ['a.flac', 'b.flac', 'c.flac'])], opts);
  await done(events);
  const tracks = events.filter((e) => e.type === 'track');
  assert.equal(tracks.length, 3);
  assert.equal(tracks.find((e) => e.relPath === 'A/b.flac').result.error, 'bad file');
  assert.ok(events.some((e) => e.type === 'album-done'));
  assert.deepEqual(env.coherenceInputs[0].map((r) => r.ruta).sort(), [a, c]);
});

test('worker crash shrinks pool to 1 and finishes', async () => {
  const names = ['a', 'b', 'c', 'd'];
  const script = {};
  for (const n of names) script[V('A', `${n}.flac`)] = { json: verdict('ok', V('A', `${n}.flac`)) };
  script[V('A', 'b.flac')] = { crash: true };
  const { env, events, sched } = setup(script);
  sched.run([album('A', names.map((n) => `${n}.flac`))], opts);
  await done(events);
  const tracks = events.filter((e) => e.type === 'track');
  assert.equal(tracks.length, 4);
  assert.equal(tracks.find((e) => e.relPath === 'A/b.flac').result.error, 'oom');
  assert.ok(events.some((e) => e.type === 'album-done'));
  assert.ok(env.workers.filter((w) => !w.terminated).length <= 1);
});

test('stop then run emits nothing from old run', async () => {
  const p = V('A', 'a.flac');
  const script = { [p]: { json: verdict('ok', p) } };
  const { events, sched } = setup(script);
  sched.run([album('A', ['a.flac'])], opts);
  await new Promise((r) => setTimeout(r, 3));
  sched.stop();
  events.length = 0;
  sched.run([album('B', ['a.flac'])], opts);
  script[V('B', 'a.flac')] = { json: verdict('ok', V('B', 'a.flac')) };
  await done(events);
  assert.ok(events.every((e) => e.album === undefined || e.album === 'B'));
  assert.equal(events.filter((e) => e.type === 'start').length, 1);
});

test('init failure reports fatal', async () => {
  const events = [];
  const sched = createScheduler({
    cache: openCache(memoryBackend()), poolSize: 1, onEvent: (e) => events.push(e),
    workerFactory: () => ({
      terminate() {},
      postMessage() { setTimeout(() => this.onmessage({ data: { type: 'fatal', message: 'engine down' } }), 1); },
    }),
  });
  sched.run([album('A', ['a.flac'])], opts);
  await done(events);
  assert.equal(events.at(-1).type, 'fatal');
  assert.equal(events.at(-1).message, 'engine down');
});
