// Browser UI: intake, scheduling and display only. No analysis logic here.
import { buildAlbums, entriesFromInput, entriesFromDrop } from './intake.js';
import { createScheduler } from './scheduler.js';
import { openCache } from './cache.js';

const $ = (id) => document.getElementById(id);

// Display tables transcribed from gui.py.
const VERDICT_TONES = {
  'GENUINE LOSSLESS': 'ok', 'PROBABLY LOSSLESS': 'warn', SUSPICIOUS: 'warn',
  'PROBABLE UPSCALE': 'bad', indeterminate: 'unknown',
};
const FILTERS = [['all', 'All', 'accent'], ['flagged', 'Flagged', 'bad'],
  ['suspicious', 'Suspicious', 'warn'], ['errors', 'Errors', 'danger']];
const CUTOFF_LABELS = { no_cutoff: 'no cut-off', near_nyquist_cutoff: 'near Nyquist', far_cutoff: 'far cut-off' };
const MD5_LABELS = { match: 'verified', mismatch: 'mismatch', absent: 'absent', unverifiable: 'unverifiable' };
const LSBS = ['active', 'partial', 'empty', 'indeterminate'];

const isFailed = (e) => Boolean(e.error) || !e.veredicto || (e.md5 && e.md5.estado === 'mismatch');
function matchesFilter(e, mode) {
  if (mode === 'all') return true;
  if (mode === 'errors') return isFailed(e);
  if (isFailed(e)) return false;
  if (mode === 'flagged') return e.veredicto === 'PROBABLE UPSCALE';
  return e.veredicto === 'SUSPICIOUS';
}
const fmtDepth = (b) => (typeof b === 'number' && b ? `${b} bit` : '—');
const fmtRate = (r) => (typeof r !== 'number' || r <= 0 ? '—'
  : r % 1000 === 0 ? `${r / 1000} kHz` : `${(r / 1000).toFixed(1)} kHz`);
function cutoffText(e) {
  const s = e.esp;
  if (!s || !Object.keys(s).length) return '—';
  if (s.error) return 'not analyzed';
  if (!s.corte_detectado) return 'no cut-off';
  const cat = CUTOFF_LABELS[s.clase_corte] || 'cut-off';
  return s.frecuencia_corte ? `${cat} · ${(s.frecuencia_corte / 1000).toFixed(1)} kHz` : cat;
}
function lsbText(e) {
  const b = e.bdi;
  if (!b || !Object.keys(b).length) return '—';
  if (!b.aplica) return 'not applicable';
  return LSBS.includes(b.lsbs) ? b.lsbs : '—';
}
function clippingText(e) {
  const c = e.clip;
  if (!c || !Object.keys(c).length) return '—';
  const runs = c.runs_clip || 0;
  return c.hay_clipping || runs ? `${runs} run(s) at full scale` : 'no clipping';
}
function md5Detail(e) {
  const m = e.md5;
  if (!m || !Object.keys(m).length) return '—';
  const state = MD5_LABELS[m.estado] || '—';
  return m.calculado ? `${state} · ${String(m.calculado).slice(0, 8)}…` : state;
}

const state = {
  albums: [], dropName: 'Files', rows: new Map(), warnings: new Map(),
  filter: 'all', selected: null, status: 'Idle', done: 0, total: 0, running: false,
};
let scheduler = null;
let cache = null;

function setStatus(text) { state.status = text; $('state').textContent = text; renderControls(); }
function renderControls() {
  const busy = state.status === 'Loading engine…' || state.status === 'Analyzing';
  $('analyze').disabled = busy || !state.albums.length;
  $('stop').disabled = !busy;
  $('progress').textContent = state.total ? `${state.done} / ${state.total}` : '';
}

function showError(text) { const p = $('start-error'); p.textContent = text; p.hidden = !text; }

function loadEntries(entries, dropName) {
  const albums = buildAlbums(entries, dropName);
  if (!albums.length) { showError('No FLAC files found. Choose a folder or .flac files.'); return; }
  showError('');
  state.albums = albums;
  state.dropName = dropName;
  analyze();
}

function rootName(entries) {
  const first = entries[0]?.relPath || '';
  return first.includes('/') ? first.split('/')[0] : 'Files';
}

function setupWorkers() {
  const max = Math.max(1, Math.min((navigator.hardwareConcurrency || 2) - 1, 6));
  const sel = $('workers');
  for (let i = 1; i <= max; i++) sel.add(new Option(String(i), String(i)));
  sel.value = String(Math.min(2, max));
}

