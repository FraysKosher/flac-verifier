// Shared Pyodide loader. Runs in a Worker and in Node; no Node APIs here.
// The caller supplies fetchText(name) -> Promise<string> for engine files.

export const PYODIDE_INDEX_URL = 'https://cdn.jsdelivr.net/pyodide/v314.0.8/full/';

export const ENGINE_FILES = [
  'motor_flac.py', '_motor_bits.py', '_motor_espectral.py',
  '_motor_veredicto.py', '_motor_cache.py', 'engine_host.py', 'wasm_compat.py',
];

const PACKAGES = ['numpy', 'scipy', 'pillow', 'soundfile', 'micropip'];

// loadPyodide may be injected (Node passes the npm module's); otherwise it is
// imported from `${indexURL}pyodide.mjs`.
export async function loadEngine({
  indexURL = PYODIDE_INDEX_URL,
  engineFiles = ENGINE_FILES,
  fetchText,
  loadPyodide,
} = {}) {
  if (!fetchText) throw new Error('loadEngine: fetchText is required');
  if (!loadPyodide) {
    ({ loadPyodide } = await import(/* @vite-ignore */ `${indexURL}pyodide.mjs`));
  }
  const py = await loadPyodide({ indexURL });
  await py.loadPackage(PACKAGES);
  await py.runPythonAsync("import micropip\nawait micropip.install('mutagen')");
  py.FS.mkdirTree('/engine');
  py.FS.mkdirTree('/cache');
  for (const name of engineFiles) {
    py.FS.writeFile(`/engine/${name}`, await fetchText(name));
  }
  await py.runPythonAsync("import sys\nsys.path.insert(0, '/engine')\nimport engine_host");
  const host = py.pyimport('engine_host');
  const engineVersion = host.setup('/cache');

  function convert(value) {
    if (value && typeof value === 'object' && typeof value.toJs === 'function') {
      const out = value.toJs();
      value.destroy();
      return out;
    }
    return value;
  }

  function call(name, ...args) {
    return convert(host[name](...args));
  }

  return { py, call, engineVersion };
}
