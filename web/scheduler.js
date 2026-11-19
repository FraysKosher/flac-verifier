// Album-aware scheduler. Mirrors analyze_files.in_order() (motor_flac.py):
// albums in sorted order, tracks of one album fan out over the worker pool,
// immediate results stream, deferred ones wait for album coherence.
import { cacheKey } from './cache.js';

const basename = (p) => p.slice(p.lastIndexOf('/') + 1);

export function createScheduler({ workerFactory, poolSize = 2, cache, onEvent, base }) {
  let current = null;

  function stop() {
    if (!current) return;
    current.alive = false;
    for (const w of current.workers) w.terminate();
    current.workers.length = 0;
    current = null;
  }

  function makeRun() {
    const run = {
      alive: true, limit: poolSize, workers: [], waiters: [], nextId: 1,
      engineVersion: null, everReady: false,
    };
    run.emit = (event) => { if (run.alive) onEvent(event); };

    function drop(w) {
      const i = run.workers.indexOf(w);
      if (i >= 0) run.workers.splice(i, 1);
      w.terminate();
    }
    function wake() { const next = run.waiters.shift(); if (next) next(); }

    function spawn() {
      const w = workerFactory();
      w.busy = true;
      w.pending = new Map();
      w.ready = new Promise((resolve, reject) => { w.onReady = { resolve, reject }; });
      w.onmessage = (e) => {
        if (!run.alive) return;
        const m = e.data;
        if (m.type === 'ready') { run.engineVersion = m.engineVersion; w.onReady.resolve(); return; }
        if (m.type === 'fatal') { w.onReady.reject(new Error(m.message)); return; }
        const p = w.pending.get(m.id);
        if (!p) return;
        w.pending.delete(m.id);
        if (m.type === 'error') p.reject(new Error(m.message));
        else p.resolve(m);
      };
      w.onerror = (e) => {
        if (!run.alive) return;
        const err = new Error((e && e.message) || 'Analysis worker crashed');
        run.limit = 1;
        drop(w);
        w.onReady.reject(err);
        for (const p of w.pending.values()) p.reject(err);
        w.pending.clear();
        wake();
      };
      run.workers.push(w);
      w.postMessage({ type: 'init', base });
      return w;
    }

    async function acquire() {
      for (;;) {
        if (!run.alive) throw new Error('stopped');
        const idle = run.workers.find((w) => !w.busy);
        if (idle) { idle.busy = true; return idle; }
        if (run.workers.length < run.limit) {
          const w = spawn();
          try {
            await w.ready;
          } catch (err) {
            // Init failure or crash: drop this worker; fatal only if none can start.
            drop(w);
            if (!run.everReady && run.workers.length === 0) { err.fatal = true; throw err; }
            run.limit = Math.max(1, Math.min(run.limit, run.workers.length));
            wake();
            continue;
          }
          run.everReady = true;
          return w;
        }
        await new Promise((resolve) => run.waiters.push(resolve));
      }
    }

    function release(w) {
      if (!run.workers.includes(w)) return;
      if (run.workers.length > run.limit) { drop(w); return; }
      w.busy = false;
      wake();
    }

    // Send one request on a pooled worker; a crash rejects the promise.
    async function request(msg, transfer = []) {
      const w = await acquire();
      try {
        const id = run.nextId++;
        const reply = new Promise((resolve, reject) => w.pending.set(id, { resolve, reject }));
        w.postMessage({ ...msg, id }, transfer);
        return await reply;
      } finally {
        release(w);
      }
    }

    run.call = async (fn, ...args) => (await request({ type: 'call', fn, args })).value;
    run.analyze = (file, vpath, mode, seconds) =>
      request({ type: 'analyze', file, vpath, mode, seconds });
    return run;
  }

  async function sorted(run, names) {
    return JSON.parse(await run.call('sort_names', JSON.stringify(names)));
  }

  async function analyzeTrack(run, track, opts) {
    const key = cacheKey({
      relPath: track.relPath, size: track.file.size, lastModified: track.file.lastModified,
      mode: opts.mode, seconds: opts.seconds, engineVersion: run.engineVersion,
    });
    try {
      // Cache failures are never allowed to turn a good analysis into an error.
      let entry = null;
      if (!opts.ignoreCache) {
        try { entry = await cache.get(key); } catch { entry = null; }
      }
      const fromCache = !!entry;
      if (!entry) {
        const r = await run.analyze(track.file, track.vpath, opts.mode, opts.seconds ?? null);
        entry = { json: r.json, png: r.png ?? null };
        try { await cache.put(key, entry); } catch { /* ignore */ }
      }
      const kind = await run.call('disposition', entry.json);
      return { track, json: entry.json, png: entry.png, fromCache, kind };
    } catch (err) {
      if (err.fatal) throw err;
      return {
        track, json: null, png: null, fromCache: false, kind: 'immediate',
        error: { ruta: track.vpath, error: err.message },
      };
    }
  }

  async function processAlbum(run, album, opts) {
    const order = await sorted(run, album.tracks.map((t) => basename(t.vpath)));
    const byName = new Map(album.tracks.map((t) => [basename(t.vpath), t]));
    const held = [];
    const ok = [];
    const emitTrack = (r, result, extra = {}) => run.emit({
      type: 'track', album: album.key, relPath: r.track.relPath,
      result, png: r.png, fromCache: r.fromCache, ...extra,
    });
    await Promise.all(order.map(async (name) => {
      const r = await analyzeTrack(run, byName.get(name), opts);
      if (r.error) { emitTrack(r, r.error); return; }
      ok.push(r);
      if (r.kind === 'immediate') emitTrack(r, JSON.parse(r.json));
      else held.push(r);
    }));
    if (held.length) {
      const results = ok.map((r) => JSON.parse(r.json));
      const coherent = new Map();
      let failed = true;
      for (let attempt = 0; attempt < 2 && failed; attempt++) {
        try {
          const out = JSON.parse(await run.call('album_coherence', JSON.stringify(results)));
          for (const res of out) coherent.set(res.ruta, res);
          failed = false;
        } catch (err) {
          if (err.fatal) throw err;
        }
      }
      held.sort((a, b) => order.indexOf(basename(a.track.vpath)) - order.indexOf(basename(b.track.vpath)));
      for (const r of held) {
        emitTrack(r, coherent.get(r.track.vpath) ?? JSON.parse(r.json), failed ? { coherence_failed: true } : {});
      }
      if (failed) {
        run.emit({
          type: 'album-warning', album: album.key,
          message: 'Album consistency check failed. Re-analyze this album.',
        });
      }
    }
    run.emit({ type: 'album-done', album: album.key });
  }

  async function runAll(albums, opts) {
    stop();
    const run = makeRun();
    current = run;
    try {
      run.emit({ type: 'start', total: albums.reduce((n, a) => n + a.tracks.length, 0) });
      const keys = await sorted(run, albums.map((a) => a.key));
      const byKey = new Map(albums.map((a) => [a.key, a]));
      for (const key of keys) await processAlbum(run, byKey.get(key), opts);
      run.emit({ type: 'done' });
    } catch (err) {
      if (run.alive) {
        run.emit({ type: 'fatal', message: err.message });
        stop();
      }
    }
  }

  return { run: runAll, stop };
}