async function analyze() {
  if (!state.albums.length) return;
  if (scheduler) scheduler.stop();
  try { cache ??= openCache(); } catch { cache = null; }
  state.rows = new Map(); state.warnings = new Map(); state.selected = null;
  state.done = 0; state.total = 0;
  $('start').hidden = true; $('results').hidden = false; $('inspector').hidden = true;
  setStatus('Loading engine…');
  const mode = $('mode').value;
  const seconds = Number($('seconds').value) || 15;
  scheduler = createScheduler({
    workerFactory: () => new Worker(new URL('./worker.js', import.meta.url), { type: 'module' }),
    poolSize: Number($('workers').value) || 2,
    cache,
    onEvent: handleEvent,
    base: '.',
  });
  renderTable();
  scheduler.run(state.albums, { mode, seconds, ignoreCache: $('ignore-cache').checked });
}

function handleEvent(ev) {
  if (ev.type === 'start') { state.total = ev.total; setStatus('Loading engine…'); }
  else if (ev.type === 'track') {
    if (state.status === 'Loading engine…') setStatus('Analyzing');
    state.done += 1;
    const old = state.rows.get(ev.relPath);
    if (old?.url) URL.revokeObjectURL(old.url);
    state.rows.set(ev.relPath, {
      album: ev.album, relPath: ev.relPath, result: ev.result,
      url: ev.png ? URL.createObjectURL(new Blob([ev.png], { type: 'image/png' })) : null,
      coherenceFailed: Boolean(ev.coherence_failed),
    });
    renderTable(); renderControls();
    if (state.selected === ev.relPath) renderInspector();
  } else if (ev.type === 'album-warning') {
    state.warnings.set(ev.album, ev.message); renderWarnings();
  } else if (ev.type === 'done') { setStatus('Idle'); }
  else if (ev.type === 'fatal') { scheduler.stop(); setStatus('Idle'); showFatal(ev.message); }
}

function showFatal(message) {
  state.warnings.set('', message); renderWarnings();
}

function renderWarnings() {
  const box = $('warnings'); box.replaceChildren();
  for (const [album, message] of state.warnings) {
    const p = document.createElement('p');
    p.className = 'error';
    p.textContent = album ? `${album}: ${message}` : message;
    box.append(p);
  }
}

const rowFile = (r) => r.result.archivo || r.relPath.slice(r.relPath.lastIndexOf('/') + 1);
const multiAlbum = () => new Set([...state.rows.values()].map((r) => r.album)).size > 1
  || state.albums.length > 1;

function chip(tone, text) {
  const s = document.createElement('span');
  s.className = `chip tone-${tone}`; s.textContent = text; return s;
}
function rowTone(e) {
  return e.error || !e.veredicto ? 'danger' : (VERDICT_TONES[e.veredicto] || 'unknown');
}

function renderFilters() {
  const bar = $('filters'); bar.replaceChildren();
  const all = [...state.rows.values()].map((r) => r.result);
  for (const [key, title, tone] of FILTERS) {
    const b = document.createElement('button');
    b.textContent = `${title} (${all.filter((e) => matchesFilter(e, key)).length})`;
    if (key === state.filter) { b.classList.add('active', tone === 'accent' ? 'primary' : `tone-${tone}`); }
    b.onclick = () => { state.filter = key; renderTable(); };
    bar.append(b);
  }
}

function renderTable() {
  renderFilters();
  const multi = multiAlbum();
  const head = $('head'); head.replaceChildren();
  const cols = [...(multi ? ['Album'] : []), 'Track', 'Verdict', 'Score', 'Issues'];
  for (const c of cols) {
    const th = document.createElement('th'); th.textContent = c;
    if (c === 'Score' || c === 'Issues') th.className = 'num';
    head.append(th);
  }
  const body = $('rows'); body.replaceChildren();
  let shown = 0;
  for (const r of state.rows.values()) {
    const e = r.result;
    if (!matchesFilter(e, state.filter)) continue;
    shown += 1;
    const tr = document.createElement('tr');
    if (r.relPath === state.selected) tr.className = 'selected';
    const cell = (content, cls) => {
      const td = document.createElement('td'); if (cls) td.className = cls;
      td.append(content); tr.append(td);
    };
    if (multi) cell(r.album.split('/').pop());
    cell(rowFile(r));
    cell(chip(rowTone(e), e.error || !e.veredicto ? 'ERROR' : e.veredicto.toUpperCase()));
    cell(typeof e.score === 'number' ? String(Math.round(e.score * 100)) : '—', 'num');
    cell(String(e.error ? 1 : (e.problemas || []).length), 'num');
    tr.onclick = () => { state.selected = r.relPath; renderTable(); renderInspector(); };
    body.append(tr);
  }
  $('empty').hidden = shown > 0;
}

