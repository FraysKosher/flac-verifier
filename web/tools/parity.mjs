// Native-vs-Pyodide parity: node tools/parity.mjs <dir-with-flac-files>
// Native interpreter: repo .venv, then $FV_PYTHON, then python3.
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadPyodide } from 'pyodide';
import { loadEngine } from '../runtime.js';
import { decodeFlac32 } from '../flac32.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = path.resolve(here, '..');
const root = path.resolve(web, '..');
const dir = path.resolve(process.env.INIT_CWD ?? '.', process.argv[2] ?? '');
if (!process.argv[2] || !existsSync(dir)) {
  console.error('usage: npm run parity -- <dir with .flac files>');
  process.exit(2);
}
const names = readdirSync(dir).filter((n) => n.toLowerCase().endsWith('.flac')).sort();

const IGNORED = new Set(['/ruta', '/esp/espectrograma']);
const TOLERANT = '/esp/var_alta_db';
const isTiming = (k) => /(^|\/)(_?secs|segundos|tiempo\w*|elapsed\w*|duracion_analisis\w*)$/i.test(k);

function pythonBin() {
  for (const p of [path.join(root, '.venv/bin/python'), path.join(root, '.venv/Scripts/python.exe'), process.env.FV_PYTHON]) {
    if (p && existsSync(p)) return p;
  }
  return 'python3';
}

function flat(value, prefix = '', out = {}) {
  if (Array.isArray(value)) value.forEach((v, i) => flat(v, `${prefix}[${i}]`, out));
  else if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) flat(v, `${prefix}/${k}`, out);
  } else out[prefix] = value;
  return out;
}

function differs(key, a, b) {
  if (IGNORED.has(key) || isTiming(key)) return false;
  if (key === TOLERANT && typeof a === 'number' && typeof b === 'number') {
    return Math.abs(a - b) > 1e-4 * Math.max(Math.abs(a), Math.abs(b));
  }
  return a !== b;
}

const native = JSON.parse(execFileSync(pythonBin(), [path.join(here, 'parity_native.py'), dir], {
  maxBuffer: 1 << 28, encoding: 'utf8',
}));

const engineFiles = ['motor_flac.py', '_motor_bits.py', '_motor_espectral.py', '_motor_veredicto.py',
  '_motor_cache.py', 'engine_host.py', 'wasm_compat.py'];
const fetchText = async (name) => readFileSync(
  path.join(name === 'engine_host.py' || name === 'wasm_compat.py' ? web : root, name), 'utf8');
const indexURL = path.join(web, 'node_modules/pyodide') + '/';
const engine = await loadEngine({ indexURL, engineFiles, fetchText, loadPyodide });
engine.py.FS.mkdirTree('/fx');
engine.py.FS.mount(engine.py.FS.filesystems.NODEFS, { root: dir }, '/fx');

let failed = 0;
let seq = 0;
for (const name of names) {
  const vpath = `/fx/${name}`;
  let raw = null;
  if (engine.call('needs_s32', vpath)) {
    const bytes = readFileSync(path.join(dir, name));
    const { interleaved, release } = await decodeFlac32(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length));
    raw = `/s32/${++seq}.raw`;
    engine.py.FS.mkdirTree('/s32');
    engine.py.FS.writeFile(raw, new Uint8Array(interleaved.buffer, interleaved.byteOffset, interleaved.byteLength));
    release();
    engine.call('set_s32_source', vpath, raw);
  }
  const [json] = engine.call('analyze', vpath, 'full', null);
  if (raw) { engine.call('set_s32_source', vpath, ''); engine.py.FS.unlink(raw); }
  const browser = JSON.parse(json);
  const n = native[name];
  const a = flat(n), b = flat(browser);
  const diffs = [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()
    .filter((k) => differs(k, a[k], b[k]));
  if (diffs.length === 0) console.log(`IDENTICAL  ${name}`);
  else {
    failed++;
    console.log(`DIFFERENT  ${name}  (${diffs.length} fields)`);
    for (const k of diffs.slice(0, 10)) console.log(`    ${k}: native=${JSON.stringify(a[k])} browser=${JSON.stringify(b[k])}`);
  }
}
process.exit(failed ? 1 : 0);
