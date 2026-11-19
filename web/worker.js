// Module worker: hosts the Pyodide engine. Protocol: see scheduler.js.
import { loadEngine, PYODIDE_INDEX_URL, ENGINE_FILES } from './runtime.js';

let engine = null;

function dirname(p) { return p.slice(0, p.lastIndexOf('/')) || '/'; }

async function init(base) {
  try {
    engine = await loadEngine({
      indexURL: PYODIDE_INDEX_URL,
      engineFiles: ENGINE_FILES,
      fetchText: async (name) => {
        const res = await fetch(new URL(name, base));
        if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
        return res.text();
      },
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

function analyze({ id, file, vpath, mode, seconds }) {
  const { FS } = engine.py;
  const dir = dirname(vpath);
  FS.mkdirTree(dir);
  FS.mount(FS.filesystems.WORKERFS, { files: [file] }, dir);
  try {
    const [json, png] = engine.call('analyze', vpath, mode, seconds);
    const out = png ? new Uint8Array(png) : null;
    postMessage({ type: 'result', id, json, png: out }, out ? [out.buffer] : []);
  } finally {
    FS.unmount(dir);
  }
}

function callFn({ id, fn, args }) {
  postMessage({ type: 'return', id, value: engine.call(fn, ...args) });
}

onmessage = async (event) => {
  const msg = event.data;
  if (msg.type === 'init') return init(msg.base);
  try {
    if (!engine) throw new Error('Engine not ready');
    if (msg.type === 'analyze') analyze(msg);
    else if (msg.type === 'call') callFn(msg);
  } catch (err) {
    postMessage({ type: 'error', id: msg.id, message: String(err && err.message || err) });
  }
};

onunhandledrejection = (event) => {
  postMessage({ type: 'error', id: null, message: String(event.reason && event.reason.message || event.reason) });
};