function renderInspector() {
  const panel = $('inspector');
  const r = state.rows.get(state.selected);
  if (!r) { panel.hidden = true; return; }
  const e = r.result;
  panel.hidden = false; panel.replaceChildren();
  const tone = rowTone(e);
  const failed = e.error || !e.veredicto;
  const head = document.createElement('div'); head.className = 'head';
  head.append(chip(tone, failed ? 'ERROR' : e.veredicto.toUpperCase()));
  const score = document.createElement('span'); score.className = 'score';
  score.textContent = typeof e.score === 'number' ? String(Math.round(e.score * 100)) : '—';
  head.append(score);
  const title = document.createElement('h2'); title.textContent = rowFile(r);
  const meta = e.meta || {};
  const dl = document.createElement('dl');
  const add = (k, v) => {
    const dt = document.createElement('dt'); dt.textContent = k;
    const dd = document.createElement('dd'); dd.textContent = v; dl.append(dt, dd);
  };
  add('Depth', fmtDepth(meta.bits_per_sample)); add('Rate', fmtRate(meta.sample_rate));
  add('DR', typeof e.dr === 'number' ? String(e.dr) : '—');
  add('MD5', MD5_LABELS[(e.md5 || {}).estado] || '—');
  add('Cut-off', cutoffText(e)); add('LSB', lsbText(e));
  add('Clipping', clippingText(e)); add('MD5 detail', md5Detail(e));
  const list = document.createElement('ul'); list.className = 'problems';
  const items = e.error ? [[String(e.error), 'danger']] : [
    ...(e.problemas || []).map((p) => [String(p), tone === 'bad' ? 'bad' : 'warn']),
    ...(e.notes || []).map((n) => [String(n), 'accent']),
  ];
  if (!items.length) { const li = document.createElement('li'); li.textContent = 'No anomalies detected'; li.style.setProperty('--dot', 'var(--ok)'); list.append(li); }
  for (const [text, t] of items) {
    const li = document.createElement('li'); li.textContent = text;
    li.style.setProperty('--dot', `var(--${t})`); list.append(li);
  }
  panel.append(head, title, dl, list);
  if (r.url) { const img = document.createElement('img'); img.src = r.url; img.alt = 'Spectrogram'; panel.append(img); }
  else { const n = document.createElement('div'); n.className = 'none'; n.textContent = 'No spectrogram'; panel.append(n); }
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('theme-icon').style.setProperty('--src', `url(icons/${theme === 'dark' ? 'sun' : 'moon'}.png)`);
  try { localStorage.setItem('theme', theme); } catch { /* storage unavailable */ }
}

function wire() {
  let saved = 'dark';
  try { saved = localStorage.getItem('theme') || 'dark'; } catch { /* storage unavailable */ }
  setTheme(saved);
  $('theme').onclick = () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  $('mode').onchange = () => { $('seconds-row').hidden = $('mode').value !== 'seconds'; };
  $('analyze').onclick = analyze;
  $('stop').onclick = () => { scheduler?.stop(); setStatus('Stopped'); };
  $('browse-folder').onclick = () => $('folder-input').click();
  $('browse-files').onclick = () => $('files-input').click();
  const fromInput = (ev) => {
    const entries = entriesFromInput(ev.target.files);
    if (entries.length) loadEntries(entries, rootName(entries));
    ev.target.value = '';
  };
  $('folder-input').onchange = fromInput; $('files-input').onchange = fromInput;
  const zone = document.body;
  zone.addEventListener('dragover', (ev) => { ev.preventDefault(); $('dropzone').classList.add('over'); });
  zone.addEventListener('dragleave', () => $('dropzone').classList.remove('over'));
  zone.addEventListener('drop', async (ev) => {
    ev.preventDefault(); $('dropzone').classList.remove('over');
    try {
      const entries = await entriesFromDrop(ev.dataTransfer.items);
      loadEntries(entries, rootName(entries));
    } catch (err) {
      showError(`Could not read the dropped items: ${err.message}. Try Browse folder.`);
      $('start').hidden = false;
    }
  });
  setupWorkers(); renderControls(); renderTable();
}
wire();
