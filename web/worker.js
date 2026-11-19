// Module worker: hosts the Pyodide engine. Protocol: see scheduler.js.
import { decodeFlac32, TOO_LARGE } from './flac32.js';
import { loadEngine, PYODIDE_INDEX_URL, ENGINE_FILES } from './runtime.js';

let engine = null;
let currentId = null;

function dirname(p) { return p.slice(0, p.lastIndexOf('/')) || '/'; }

async function init(base) {
  try {
    const root = new URL(base || '.', self.location.href);
    const get = async (url) => {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
      return res;
    };
    engine = await loadEngine({
      indexURL: PYODIDE_INDEX_URL,
      engineFiles: ENGINE_FILES,
      fetchText: async (name) => (await get(new URL(name, root))).text(),
      fetchBytes: async (name) => new Uint8Array(await (await get(new URL(name, root))).arrayBuffer()),
    });
    postMessage({ type: 'ready', engineVersion: engine.engineVersion });
  } catch (err) {
    console.error(err);
    postMessage({
      type: 'fatal',
      message: 'Could not load the analysis engine. Check your connection and reload.',
    });
  }
}

let seq = 0;
const perFile = (message) => Object.assign(new Error(message), { perFile: true });

async function registerS32(file, vpath, raw) {
  const { FS } = engine.py;
  let decoded;
  try {
    decoded = await decodeFlac32(await file.arrayBuffer());
  } catch (err) {
    const message = String(err.message || err);
    throw perFile(message === TOO_LARGE ? message : `Could not decode this 32-bit FLAC file: ${message}`);
  }
  try {
    FS.mkdirTree('/s32');
    FS.writeFile(raw, new Uint8Array(decoded.interleaved.buffer, decoded.interleaved.byteOffset, decoded.interleaved.byteLength));
  } catch {
    throw perFile(TOO_LARGE);
  } finally {
    decoded.release();
  }
  engine.call('set_s32_source', vpath, raw);
}

async function analyze({ id, file, vpath, mode, seconds }) {
  const { FS } = engine.py;
  const dir = dirname(vpath);
  FS.mkdirTree(dir);
  FS.mount(FS.filesystems.WORKERFS, { files: [file] }, dir);
  let raw = null;
  try {
    if (engine.call('needs_s32', vpath)) {
      raw = `/s32/${++seq}.raw`;
      await registerS32(file, vpath, raw);
    }
    const [json, png] = engine.call('analyze', vpath, mode, seconds);
    const out = png ? new Uint8Array(png) : null;
    postMessage({ type: 'result', id, json, png: out }, out ? [out.buffer] : []);
  } finally {
    if (raw) {
      try { engine.call('set_s32_source', vpath, ''); } catch { /* nothing registered */ }
      try { FS.unlink(raw); } catch { /* already gone */ }
    }
    FS.unmount(dir);
  }
}

function callFn({ id, fn, args }) {
  postMessage({ type: 'return', id, value: engine.call(fn, ...args) });
}

onmessage = async (event) => {
  const msg = event.data;
  if (msg.type === 'init') return init(msg.base);
  currentId = msg.id;
  try {
    if (!engine) throw new Error('Engine not ready');
    if (msg.type === 'analyze') await analyze(msg);
    else if (msg.type === 'call') callFn(msg);
  } catch (err) {
    const message = String(err && err.message || err);
    const python = err && (err.perFile || err.constructor?.name === 'PythonError' || err.type);
    if (python || !engine) postMessage({ type: 'error', id: msg.id, message });
    else { postMessage({ type: 'error', id: msg.id, message, crash: true }); self.close(); }
  }
};

onunhandledrejection = (event) => {
  postMessage({ type: 'error', id: currentId, message: String(event.reason && event.reason.message || event.reason), crash: true });
  self.close();
};
