// Exact 32-bit FLAC decoding (libFLAC 1.5.0 compiled to WebAssembly).
// The decoder is fetched on first use only.
export const TOO_LARGE = 'File is too large for the browser. Use the desktop app.';
let modulePromise = null;

function load() {
  if (!modulePromise) {
    modulePromise = import('./vendor/flac32.mjs').then((m) => m.default({
      locateFile: (name) => new URL(`./vendor/${name}`, import.meta.url).href,
    })).catch((err) => { modulePromise = null; throw err; });
  }
  return modulePromise;
}

// Node has no fetch for file: URLs; hand the wasm bytes over directly.
async function loadNode() {
  const { readFile } = await import('node:fs/promises');
  const { fileURLToPath } = await import('node:url');
  const mod = (await import('./vendor/flac32.mjs')).default;
  const wasmBinary = await readFile(fileURLToPath(new URL('./vendor/flac32.wasm', import.meta.url)));
  return mod({ wasmBinary });
}

export async function decodeFlac32(arrayBuffer) {
  const isNode = typeof process !== 'undefined' && process.versions?.node && typeof window === 'undefined' && typeof WorkerGlobalScope === 'undefined';
  if (isNode && !modulePromise) modulePromise = loadNode();
  const m = await (isNode ? modulePromise : load());
  const bytes = new Uint8Array(arrayBuffer);
  const tooLarge = () => new Error(TOO_LARGE);
  const inPtr = m._malloc(bytes.length || 1);
  if (!inPtr) throw tooLarge();
  m.HEAPU8.set(bytes, inPtr);
  const h = m._flac32_decode(inPtr, bytes.length);
  try {
    if (!h || m._flac32_oom(h)) throw tooLarge();
    if (!m._flac32_ok(h)) throw new Error('FLAC decoding failed.');
    const channels = m._flac32_channels(h);
    const frames = m._flac32_frames(h);
    // View into the wasm heap; valid until the next call into the module.
    const view = new Int32Array(m.HEAP32.buffer, m._flac32_data(h), frames * channels);
    return { channels, sampleRate: m._flac32_sample_rate(h), interleaved: view, release };
  } catch (err) {
    release();
    throw err;
  }
  function release() {
    if (h) m._flac32_free(h);
    m._free(inPtr);
  }
}
