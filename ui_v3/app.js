import { renderCoverHTML, attachCoverFallbacks } from "./covers.js";
import { renderInteractiveSpectrogram } from "./spectrogram.js";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const ROW_HEIGHT = 44;
const OVERSCAN = 14;

const TECH_GLOSSARY = {
  "DR": "Dynamic Range (Crest Factor / 3s RMS): higher numbers indicate less loudness compression.",
  "MDCT-2048": "WMAv2 2048-point Modified Discrete Cosine Transform quantization lattice.",
  "MDCT-1024": "AAC / Vorbis 1024-point lapped transform quantization lattice.",
  "MDCT-512": "AC-3 / E-AC-3 512-point Kaiser-Bessel Derived transform lattice.",
  "CELT-960": "Opus 20 ms low-overlap MDCT frame boundary lattice (960 samples at 48 kHz).",
  "PQMF-576": "MP3 32-band polyphase filterbank + 18-point MDCT quantization grid.",
  "PQMF-32": "Musepack 32-band subband filterbank 36-sample frame lattice.",
  "SBR": "Spectral Band Replication (HE-AAC): synthesizes upper frequencies from lower bands.",
  "EnCodec": "Neural audio codec leaving exact 1,200 Hz transposed-convolution comb harmonics.",
  "sfb21": "MP3 scalefactor band 21 (>16 kHz) producing intermittent high-frequency spectral islands.",
  "AccurateRip": "Offline Red Book CDDA frame CRC check matching lossless CD pressing boundaries.",
  "DoP": "DSD over PCM (DoP 1.0): 1-bit DSD stream framed inside a 24-bit PCM container.",
};

const TECH_TERM_RE = new RegExp(
  `\\b(${Object.keys(TECH_GLOSSARY).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b`,
  "g"
);

const VIEW_TITLES = {
  albums: "Albums",
  tracks: "All Tracks",
  needs_review: "Needs Review",
  drop: "Quick Drop",
  lossy: "Lossy Transcodes",
  hires_upscaled: "Hi-Res Upscaled",
  fake_24bit: "Fake 24-bit Containers",
  ai_neural: "AI / Neural Attacks",
  corrupt_md5: "Corrupt MD5",
  reviewed: "Reviewed / Ignored",
};

const state = {
  appVersion: "3.0.0",
  engineVersion: "17.2",
  portable: false,
  theme: "dark",
  cpuCount: 8,
  settings: {
    theme: "dark",
    workers: 0,
    pdf_dest_mode: "album",
    custom_pdf_dir: "",
    offline_mode: false,
    albums_view: "grid",
    albums_sort: "artist",
    albums_sort_desc: false,
    library_pdf: false,
    library_no_cache: false,
  },
  counts: {
    albums: 0, tracks: 0, genuine: 0, suspicious: 0, flagged: 0,
    needs_review: 0, reviewed: 0, corrupt_md5: 0,
    lossy: 0, hires_upscaled: 0, fake_24bit: 0, ai_neural: 0,
  },
  roots: [],
  outdated: { total_outdated: 0, flagged_outdated: 0, versions: [] },
  bannerDismissed: false,
  libraryStatus: {
    state: "idle", paused: false, queued_albums: 0,
    total_albums: 0, done_albums: 0, current_album: "",
    current_album_path: "", current_track_file: "",
    current_track_index: 0, current_track_total: 0,
    active_workers: 0, up_next: [],
  },
  updateInfo: null,

  // Navigation & filter state
  view: "albums",       // albums | album_detail | tracks | needs_review | drop
  albumsView: "grid",   // grid | list
  albumsSort: "artist", // artist | title | flagged | year | format | tracks | recent
  albumsSortDesc: false,
  albumId: null,
  smartFilter: null,    // lossy | hires_upscaled | fake_24bit | ai_neural | corrupt_md5 | reviewed
  rootFilter: null,     // root_id
  pill: "all",          // all | ok | warn | bad
  search: "",
  sortKey: "score",
  sortDesc: false,
  userSortedAlbum: false,

  // Add Library Folder modal draft
  addRootDraft: {
    path: "",
    workers: 0,
    pdf: false,
    pdf_dest_mode: "album",
    custom_pdf_dir: "",
    no_cache: false,
  },

  // Active data cache
  albums: [],
  activeAlbum: null,
  tracks: [],
  pillCounts: { all: 0, ok: 0, warn: 0, bad: 0 },

  // Inspector & Lightbox state
  selectedTrack: null,      // full track object (library or quick-drop)
  specZoom: "full",
  specOverlay: true,
  specCache: new Map(),     // file_path -> spectrogram response
  specRequestSeq: 0,
  lightboxSpecData: null,

  // Quick Drop state
  dropRunning: false,
  dropTarget: "",
  dropTotalAlbums: 0,
  dropCurrentAlbumIdx: 0,
  dropResults: [],          // normalized track-like objects from live NDJSON
  dropPdfs: [],
  dropPdf: false,
  dropPdfDest: "album",
  dropCustomPdfDir: "",
  dropIgnoreCache: false,
  dropWorkers: 0,
  recentQuickDrops: [],

  // Command palette
  cmdItems: [],
  cmdActiveIdx: 0,
};

let searchTimer = null;
let virtualBound = null;
let dragDepth = 0;

function api() {
  return window.pywebview && window.pywebview.api ? window.pywebview.api : null;
}

function esc(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatRateCompact(sr) {
  const n = Number(sr);
  if (!n) return "—";
  return n % 1000 ? `${(n / 1000).toFixed(1)}k` : `${Math.round(n / 1000)}k`;
}

function formatRateFull(sr) {
  const n = Number(sr);
  if (!n) return "—";
  return n % 1000 ? `${(n / 1000).toFixed(1)} kHz` : `${Math.round(n / 1000)} kHz`;
}

function formatAlbumFormat(bits, sr) {
  if (!bits && !sr) return "FLAC";
  const b = bits ? `${bits}` : "?";
  const r = sr ? (sr % 1000 ? (sr / 1000).toFixed(1) : `${Math.round(sr / 1000)}`) : "?";
  return `${b} / ${r}`;
}

function shortVerdictLabel(t) {
  if (t.reviewed) return "Reviewed";
  const v = String(t.verdict || "");
  if (v === "GENUINE LOSSLESS") return "Genuine";
  if (v === "PROBABLY LOSSLESS") return "Prob. Lossless";
  if (v === "SUSPICIOUS") return "Suspicious";
  if (v === "PROBABLE UPSCALE") return "Upscale";
  if (v === "CORRUPT MD5") return "Corrupt MD5";
  if (v === "ERROR") return "Error";
  return "Indeterminate";
}

function showToast(msg, duration = 2800) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.remove("show"), duration);
}

function annotateTechnicalTerms(htmlText) {
  return htmlText.replace(TECH_TERM_RE, (term) => {
    const tip = TECH_GLOSSARY[term];
    if (!tip) return term;
    return `<span class="tech-term" data-tip="${esc(tip)}">${term}</span>`;
  });
}

function formatEvidenceText(raw) {
  const escaped = esc(raw);
  const withCode = escaped.replace(/`([^`]+)`/g, '<code class="ev-code">$1</code>');
  const colonIdx = withCode.indexOf(":");
  let formatted = withCode;
  if (colonIdx > 0 && colonIdx < 48 && !withCode.slice(0, colonIdx).includes("<")) {
    formatted = `<strong>${withCode.slice(0, colonIdx + 1)}</strong>${withCode.slice(colonIdx + 1)}`;
  }
  return annotateTechnicalTerms(formatted);
}

/* ==========================================================================
   THEME & TOOLTIPS
   ========================================================================== */
function applyThemeUI(mode) {
  const clean = mode === "light" ? "light" : "dark";
  state.theme = clean;
  document.documentElement.dataset.theme = clean;
  $("#theme-svg").innerHTML = clean === "dark"
    ? `<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4"/>`
    : `<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>`;
}

async function toggleTheme() {
  const next = state.theme === "dark" ? "light" : "dark";
  applyThemeUI(next);
  if (api()) {
    await api().set_theme(next);
  }
}

function bindGlobalTooltips() {
  const tipEl = $("#tooltip-float");
  let activeTarget = null;
  document.addEventListener("mouseover", (e) => {
    const target = e.target.closest("[data-tip]");
    if (!target) return;
    activeTarget = target;
    tipEl.textContent = target.dataset.tip || "";
    tipEl.hidden = false;
    positionTooltip(target, tipEl);
  });
  document.addEventListener("mouseout", (e) => {
    if (!activeTarget) return;
    if (!e.relatedTarget || !activeTarget.contains(e.relatedTarget)) {
      tipEl.hidden = true;
      activeTarget = null;
    }
  });
}

function positionTooltip(target, tipEl) {
  const rect = target.getBoundingClientRect();
  const tw = tipEl.offsetWidth || 220;
  const th = tipEl.offsetHeight || 40;
  let left = rect.left + rect.width / 2 - tw / 2;
  left = Math.max(10, Math.min(window.innerWidth - tw - 10, left));
  let top = rect.top - th - 8;
  if (top < 10) top = rect.bottom + 8;
  tipEl.style.left = `${Math.round(left)}px`;
  tipEl.style.top = `${Math.round(top)}px`;
}

/* ==========================================================================
   SIDEBAR, COUNTS, BANNER & INDEXER FOOTER
   ========================================================================== */
function renderSidebarChrome() {
  const c = state.counts;
  $("#cnt-albums").textContent = c.albums;
  $("#cnt-tracks").textContent = c.tracks;
  $("#cnt-flagged").textContent = c.needs_review;
  $("#cnt-flagged").style.display = c.needs_review ? "" : "none";

  $("#cnt-lossy").textContent = c.lossy;
  $("#cnt-hires-up").textContent = c.hires_upscaled;
  $("#cnt-fake24").textContent = c.fake_24bit;
  $("#cnt-ai").textContent = c.ai_neural;
  $("#cnt-corrupt").textContent = c.corrupt_md5;
  $("#cnt-reviewed").textContent = c.reviewed;

  $("#st-ok").textContent = c.genuine;
  $("#st-bad").textContent = c.needs_review;
  $("#st-engine").textContent = state.engineVersion;
  $("#engine-ver-footer").textContent = `Engine ${state.engineVersion}`;
  $("#db-mode-label").textContent = state.portable ? "Portable · SQLite" : "library.db · SQLite";

  $("#brand-ver").textContent = String(state.appVersion || "3.0.0");

  // Active nav highlighting
  $$(".nav-item").forEach((btn) => {
    const isView = btn.dataset.view && btn.dataset.view === state.view && !state.smartFilter && !state.rootFilter;
    const isSmart = btn.dataset.smart && btn.dataset.smart === state.smartFilter;
    btn.classList.toggle("active", Boolean(isView || isSmart));
  });

  // Library root folders
  const rootsEl = $("#roots-list");
  if (!state.roots.length) {
    rootsEl.innerHTML = `<div class="roots-empty">No folders connected yet</div>`;
  } else {
    rootsEl.innerHTML = state.roots.map((r) => {
      const active = state.rootFilter === r.id ? "active" : "";
      const ledCls = r.online ? "" : "offline";
      const sub = r.online
        ? `${r.album_count} album${r.album_count === 1 ? "" : "s"}`
        : "Offline";
      return `
        <div class="folder-row ${active}">
          <button class="folder-item" data-root-id="${r.id}" title="${esc(r.path)}">
            <span class="status-led ${ledCls}"></span>
            <span class="folder-meta">
              <span class="folder-name">${esc(r.label)}</span>
              <span class="folder-sub">${esc(sub)}</span>
            </span>
          </button>
          <button class="folder-remove" data-remove-root="${r.id}" title="Remove folder from library">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button>
        </div>`;
    }).join("");

    rootsEl.querySelectorAll("[data-root-id]").forEach((btn) => {
      btn.onclick = () => {
        const rid = Number(btn.dataset.rootId);
        state.view = "albums";
        state.smartFilter = null;
        state.albumId = null;
        state.rootFilter = state.rootFilter === rid ? null : rid;
        state.pill = "all";
        updatePillUI();
        refreshCurrentView();
      };
    });
    rootsEl.querySelectorAll("[data-remove-root]").forEach((btn) => {
      btn.onclick = async (e) => {
        e.stopPropagation();
        const rid = Number(btn.dataset.removeRoot);
        if (!api()) return;
        const res = await api().remove_library_folder(rid, true);
        if (res.ok) {
          if (state.rootFilter === rid) state.rootFilter = null;
          state.roots = res.roots || [];
          state.counts = res.counts || state.counts;
          state.outdated = res.outdated || state.outdated;
          showToast("Removed folder from library");
          await refreshCurrentView();
        }
      };
    });
  }

  // Engine version upgrade banner
  const banner = $("#engine-banner");
  const outCount = Number(state.outdated?.total_outdated || 0);
  if (outCount > 0 && !state.bannerDismissed) {
    const prevVers = (state.outdated.versions || []).join(", ") || "previous version";
    $("#engine-banner-text").innerHTML =
      `Engine updated to <strong>${esc(state.engineVersion)}</strong> · ` +
      `<strong>${outCount}</strong> album${outCount === 1 ? "" : "s"} analyzed with ${esc(prevVers)}`;
    $("#btn-reverify-flagged").hidden = Number(state.outdated?.flagged_outdated || 0) === 0;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }

  // Optional update pill (populated by Phase 4 updater)
  const updBtn = $("#btn-update-pill");
  if (state.updateInfo && state.updateInfo.version) {
    $("#update-pill-text").textContent = `Update ${state.updateInfo.version} available`;
    updBtn.hidden = false;
  } else {
    updBtn.hidden = true;
  }

  renderIndexerFooter();
}

function renderIndexerFooter() {
  const st = state.libraryStatus || {};
  const dot = $("#idx-dot");
  const albEl = $("#idx-album");
  const detEl = $("#idx-detail");
  const btn = $("#idx-btn");
  const cancelBtn = $("#idx-cancel");
  const bar = $("#idx-bar");

  dot.classList.remove("scanning", "paused");
  if (st.state === "scanning" || st.state === "pausing") {
    dot.classList.add("scanning");
    const total = Math.max(1, Number(st.total_albums || 1));
    const done = Number(st.done_albums || 0);
    const tTot = Math.max(1, Number(st.current_track_total || 1));
    const tCur = Number(st.current_track_index || 0);
    const pct = Math.min(99, Math.max(4, Math.round(((done + tCur / tTot) / total) * 100)));
    bar.style.width = `${pct}%`;
    albEl.textContent = st.current_album || "Scanning library…";
    albEl.title = st.current_album_path || st.current_album || "";
    detEl.textContent = st.state === "pausing"
      ? `Pausing · Album ${Math.min(done + 1, total)}/${total}`
      : `Album ${Math.min(done + 1, total)}/${total} · Track ${Math.min(tCur, tTot)}/${tTot}`;
    btn.textContent = st.state === "pausing" ? "Pausing" : "Pause";
    cancelBtn.hidden = false;
  } else if (st.state === "paused") {
    dot.classList.add("paused");
    const total = Math.max(1, Number(st.total_albums || 1));
    const done = Number(st.done_albums || 0);
    bar.style.width = `${Math.round((done / total) * 100)}%`;
    albEl.textContent = st.current_album || "Scan paused";
    detEl.textContent = `Paused · ${done}/${total} albums`;
    btn.textContent = "Resume";
    cancelBtn.hidden = false;
  } else {
    bar.style.width = "100%";
    albEl.textContent = state.counts.albums ? "Library up to date" : "Ready to index";
    albEl.title = "";
    detEl.textContent = state.counts.albums
      ? `${state.counts.albums} album${state.counts.albums === 1 ? "" : "s"} · ${state.counts.tracks} tracks`
      : "No active scan";
    btn.textContent = "Rescan";
    cancelBtn.hidden = true;
  }

  if ($("#activity-modal").classList.contains("open")) {
    renderScanActivityModal();
  }
}

function updatePillCountsUI() {
  $("#pill-all").textContent = state.pillCounts.all || 0;
  $("#pill-ok").textContent = state.pillCounts.ok || 0;
  $("#pill-warn").textContent = state.pillCounts.warn || 0;
  $("#pill-bad").textContent = state.pillCounts.bad || 0;
}

function updatePillUI() {
  $$("#filter-pills .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.pill === state.pill));
}

function updateAlbumsViewToggleUI() {
  const isAlbums = state.view === "albums";
  const wrap = $("#albums-view-toggle");
  if (wrap) {
    wrap.hidden = !isAlbums;
    wrap.querySelectorAll("[data-albums-view]").forEach((b) => {
      b.classList.toggle("active", b.dataset.albumsView === state.albumsView);
    });
  }
  const sortWrap = $("#albums-sort-wrap");
  if (sortWrap) {
    sortWrap.hidden = !isAlbums;
    const sel = $("#sel-albums-sort");
    if (sel) {
      sel.value = ["artist", "title", "flagged", "year", "format", "recent"].includes(state.albumsSort)
        ? state.albumsSort
        : "artist";
    }
    const dirBtn = $("#btn-albums-sort-dir");
    if (dirBtn) {
      dirBtn.textContent = state.albumsSortDesc ? "↓" : "↑";
      dirBtn.title = state.albumsSortDesc ? "Descending (click to flip)" : "Ascending (click to flip)";
    }
  }
}

const DEFAULT_ALBUM_SORT_DESC = {
  artist: false,
  title: false,
  flagged: true,
  year: true,
  format: true,
  tracks: true,
  recent: true,
};

function setAlbumsSort(sortKey, descOverride = null) {
  const key = String(sortKey || "artist");
  if (descOverride === null) {
    if (state.albumsSort === key) {
      state.albumsSortDesc = !state.albumsSortDesc;
    } else {
      state.albumsSort = key;
      state.albumsSortDesc = Boolean(DEFAULT_ALBUM_SORT_DESC[key]);
    }
  } else {
    state.albumsSort = key;
    state.albumsSortDesc = Boolean(descOverride);
  }
  state.settings.albums_sort = state.albumsSort;
  state.settings.albums_sort_desc = state.albumsSortDesc;
  updateAlbumsViewToggleUI();
  if (api()) {
    api().save_preferences({
      albums_sort: state.albumsSort,
      albums_sort_desc: state.albumsSortDesc,
    });
  }
  refreshCurrentView();
}

function buildWorkerOptionsHTML(selectedVal) {
  const choices = [0, 1, 2, 4, 6, 8];
  const selNum = Number(selectedVal || 0);
  if (selNum > 0 && !choices.includes(selNum)) choices.push(selNum);
  choices.sort((a, b) => a - b);
  return choices.map((n) => {
    const label = n === 0 ? `Auto (${Math.min(8, state.cpuCount)})` : String(n);
    const sel = selNum === n ? "selected" : "";
    return `<option value="${n}" ${sel}>${label}</option>`;
  }).join("");
}

/* ==========================================================================
   DATA LOADING & VIEW RENDERING
   ========================================================================== */
async function refreshOverview() {
  if (!api()) return;
  const ov = await api().get_overview();
  if (ov && ov.ok) {
    state.counts = ov.counts;
    state.roots = ov.roots;
    state.outdated = ov.outdated;
    state.libraryStatus = ov.library_status;
  }
}

async function refreshCurrentView() {
  renderSidebarChrome();
  const backBtn = $("#btn-back");
  backBtn.hidden = state.view !== "album_detail";
  const subbar = $("#subbar");
  const isEmptyWelcome =
    state.counts.albums === 0 &&
    (state.view === "albums" || state.view === "tracks") &&
    !state.smartFilter &&
    state.libraryStatus.state === "idle";
  subbar.style.display = state.view === "drop" || isEmptyWelcome ? "none" : "flex";
  updateAlbumsViewToggleUI();

  if (state.view === "drop") {
    $("#view-title").textContent = "Quick Drop";
    $("#view-meta").textContent = "One-off forensic audit";
    renderQuickDrop();
    return;
  }

  if (isEmptyWelcome) {
    $("#view-title").textContent = "Welcome";
    $("#view-meta").textContent = `Engine ${state.engineVersion}`;
    renderWelcomeEmptyState();
    return;
  }

  if (!api()) return;

  if (state.view === "albums") {
    const rootObj = state.roots.find((r) => r.id === state.rootFilter);
    $("#view-title").textContent = rootObj ? rootObj.label : "Albums";
    const res = await api().get_albums(
      state.rootFilter,
      state.pill,
      state.search,
      state.albumsSort,
      state.albumsSortDesc
    );
    state.albums = res.albums || [];
    state.pillCounts = res.pill_counts || { all: 0, ok: 0, warn: 0, bad: 0 };
    $("#view-meta").textContent = `${state.albums.length} indexed`;
    updatePillCountsUI();
    renderAlbumsView();
  } else if (state.view === "album_detail" && state.albumId != null) {
    const sortKey = state.userSortedAlbum ? state.sortKey : "num";
    const sortDesc = state.userSortedAlbum ? state.sortDesc : false;
    const res = await api().get_album_detail(
      state.albumId, state.pill, state.search, sortKey, sortDesc
    );
    if (!res.ok) {
      state.view = "albums";
      state.albumId = null;
      return refreshCurrentView();
    }
    state.activeAlbum = res.album;
    state.tracks = res.tracks || [];
    state.pillCounts = res.pill_counts || { all: 0, ok: 0, warn: 0, bad: 0 };
    $("#view-title").textContent = res.album.title;
    $("#view-meta").textContent = res.album.artist;
    updatePillCountsUI();
    renderAlbumDetail();
  } else {
    const viewKey = state.smartFilter || (state.view === "needs_review" ? "needs_review" : "all");
    const res = await api().get_tracks(
      viewKey, state.rootFilter, state.pill, state.search, state.sortKey, state.sortDesc
    );
    state.tracks = res.tracks || [];
    state.pillCounts = res.pill_counts || { all: 0, ok: 0, warn: 0, bad: 0 };
    const titleKey = state.smartFilter || state.view;
    $("#view-title").textContent = VIEW_TITLES[titleKey] || "Tracks";
    $("#view-meta").textContent = `${state.tracks.length} track${state.tracks.length === 1 ? "" : "s"}`;
    updatePillCountsUI();
    renderVirtualizedTrackTable(state.tracks, true);
  }
}

function renderWelcomeEmptyState() {
  detachVirtualScroll();
  $("#viewport").innerHTML = `
    <div class="welcome-wrap">
      <div class="welcome-card">
        <svg class="welcome-mark" viewBox="0 0 64 64" fill="none" aria-hidden="true">
          <rect width="64" height="64" rx="14" fill="#0f0f0d"/>
          <rect x="2" y="32" width="8" height="28" rx="3" fill="#ffffff"/>
          <rect x="15" y="16" width="8" height="44" rx="3" fill="#ffffff"/>
          <rect x="28" y="0" width="8" height="60" rx="3" fill="#ffffff"/>
          <rect x="41" y="24" width="8" height="36" rx="3" fill="#ffffff"/>
          <rect x="54" y="8" width="8" height="52" rx="3" fill="#ffffff"/>
          <polyline points="42,56 48,62 62,48" stroke="#5cb2bc" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <h1 class="welcome-title">Verify your lossless library</h1>
        <p class="welcome-sub">
          Connect a root music folder to discover and audit every album recursively in the background, or run a one-off check with Quick Drop.
        </p>
        <div class="welcome-actions">
          <button class="btn-primary" id="btn-welcome-add">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5v14M5 12h14"/></svg>
            <span>Add Music Library Folder…</span>
          </button>
          <button class="btn-secondary" id="btn-welcome-drop">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 16V4m0 0-4 4m4-4 4 4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
            <span>Open Quick Drop</span>
          </button>
        </div>
        <div class="welcome-hint">Tip: drag and drop any .flac file or album folder onto this window at any time.</div>
      </div>
    </div>`;
  $("#btn-welcome-add").onclick = () => triggerAddLibraryFolder();
  $("#btn-welcome-drop").onclick = () => navigateView("drop");
}

function albumHealthBadge(a) {
  const bad = Number(a.flagged_count || 0);
  const warn = Number(a.suspicious_count || 0);
  const rev = Number(a.reviewed_count || 0);
  if (bad > 0) return { dot: "var(--bad)", cls: "bad", label: `${bad} Flagged` };
  if (warn > 0) return { dot: "var(--warn)", cls: "warn", label: `${warn} Check` };
  if (rev > 0) return { dot: "var(--neutral)", cls: "neutral", label: `${rev} Reviewed` };
  return { dot: "var(--ok)", cls: "ok", label: "Genuine" };
}

function openAlbumById(albumId) {
  state.view = "album_detail";
  state.albumId = Number(albumId);
  state.userSortedAlbum = false;
  state.pill = "all";
  updatePillUI();
  refreshCurrentView();
}

function renderAlbumsView() {
  if (state.albumsView === "list") {
    renderAlbumsList();
  } else {
    renderAlbumsGrid();
  }
}

function renderAlbumsGrid() {
  detachVirtualScroll();
  if (!state.albums.length) {
    $("#viewport").innerHTML = `<div style="padding:56px 24px;text-align:center;color:var(--text-tertiary)">No albums match the current filter.</div>`;
    return;
  }
  const cards = state.albums.map((a) => {
    const health = albumHealthBadge(a);
    const seed = `${a.artist || ""}-${a.title || ""}`;
    return `
      <button class="album-card" data-open-album="${a.id}">
        <div class="album-cover-wrap">
          ${renderCoverHTML(a.cover_hash, seed)}
          <span class="album-badge"><i class="chip-dot" style="background:${health.dot}"></i>${esc(health.label)}</span>
        </div>
        <div class="album-title" title="${esc(a.title)}">${esc(a.title)}</div>
        <div class="album-artist" title="${esc(a.artist)}">${esc(a.artist)}</div>
        <div class="album-foot">
          <span>${esc(formatAlbumFormat(a.bit_depth, a.sample_rate))}</span>
          <span>${esc(a.year || `${a.track_count} trk`)}</span>
        </div>
      </button>`;
  }).join("");

  const vp = $("#viewport");
  vp.innerHTML = `<div class="albums-wrap"><div class="albums-grid">${cards}</div></div>`;
  attachCoverFallbacks(vp);
  vp.querySelectorAll("[data-open-album]").forEach((btn) => {
    btn.onclick = () => openAlbumById(btn.dataset.openAlbum);
  });
}

function renderAlbumsList() {
  detachVirtualScroll();
  const vp = $("#viewport");
  vp.scrollTop = 0;
  if (!state.albums.length) {
    vp.innerHTML = `<div style="padding:56px 24px;text-align:center;color:var(--text-tertiary)">No albums match the current filter.</div>`;
    return;
  }
  const rowsHTML = state.albums.map((a) => {
    const health = albumHealthBadge(a);
    const seed = `${a.artist || ""}-${a.title || ""}`;
    const trkCount = Number(a.track_count || 0);
    return `
      <tr class="album-list-row" data-open-album="${a.id}">
        <td>
          <div class="cell-track">
            <div class="album-list-cover">${renderCoverHTML(a.cover_hash, seed)}</div>
            <div class="track-text">
              <div class="track-name" title="${esc(a.title)}">${esc(a.title)}</div>
              <div class="track-sub" title="${esc(a.artist)}">${esc(a.artist || "Unknown Artist")}</div>
            </div>
          </div>
        </td>
        <td class="mono" style="color:var(--text-secondary)">${esc(formatAlbumFormat(a.bit_depth, a.sample_rate))}</td>
        <td class="mono" style="color:var(--text-tertiary)">${esc(a.year || "—")}</td>
        <td class="right mono" style="color:var(--text-secondary)">${trkCount}</td>
        <td class="right"><span class="chip ${health.cls}"><i class="chip-dot"></i>${esc(health.label)}</span></td>
      </tr>`;
  }).join("");

  const sortMark = (k) => (state.albumsSort === k ? (state.albumsSortDesc ? " ↓" : " ↑") : "");
  const sortedCls = (k) => (state.albumsSort === k ? "sorted" : "");
  vp.innerHTML = `
    <table class="albums-list-table">
      <thead>
        <tr>
          <th class="${sortedCls("artist")}" data-album-sort="artist">Album / Artist${sortMark("artist")}</th>
          <th class="${sortedCls("format")}" style="width:116px" data-album-sort="format">Format${sortMark("format")}</th>
          <th class="${sortedCls("year")}" style="width:78px" data-album-sort="year">Year${sortMark("year")}</th>
          <th class="right ${sortedCls("tracks")}" style="width:82px" data-album-sort="tracks">Tracks${sortMark("tracks")}</th>
          <th class="right ${sortedCls("flagged")}" style="width:148px" data-album-sort="flagged">Verdict${sortMark("flagged")}</th>
        </tr>
      </thead>
      <tbody>${rowsHTML}</tbody>
    </table>`;
  attachCoverFallbacks(vp);
  vp.querySelectorAll("thead th[data-album-sort]").forEach((th) => {
    th.onclick = () => setAlbumsSort(th.dataset.albumSort);
  });
  vp.querySelectorAll("[data-open-album]").forEach((row) => {
    row.onclick = () => openAlbumById(row.dataset.openAlbum);
  });
}

function renderAlbumDetail() {
  const alb = state.activeAlbum;
  if (!alb) return;
  const bad = Number(alb.flagged_count || 0);
  const warn = Number(alb.suspicious_count || 0);
  const badgeCls = bad > 0 ? "bad" : (warn > 0 ? "warn" : "ok");
  const badgeTxt = bad > 0 ? `${bad} Flagged` : (warn > 0 ? `${warn} Suspicious` : "Album Verified");
  const seed = `${alb.artist || ""}-${alb.title || ""}`;
  const kickerParts = [
    formatAlbumFormat(alb.bit_depth, alb.sample_rate),
    alb.year || null,
    alb.root_label || null,
  ].filter(Boolean);

  const heroHTML = `
    <section class="album-hero">
      <div class="album-hero-cover">${renderCoverHTML(alb.cover_hash, seed)}</div>
      <div class="album-hero-info">
        <div class="album-hero-kicker">${esc(kickerParts.join(" · "))}</div>
        <h1 class="album-hero-title">${esc(alb.title)}</h1>
        <div class="album-hero-artist">${esc(alb.artist)}</div>
        <div class="album-hero-facts">
          <span class="chip ${badgeCls}"><i class="chip-dot"></i>${esc(badgeTxt)}</span>
          ${alb.coherence_summary ? `<span class="fact-pill">${esc(alb.coherence_summary)}</span>` : ""}
        </div>
      </div>
      <div class="album-hero-actions">
        <button class="btn-secondary" id="btn-alb-pdf">Generate PDF Report</button>
        <button class="btn-ghost" id="btn-alb-pdf-custom">Save PDF to folder…</button>
        <button class="btn-ghost" id="btn-alb-rescan">Re-verify album</button>
      </div>
    </section>`;

  renderVirtualizedTrackTable(state.tracks, false, heroHTML);
  const vp = $("#viewport");
  attachCoverFallbacks(vp);
  $("#btn-alb-pdf").onclick = async () => {
    if (!api()) return;
    showToast("Generating album PDF report…");
    await api().generate_album_pdf(alb.id, "album", "");
  };
  $("#btn-alb-pdf-custom").onclick = async () => {
    if (!api()) return;
    const res = await api().generate_album_pdf(alb.id, "custom", "");
    if (res && !res.cancelled) showToast("Generating PDF in selected folder…");
  };
  $("#btn-alb-rescan").onclick = async () => {
    if (!api()) return;
    const res = await api().reverify_album(alb.id);
    if (res.ok) showToast(`Queued "${alb.title}" for re-verification`);
    else showToast(res.error || "Could not re-verify album");
  };
}

/* ==========================================================================
   VIRTUALIZED TRACK TABLE (60-120 fps for 50,000+ rows)
   ========================================================================== */
function detachVirtualScroll() {
  if (virtualBound) {
    $("#viewport").removeEventListener("scroll", virtualBound);
    virtualBound = null;
  }
}

function renderTrackRowHTML(t, showAlbum) {
  const sr = Number(t.sample_rate || 44100);
  const nyq = sr / 2;
  const ceiling = Number(t.ceiling_hz || 0);
  const pct = nyq > 0 && ceiling > 0 ? Math.min(100, Math.round((ceiling / nyq) * 100)) : 0;
  const cls = t.verdict_class || "neutral";
  const barColor = cls === "ok" ? "var(--ok)" : (cls === "warn" ? "var(--warn)" : "var(--bad)");
  const chipCls = t.reviewed ? "neutral" : cls;
  const sel = state.selectedTrack && state.selectedTrack.id === t.id ? "selected" : "";
  const rev = t.reviewed ? "reviewed" : "";
  const seed = `${t.album_artist || t.artist || ""}-${t.album_title || ""}`;
  const fmtText = t.bit_depth && t.sample_rate
    ? `${t.bit_depth}/${formatRateCompact(t.sample_rate)}`
    : "—";
  const scoreTxt = t.score != null ? Math.round(Number(t.score)) : "—";
  const md5 = String(t.md5_status || "");
  const md5Glyph = md5 === "match" ? "✓" : (md5 === "mismatch" ? "✕" : "—");
  const md5Color = md5 === "match" ? "var(--ok)" : (md5 === "mismatch" ? "var(--bad)" : "var(--text-tertiary)");

  return `
    <tr class="track-row ${sel} ${rev}" data-track-id="${t.id}">
      <td class="mono" style="color:var(--text-tertiary)">${esc(t.track_number || "—")}</td>
      <td>
        <div class="cell-track">
          ${showAlbum ? `<div class="mini-cover">${renderCoverHTML(t.cover_hash, seed)}</div>` : ""}
          <div class="track-text">
            <div class="track-name" title="${esc(t.title)}">${esc(t.title)}</div>
            ${showAlbum ? `<div class="track-sub" title="${esc((t.album_artist || t.artist) + " — " + (t.album_title || ""))}">${esc(t.album_artist || t.artist || "")}${t.album_title ? " — " + esc(t.album_title) : ""}</div>` : ""}
          </div>
        </div>
      </td>
      <td><span class="chip ${chipCls}"><i class="chip-dot"></i>${esc(shortVerdictLabel(t))}</span></td>
      <td class="right mono" style="font-weight:550">${scoreTxt}</td>
      <td class="col-format mono" style="color:var(--text-secondary)">${esc(fmtText)}</td>
      <td class="col-ceiling">
        <div class="ceiling-cell">
          <span class="ceiling-bar"><i style="width:${pct}%;background:${barColor}"></i></span>
          <span class="mono" style="font-size:11.5px;color:var(--text-secondary)">${ceiling > 0 ? (ceiling / 1000).toFixed(1) + "k" : "—"}</span>
        </div>
      </td>
      <td class="col-evidence"><span class="finding-tag">${esc(t.primary_finding || "—")}</span></td>
      <td class="col-md5 right mono" style="color:${md5Color}">${md5Glyph}</td>
    </tr>`;
}

function renderVirtualizedTrackTable(rows, showAlbum, prefixHTML = "") {
  detachVirtualScroll();
  const vp = $("#viewport");
  vp.scrollTop = 0;

  vp.innerHTML = `${prefixHTML}
    <table class="track-table">
      <thead>
        <tr>
          <th style="width:54px;padding-right:4px" data-sort="num">#</th>
          <th data-sort="title">${showAlbum ? "Track / Album" : "Track"}</th>
          <th style="width:136px" data-sort="verdict">Verdict</th>
          <th style="width:62px" class="right" data-sort="score">Score</th>
          <th class="col-format" style="width:104px" data-sort="sr">Format</th>
          <th class="col-ceiling" style="width:104px" data-sort="ceiling">Ceiling</th>
          <th class="col-evidence" style="width:146px" data-sort="finding">Evidence</th>
          <th class="col-md5 right" style="width:54px">MD5</th>
        </tr>
      </thead>
      <tbody id="track-tbody"></tbody>
    </table>`;

  const tbody = $("#track-tbody");
  vp.querySelectorAll("thead th[data-sort]").forEach((th) => {
    th.onclick = () => {
      const k = th.dataset.sort;
      if (state.view === "album_detail") state.userSortedAlbum = true;
      if (state.sortKey === k) state.sortDesc = !state.sortDesc;
      else {
        state.sortKey = k;
        state.sortDesc = false;
      }
      refreshCurrentView();
    };
  });

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="8" style="padding:48px;text-align:center;color:var(--text-tertiary)">No tracks match the current filter.</td></tr>`;
    return;
  }

  let lastRange = "";
  const paintSlice = () => {
    const heroEl = vp.querySelector(".album-hero");
    const headerOffset = (heroEl ? heroEl.offsetHeight : 0) + 33;
    const scrollTop = Math.max(0, vp.scrollTop - headerOffset);
    const viewH = vp.clientHeight || 680;
    const startIdx = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN);
    const endIdx = Math.min(rows.length, Math.ceil((scrollTop + viewH) / ROW_HEIGHT) + OVERSCAN);
    const key = `${startIdx}:${endIdx}:${state.selectedTrack ? state.selectedTrack.id : ""}`;
    if (key === lastRange) return;
    lastRange = key;

    const topPad = startIdx * ROW_HEIGHT;
    const botPad = (rows.length - endIdx) * ROW_HEIGHT;
    let html = "";
    if (topPad > 0) html += `<tr class="spacer-row" style="height:${topPad}px"><td colspan="8"></td></tr>`;
    for (let i = startIdx; i < endIdx; i++) {
      html += renderTrackRowHTML(rows[i], showAlbum);
    }
    if (botPad > 0) html += `<tr class="spacer-row" style="height:${botPad}px"><td colspan="8"></td></tr>`;
    tbody.innerHTML = html;
    attachCoverFallbacks(tbody);
  };

  tbody.onclick = (e) => {
    const tr = e.target.closest("tr.track-row[data-track-id]");
    if (!tr) return;
    selectLibraryTrack(Number(tr.dataset.trackId));
  };

  virtualBound = () => requestAnimationFrame(paintSlice);
  vp.addEventListener("scroll", virtualBound, { passive: true });
  paintSlice();
}

/* ==========================================================================
   QUICK DROP VIEW (100% parity with v2.13.2)
   ========================================================================== */
function renderQuickDrop() {
  detachVirtualScroll();
  const workerOpts = buildWorkerOptionsHTML(state.dropWorkers);

  const hasResults = state.dropResults.length > 0;
  const batchHTML = (state.dropRunning || hasResults) ? `
    <div class="live-batch-box">
      <div class="live-batch-head">
        <span style="font-weight:550;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${state.dropRunning ? "Auditing" : "Audit Complete"} · ${esc(state.dropTarget ? state.dropTarget.split(/[\\/]/).pop() : "Quick Drop")}
        </span>
        <div class="live-batch-actions">
          <span class="mono" style="color:var(--text-tertiary)">${state.dropResults.length} track${state.dropResults.length === 1 ? "" : "s"}</span>
          ${state.dropRunning ? `<button class="btn-ghost" id="btn-drop-cancel">Cancel</button>` : ""}
          ${!state.dropRunning && state.dropPdfs.length ? `<button class="btn-secondary" id="btn-drop-open-pdf">Open PDF</button>` : ""}
          ${!state.dropRunning && hasResults ? `<button class="btn-primary" id="btn-drop-promote">Add to Library</button>` : ""}
        </div>
      </div>
      ${state.dropResults.map((t, idx) => `
        <button class="recent-row" data-drop-idx="${idx}">
          <span class="mono" style="color:var(--text-tertiary);width:24px">${esc(t.track_number || String(idx + 1).padStart(2, "0"))}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500">${esc(t.title)}</span>
          <span class="finding-tag">${esc(t.primary_finding)}</span>
          <span class="chip ${t.verdict_class}"><i class="chip-dot"></i>${esc(shortVerdictLabel(t))}</span>
          <span class="mono" style="width:30px;text-align:right">${t.score != null ? Math.round(t.score) : "—"}</span>
        </button>
      `).join("")}
    </div>` : "";

  const recentHTML = state.recentQuickDrops.length ? `
    <div class="recent-head">
      <span>Recent One-Off Audits</span>
      <span>Click to re-audit</span>
    </div>
    <div class="recent-list">
      ${state.recentQuickDrops.map((r) => `
        <button class="recent-row" data-recent-path="${esc(r.path)}">
          <div style="flex:1;min-width:0">
            <div style="font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.label)}</div>
            <div style="font-size:11.5px;color:var(--text-tertiary)">${r.track_count} tracks · ${r.flagged_count} flagged · Engine ${esc(r.engine_version || state.engineVersion)}</div>
          </div>
          <span class="mono" style="font-size:11px;color:var(--text-tertiary)">${esc((r.scanned_at || "").slice(0, 10))}</span>
        </button>
      `).join("")}
    </div>` : "";

  $("#viewport").innerHTML = `
    <div class="drop-wrap">
      <div class="drop-header">
        <h2>Quick Forensic Drop</h2>
        <p>Audit a single FLAC file, album, or multi-album root folder without adding it to your persistent library.</p>
      </div>

      <div class="drop-zone" id="drop-zone">
        <div class="drop-icon">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M12 16V4m0 0-4 4m4-4 4 4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/></svg>
        </div>
        <div class="drop-title">Drop a .flac file, album, or multi-album folder here</div>
        <div class="drop-sub">Runs Engine ${esc(state.engineVersion)} in parallel streaming mode · leaves source files untouched</div>
        <div class="drop-actions">
          <button class="btn-primary" id="btn-drop-browse-dir">Browse folder…</button>
          <button class="btn-secondary" id="btn-drop-browse-file">Browse .flac…</button>
        </div>
      </div>

      <div class="drop-options">
        <label class="opt-check"><input type="checkbox" id="chk-drop-pdf" ${state.dropPdf ? "checked" : ""}> Generate PDF report</label>
        <label class="opt-check"><input type="checkbox" id="chk-drop-nocache" ${state.dropIgnoreCache ? "checked" : ""}> Ignore cache</label>
        <div class="opt-select">
          <span>Workers</span>
          <select id="sel-drop-workers">${workerOpts}</select>
        </div>
      </div>

      ${state.dropPdf ? `
        <div class="drop-options drop-pdf-card">
          <div class="drop-pdf-kicker">PDF destination</div>
          <label class="opt-check">
            <input type="radio" name="pdf-dest" value="album" ${state.dropPdfDest === "album" ? "checked" : ""}>
            <span>Save in each album folder <span style="color:var(--text-tertiary);margin-left:4px">(flac_verifier_report.pdf)</span></span>
          </label>
          <label class="opt-check">
            <input type="radio" name="pdf-dest" value="custom" ${state.dropPdfDest === "custom" ? "checked" : ""}>
            <span>Save in custom folder… <span style="color:var(--text-tertiary);margin-left:4px">(&lt;Album&gt;_flac_verifier_report.pdf)</span></span>
          </label>
          ${state.dropPdfDest === "custom" ? `
            <div class="drop-pdf-dir-row">
              <input class="text-input" id="inp-pdf-dir" type="text" placeholder="Choose destination folder…" value="${esc(state.dropCustomPdfDir)}">
              <button class="btn-secondary" id="btn-pick-pdf-dir">Browse…</button>
            </div>
          ` : ""}
        </div>
      ` : ""}

      ${batchHTML}
      ${recentHTML}
    </div>`;

  $("#btn-drop-browse-dir").onclick = async () => {
    if (!api()) return;
    const picked = await api().pick_folder("");
    if (picked.ok && picked.path) await launchQuickDrop(picked.path);
  };
  $("#btn-drop-browse-file").onclick = async () => {
    if (!api()) return;
    const picked = await api().pick_flac_file("");
    if (picked.ok && picked.path) await launchQuickDrop(picked.path);
  };
  $("#chk-drop-pdf").onchange = (e) => {
    state.dropPdf = e.target.checked;
    renderQuickDrop();
  };
  $("#chk-drop-nocache").onchange = (e) => {
    state.dropIgnoreCache = e.target.checked;
  };
  $("#sel-drop-workers").onchange = (e) => {
    state.dropWorkers = Number(e.target.value || 0);
    if (api()) api().save_preferences({ workers: state.dropWorkers });
  };

  $$("input[name='pdf-dest']").forEach((r) => {
    r.onchange = (e) => {
      state.dropPdfDest = e.target.value;
      if (api()) api().save_preferences({ pdf_dest_mode: state.dropPdfDest });
      renderQuickDrop();
    };
  });
  const inpDir = $("#inp-pdf-dir");
  if (inpDir) {
    inpDir.onchange = (e) => {
      state.dropCustomPdfDir = e.target.value.trim();
      if (api()) api().save_preferences({ custom_pdf_dir: state.dropCustomPdfDir });
    };
  }
  const pickDirBtn = $("#btn-pick-pdf-dir");
  if (pickDirBtn) {
    pickDirBtn.onclick = async () => {
      if (!api()) return;
      const picked = await api().pick_folder(state.dropCustomPdfDir);
      if (picked.ok && picked.path) {
        state.dropCustomPdfDir = picked.path;
        await api().save_preferences({ custom_pdf_dir: picked.path });
        renderQuickDrop();
      }
    };
  }

  const cancelBtn = $("#btn-drop-cancel");
  if (cancelBtn) {
    cancelBtn.onclick = async () => {
      if (api()) await api().cancel_quick_drop();
    };
  }
  const openPdfBtn = $("#btn-drop-open-pdf");
  if (openPdfBtn) {
    openPdfBtn.onclick = async () => {
      if (api() && state.dropPdfs[0]) await api().open_external_path(state.dropPdfs[0]);
    };
  }
  const promoteBtn = $("#btn-drop-promote");
  if (promoteBtn) {
    promoteBtn.onclick = async () => {
      if (!api()) return;
      const res = await api().promote_quick_drop();
      if (res.ok) {
        state.counts = res.counts || state.counts;
        renderSidebarChrome();
        showToast(`Added ${res.added_albums} album${res.added_albums === 1 ? "" : "s"} to Library`);
      }
    };
  }

  $$("[data-drop-idx]").forEach((btn) => {
    btn.onclick = () => {
      const t = state.dropResults[Number(btn.dataset.dropIdx)];
      if (t) openInspectorWithTrack(t);
    };
  });
  $$("[data-recent-path]").forEach((btn) => {
    btn.onclick = () => launchQuickDrop(btn.dataset.recentPath);
  });
}

async function launchQuickDrop(targetPath) {
  if (!api()) return;
  if (state.dropPdf && state.dropPdfDest === "custom" && !state.dropCustomPdfDir) {
    showToast("Select a custom destination folder for the PDF report");
    return;
  }
  state.view = "drop";
  state.smartFilter = null;
  state.rootFilter = null;
  state.dropTarget = targetPath;
  state.dropResults = [];
  state.dropPdfs = [];
  state.dropRunning = true;
  renderSidebarChrome();
  $("#subbar").style.display = "none";
  $("#view-title").textContent = "Quick Drop";
  $("#view-meta").textContent = "Auditing…";
  renderQuickDrop();

  const res = await api().start_quick_drop(
    targetPath,
    state.dropPdf,
    state.dropPdfDest,
    state.dropCustomPdfDir,
    state.dropWorkers,
    state.dropIgnoreCache
  );
  if (!res.ok) {
    state.dropRunning = false;
    showToast(res.error || "Could not start Quick Drop");
    renderQuickDrop();
  }
}

/* ==========================================================================
   FORENSIC INSPECTOR + REAL CANVAS SPECTROGRAM
   ========================================================================== */
async function selectLibraryTrack(trackId) {
  if (!api()) return;
  const res = await api().get_track_detail(trackId);
  if (!res.ok || !res.track) return;
  openInspectorWithTrack(res.track);
  $$(".track-row").forEach((r) =>
    r.classList.toggle("selected", Number(r.dataset.trackId) === Number(trackId))
  );
}

function openInspectorWithTrack(track) {
  state.selectedTrack = track;
  $("#app").classList.add("inspector-open");
  $("#inspector").classList.remove("closed");
  renderInspector(track);
}

function closeInspector() {
  state.selectedTrack = null;
  $("#app").classList.remove("inspector-open");
  $("#inspector").classList.add("closed");
  $$(".track-row").forEach((r) => r.classList.remove("selected"));
}

const CAUTIONARY_NOTE_RE = /\b(partial|limited|below|missing|warning|suspicious|unverified|borderline|reduced|no ultrasonic|no high|low ceiling|low spectral|inactive)\b/i;

function formatInspectorTitle(t) {
  const title = String(t.title || "").trim();
  const num = String(t.track_number || "").trim();
  if (!num) return title;
  const numInt = Number.parseInt(num, 10);
  if (Number.isFinite(numInt)) {
    const leadMatch = title.match(/^\s*0*(\d{1,3})\s*[-._)\u2013\u2014]/);
    if (leadMatch && Number.parseInt(leadMatch[1], 10) === numInt) {
      return title;
    }
  }
  return `${num} · ${title}`;
}

function formatInspectorSubtitle(t) {
  const artist = String(t.album_artist || t.artist || "").trim();
  const album = String(t.album_title || "").trim();
  if (artist && album) return `${artist} — ${album}`;
  return artist || album || "";
}

function resolveEvidenceDotColor(item, trackVerdictClass) {
  const lvl = String(item.level || "neutral");
  if (lvl === "bad") return "var(--bad)";
  if (lvl === "warn") return "var(--warn)";
  if (lvl === "neutral") return "var(--neutral)";
  if (trackVerdictClass === "bad" || trackVerdictClass === "warn") return "var(--neutral)";
  if (CAUTIONARY_NOTE_RE.test(String(item.text || ""))) return "var(--neutral)";
  return "var(--ok)";
}

function renderInspector(t) {
  if (!t) return;
  const cls = t.verdict_class || "neutral";
  const verdictColor = cls === "ok" ? "var(--ok)" : (cls === "warn" ? "var(--warn)" : (cls === "bad" ? "var(--bad)" : "var(--neutral)"));
  const isLibTrack = typeof t.id === "number";
  $("#btn-mark-reviewed").hidden = !isLibTrack;
  $("#lbl-reviewed").textContent = t.reviewed ? "Restore flag" : "Mark reviewed";

  const seed = `${t.album_artist || t.artist || ""}-${t.album_title || ""}`;
  const md5 = String(t.md5_status || "");
  const md5Label = md5 === "match" ? "MD5 ✓" : (md5 === "mismatch" ? "MD5 ✕" : (md5 || "—"));
  const md5Col = md5 === "match" ? "var(--ok)" : (md5 === "mismatch" ? "var(--bad)" : "var(--text-secondary)");
  const arObj = t.accuraterip;
  const arLabel = arObj && arObj.aplica
    ? `v1 ${arObj.v1 || "—"} / v2 ${arObj.v2 || "—"}`
    : "—";
  const evidence = Array.isArray(t.evidence) ? t.evidence : [];
  const nyqKhz = t.sample_rate ? (Number(t.sample_rate) / 2000).toFixed(1) : "22.1";
  const inspTitle = formatInspectorTitle(t);
  const inspSub = formatInspectorSubtitle(t);

  $("#insp-body").innerHTML = `
    <div class="insp-track-head">
      <div class="insp-cover">${renderCoverHTML(t.cover_hash, seed)}</div>
      <div style="min-width:0">
        <div class="insp-title">${esc(inspTitle)}</div>
        ${inspSub ? `<div class="insp-sub">${esc(inspSub)}</div>` : ""}
      </div>
    </div>

    <div class="verdict-card">
      <div class="verdict-row">
        <span class="verdict-word" style="color:${verdictColor}">${esc(t.verdict || "INDETERMINATE")}</span>
        <span class="verdict-score">${t.score != null ? Math.round(Number(t.score)) : "—"}<small> /100</small></span>
      </div>
      <div class="specs-grid">
        <div><span class="spec-k">Depth</span><span class="spec-v">${t.bit_depth ? `${t.bit_depth}-bit` : "—"}</span></div>
        <div><span class="spec-k">Rate</span><span class="spec-v">${esc(formatRateFull(t.sample_rate))}</span></div>
        <div><span class="spec-k"><span class="tech-term" data-tip="${esc(TECH_GLOSSARY.DR)}">DR</span></span><span class="spec-v">${t.dr != null ? `DR ${Math.round(Number(t.dr))}` : "—"}</span></div>
        <div><span class="spec-k">Integrity</span><span class="spec-v" style="color:${md5Col}">${esc(md5Label)}</span></div>
      </div>
    </div>

    <div class="spec-section">
      <div class="section-head">
        <span class="section-title">Interactive Spectrogram</span>
        <div class="spec-controls">
          <button class="mini-pill ${state.specZoom === "full" ? "active" : ""}" data-zoom="full">Full</button>
          <button class="mini-pill ${state.specZoom === "high" ? "active" : ""}" data-zoom="high">Upper</button>
          <button class="mini-pill ${state.specOverlay ? "active" : ""}" id="btn-overlay" title="Show/hide forensic markers and CD Nyquist reference">Markers</button>
          <button class="mini-pill icon-pill" id="btn-spec-expand" title="Expand spectrogram">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
          </button>
        </div>
      </div>
      <div class="spec-frame" id="spec-frame">
        <canvas class="spec-canvas" id="spec-canvas" width="712" height="352" title="Click to expand spectrogram"></canvas>
        <div class="spec-loading" id="spec-loading">Rendering spectrogram…</div>
        <div class="spec-readout">
          <span id="spec-cursor">Hover to inspect Hz / dBFS</span>
          <span id="spec-range">0.0 – ${nyqKhz} kHz</span>
        </div>
      </div>
    </div>

    <div class="section-head"><span class="section-title">Forensic Evidence</span></div>
    <div class="evidence-list">
      ${evidence.length ? evidence.map((e) => {
        const c = resolveEvidenceDotColor(e, cls);
        return `<div class="ev-item"><span class="ev-dot" style="background:${c}"></span><span>${formatEvidenceText(e.text)}</span></div>`;
      }).join("") : `<div class="ev-item"><span class="ev-dot" style="background:var(--ok)"></span><span>No forensic defects detected.</span></div>`}
    </div>

    <div class="section-head"><span class="section-title">Telemetry</span></div>
    <div class="kv-table">
      <div class="kv-row"><span>Spectral ceiling</span><span>${t.ceiling_hz ? (Number(t.ceiling_hz) / 1000).toFixed(2) + " kHz" : "—"}</span></div>
      <div class="kv-row"><span>Primary finding</span><span>${esc(t.primary_finding || "—")}</span></div>
      <div class="kv-row"><span><span class="tech-term" data-tip="${esc(TECH_GLOSSARY.AccurateRip)}">AccurateRip CRC</span></span><span>${esc(arLabel)}</span></div>
      <div class="kv-row"><span>Album coherence</span><span>${esc((t.album_coherence || "—").split("·")[0])}</span></div>
      <div class="kv-row"><span>Engine cache</span><span>v${esc(t.engine_version || state.engineVersion)} · SQLite</span></div>
    </div>`;

  attachCoverFallbacks($("#insp-body"));

  $$("[data-zoom]").forEach((b) => {
    b.onclick = () => {
      state.specZoom = b.dataset.zoom;
      renderInspector(t);
    };
  });
  $("#btn-overlay").onclick = () => {
    state.specOverlay = !state.specOverlay;
    renderInspector(t);
  };
  $("#btn-spec-expand").onclick = () => openSpectrogramLightbox(t);
  $("#spec-canvas").onclick = () => openSpectrogramLightbox(t);

  loadAndDrawSpectrogram(t);
}

function updateMarkersButtonState(btn, markers) {
  if (!btn) return;
  const hasMarkers = Array.isArray(markers) && markers.length > 0;
  btn.disabled = !hasMarkers;
  if (!hasMarkers) {
    btn.classList.remove("active");
    btn.title = "No forensic markers on this track";
  } else {
    btn.classList.toggle("active", Boolean(state.specOverlay));
    btn.title = "Show/hide forensic markers and CD Nyquist reference";
  }
}

function prefetchNextTrackSpectrogram(currentTrack) {
  if (!api() || !currentTrack || !state.tracks.length) return;
  if (state.libraryStatus.state !== "idle" || state.dropRunning) return;
  const idx = state.tracks.findIndex((r) => r.id === currentTrack.id);
  if (idx < 0 || idx + 1 >= state.tracks.length) return;
  const next = state.tracks[idx + 1];
  if (!next || !next.file_path || state.specCache.has(next.file_path)) return;
  try {
    api().prefetch_spectrogram(next.file_path, typeof next.id === "number" ? next.id : null);
  } catch (_err) {
    // Best-effort silent cache warming.
  }
}

function openSpectrogramLightbox(track) {
  const t = track || state.selectedTrack;
  if (!t) return;
  const spec = state.specCache.get(t.file_path || "");
  if (!spec || !spec.ok) {
    showToast("Wait for the spectrogram to finish loading first");
    return;
  }
  state.lightboxSpecData = spec;
  $("#lb-spec-title").textContent = formatInspectorTitle(t);
  $("#lb-spec-sub").textContent = formatInspectorSubtitle(t);
  const fmtBits = t.bit_depth ? `${t.bit_depth}-bit` : "";
  const fmtSr = t.sample_rate ? formatRateFull(t.sample_rate) : "";
  $("#lb-spec-format").textContent = [fmtBits, fmtSr].filter(Boolean).join(" · ");
  $("#spec-modal").classList.add("open");
  drawLightboxSpectrogram();
}

function closeSpectrogramLightbox() {
  $("#spec-modal").classList.remove("open");
}

function drawLightboxSpectrogram() {
  const spec = state.lightboxSpecData;
  if (!spec || !spec.ok) return;
  $("#lb-zoom-full").classList.toggle("active", state.specZoom === "full");
  $("#lb-zoom-high").classList.toggle("active", state.specZoom === "high");
  updateMarkersButtonState($("#lb-markers-btn"), spec.markers);
  const canvas = $("#lb-spec-canvas");
  const rect = canvas.getBoundingClientRect();
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  if (rect.width > 100 && rect.height > 100) {
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
  }
  renderInteractiveSpectrogram(
    canvas,
    $("#lb-spec-cursor"),
    $("#lb-spec-range"),
    spec,
    { zoom: state.specZoom, overlay: state.specOverlay, axes: true }
  );
}

async function loadAndDrawSpectrogram(track) {
  const canvas = $("#spec-canvas");
  const loading = $("#spec-loading");
  const cursor = $("#spec-cursor");
  const range = $("#spec-range");
  if (!canvas || !loading) return;

  const cacheKey = track.file_path || "";
  if (cacheKey && state.specCache.has(cacheKey)) {
    const cached = state.specCache.get(cacheKey);
    loading.hidden = true;
    updateMarkersButtonState($("#btn-overlay"), cached.markers);
    renderInteractiveSpectrogram(canvas, cursor, range, cached, {
      zoom: state.specZoom,
      overlay: state.specOverlay,
      axes: false,
    });
    prefetchNextTrackSpectrogram(track);
    return;
  }

  const seq = ++state.specRequestSeq;
  loading.hidden = false;
  loading.textContent = "Rendering spectrogram…";
  if (!api()) {
    loading.textContent = "Spectrogram unavailable offline";
    return;
  }

  let res = null;
  try {
    res = await api().get_spectrogram(
      track.file_path || "",
      typeof track.id === "number" ? track.id : null,
      track.raw_result || null,
      seq
    );
  } catch (err) {
    if (seq !== state.specRequestSeq) return;
    loading.textContent = "Could not load spectrogram (audio file inaccessible)";
    return;
  }
  if (seq !== state.specRequestSeq) return;
  if (res && res.request_id != null && Number(res.request_id) !== seq) return;
  if (res && res.cancelled) return;
  if (!res || !res.ok) {
    loading.textContent = (res && res.error) || "Audio file offline or inaccessible";
    return;
  }
  if (cacheKey) {
    if (state.specCache.size > 40) {
      const oldest = state.specCache.keys().next().value;
      state.specCache.delete(oldest);
    }
    state.specCache.set(cacheKey, res);
  }
  loading.hidden = true;
  updateMarkersButtonState($("#btn-overlay"), res.markers);
  renderInteractiveSpectrogram(canvas, cursor, range, res, {
    zoom: state.specZoom,
    overlay: state.specOverlay,
    axes: false,
  });
  prefetchNextTrackSpectrogram(track);
}

/* ==========================================================================
   LIVE BACKEND EVENTS & NATIVE DRAG-AND-DROP
   ========================================================================== */
function normalizeQuickDropResult(ev, albumFolder) {
  const meta = ev.meta || {};
  const esp = ev.esp || {};
  const md5 = ev.md5 || {};
  const verdict = md5.estado === "mismatch"
    ? "CORRUPT MD5"
    : (ev.error ? "ERROR" : String(ev.veredicto || "indeterminate"));
  const clsMap = {
    "GENUINE LOSSLESS": "ok",
    "PROBABLY LOSSLESS": "ok",
    "SUSPICIOUS": "warn",
    "PROBABLE UPSCALE": "bad",
    "CORRUPT MD5": "bad",
    "ERROR": "bad",
  };
  const evidence = [];
  if (ev.error) evidence.push({ level: "bad", text: String(ev.error) });
  (ev.problemas || []).forEach((p) => evidence.push({ level: "bad", text: String(p) }));
  (ev.notes || []).forEach((n) => evidence.push({ level: "ok", text: String(n) }));

  const filename = String(ev.archivo || "");
  const finding = md5.estado === "mismatch"
    ? "Corrupt MD5"
    : (esp.mdct_lattice_detected ? String(esp.mdct_lattice_family || "MDCT").split("(")[0].trim()
      : (esp.muro_brickwall_nyquist ? `Brickwall ${((esp.techo_hz || 24000) / 1000).toFixed(1)} kHz`
        : (esp.corte_artificial ? `Cut-off ${((esp.frecuencia_corte || esp.techo_hz || 0) / 1000).toFixed(1)} kHz`
          : (verdict === "GENUINE LOSSLESS" ? "Clean" : verdict))));

  return {
    id: `qd-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    file_path: ev.ruta || "",
    filename,
    title: filename.replace(/\.flac$/i, ""),
    album_title: albumFolder ? albumFolder.split(/[\\/]/).pop() : "Quick Drop",
    album_artist: "",
    track_number: (filename.match(/^\s*(\d+)/) || ["", ""])[1].padStart(2, "0"),
    verdict,
    verdict_class: clsMap[verdict] || "neutral",
    score: typeof ev.score === "number" ? Math.round(ev.score * 1000) / 10 : null,
    bit_depth: meta.bits_per_sample || null,
    sample_rate: meta.sample_rate || null,
    dr: ev.dr ?? null,
    ceiling_hz: esp.techo_hz ?? null,
    md5_status: md5.estado || "",
    accuraterip: ev.accuraterip || null,
    primary_finding: finding,
    evidence,
    raw_result: ev,
    engine_version: state.engineVersion,
    reviewed: false,
  };
}

window.__flacOnEvent = async (ev) => {
  if (!ev || typeof ev !== "object") return;
  if (ev.lane === "library") {
    state.libraryStatus = {
      state: ev.state || state.libraryStatus.state,
      paused: Boolean(ev.paused),
      queued_albums: ev.queued_albums ?? state.libraryStatus.queued_albums,
      total_albums: ev.total_albums ?? state.libraryStatus.total_albums,
      done_albums: ev.done_albums ?? state.libraryStatus.done_albums,
      current_album: ev.current_album ?? state.libraryStatus.current_album,
      current_album_path: ev.current_album_path ?? state.libraryStatus.current_album_path,
      current_track_file: ev.current_track_file ?? ev.file ?? state.libraryStatus.current_track_file,
      current_track_index: ev.current_track_index ?? state.libraryStatus.current_track_index,
      current_track_total: ev.current_track_total ?? state.libraryStatus.current_track_total,
      active_workers: ev.active_workers ?? state.libraryStatus.active_workers,
      up_next: Array.isArray(ev.up_next) ? ev.up_next : state.libraryStatus.up_next,
    };
    renderIndexerFooter();

    if (ev.event === "album_done") {
      await refreshOverview();
      await refreshCurrentView();
    } else if (ev.event === "finished" || ev.event === "cancelled") {
      await refreshOverview();
      await refreshCurrentView();
      showToast(
        ev.event === "cancelled"
          ? "Library scan cancelled · completed albums saved"
          : "Library scan complete"
      );
    } else if (ev.event === "up_to_date") {
      showToast("All albums in folder are already up to date");
    }
  } else if (ev.lane === "quick_drop") {
    if (ev.event === "track_result" && ev.result) {
      state.dropResults.push(normalizeQuickDropResult(ev.result, ev.album_folder));
      if (state.view === "drop") renderQuickDrop();
    } else if (ev.event === "pdf_report" && ev.ruta) {
      state.dropPdfs.push(ev.ruta);
    } else if (ev.event === "finished" || ev.event === "cancelled" || ev.event === "error") {
      state.dropRunning = false;
      if (api()) {
        const boot = await api().bootstrap();
        state.recentQuickDrops = boot.recent_quick_drops || state.recentQuickDrops;
      }
      if (state.view === "drop") {
        $("#view-meta").textContent = "One-off forensic audit";
        renderQuickDrop();
      }
      if (ev.event === "finished") {
        const pdfNote = state.dropPdfs.length ? ` · ${state.dropPdfs.length} PDF saved` : "";
        showToast(`Audit finished · ${ev.track_count || 0} tracks${pdfNote}`);
      } else if (ev.event === "cancelled") {
        showToast("Quick Drop cancelled");
      } else if (ev.errors && ev.errors.length) {
        showToast(ev.errors[0]);
      }
    }
  } else if (ev.lane === "pdf" && ev.event === "pdf_done") {
    if (ev.ok && ev.pdf_path) {
      showToast(`Saved ${ev.pdf_path.split(/[\\/]/).pop()}`);
      if (api()) api().open_external_path(ev.pdf_path);
    } else {
      showToast(ev.error || "Could not generate PDF report");
    }
  } else if (ev.lane === "updater") {
    if (ev.event === "update_available") {
      state.updateInfo = ev.info;
      renderSidebarChrome();
      renderSettingsModal();
    } else if (ev.event === "download_started") {
      $("#upd-progress-wrap").hidden = false;
      $("#upd-progress-bar").style.width = "6%";
      $("#upd-sub").textContent = `Downloading v${ev.version} & verifying SHA-256…`;
      $("#btn-install-update").disabled = true;
    } else if (ev.event === "download_progress") {
      const pct = ev.total > 0 ? Math.min(99, Math.round((ev.downloaded / ev.total) * 100)) : 50;
      $("#upd-progress-wrap").hidden = false;
      $("#upd-progress-bar").style.width = `${pct}%`;
      $("#upd-sub").textContent = `Downloading update… ${pct}%`;
    } else if (ev.event === "installing") {
      $("#upd-progress-bar").style.width = "100%";
      $("#upd-sub").textContent = `SHA-256 verified · launching installer for v${ev.version}…`;
      showToast(`Verified v${ev.version} · restarting to finish update`);
    } else if (ev.event === "download_error") {
      $("#upd-progress-wrap").hidden = true;
      $("#btn-install-update").disabled = false;
      $("#upd-sub").textContent = ev.error || "Update download failed";
      showToast(ev.error || "Update verification failed");
    }
  }
};

window.__flacOnNativeDrop = async (paths) => {
  dragDepth = 0;
  $("#drop-scrim").hidden = true;
  if (!Array.isArray(paths) || !paths.length) return;
  const first = paths[0];
  await launchQuickDrop(first);
};

/* ==========================================================================
   NAVIGATION, ACTIONS, COMMAND PALETTE & KEYBOARD SHORTCUTS
   ========================================================================== */
function navigateView(viewName) {
  state.view = viewName;
  state.smartFilter = null;
  state.rootFilter = null;
  state.albumId = null;
  state.pill = "all";
  updatePillUI();
  refreshCurrentView();
}

async function triggerAddLibraryFolder() {
  if (!api()) return;
  const picked = await api().pick_folder("");
  if (!picked || !picked.ok || !picked.path) return;
  openAddRootModal(picked.path);
}

function openAddRootModal(folderPath) {
  state.addRootDraft = {
    path: String(folderPath || ""),
    workers: Number(state.settings.workers || 0),
    pdf: Boolean(state.settings.library_pdf),
    pdf_dest_mode: state.settings.pdf_dest_mode === "custom" ? "custom" : "album",
    custom_pdf_dir: String(state.settings.custom_pdf_dir || ""),
    no_cache: Boolean(state.settings.library_no_cache),
  };
  renderAddRootModal();
  $("#add-root-modal").classList.add("open");
}

function closeAddRootModal() {
  $("#add-root-modal").classList.remove("open");
}

function renderAddRootModal() {
  const d = state.addRootDraft;
  const pathEl = $("#add-root-path");
  pathEl.textContent = d.path || "No folder selected";
  pathEl.title = d.path || "";
  $("#sel-add-root-workers").innerHTML = buildWorkerOptionsHTML(d.workers);
  $("#chk-add-root-pdf").checked = Boolean(d.pdf);
  $("#chk-add-root-nocache").checked = Boolean(d.no_cache);
  $("#add-root-pdf-card").hidden = !d.pdf;
  $$("input[name='add-root-pdf-dest']").forEach((r) => {
    r.checked = r.value === d.pdf_dest_mode;
  });
  $("#add-root-pdf-dir-row").hidden = !d.pdf || d.pdf_dest_mode !== "custom";
  $("#inp-add-root-pdf-dir").value = d.custom_pdf_dir || "";
}

async function confirmAddLibraryFolder() {
  if (!api()) return;
  const d = state.addRootDraft;
  if (!d.path) {
    showToast("Choose a library folder first");
    return;
  }
  if (d.pdf && d.pdf_dest_mode === "custom" && !d.custom_pdf_dir.trim()) {
    showToast("Select a custom destination folder for PDF reports");
    return;
  }
  const btn = $("#btn-confirm-add-root");
  btn.disabled = true;
  try {
    const res = await api().add_library_folder(d.path, {
      workers: Number(d.workers || 0),
      pdf: Boolean(d.pdf),
      pdf_dest_mode: d.pdf_dest_mode,
      custom_pdf_dir: d.custom_pdf_dir.trim(),
      no_cache: Boolean(d.no_cache),
    });
    if (!res || res.cancelled) return;
    if (!res.ok) {
      showToast(res.error || "Could not add folder");
      return;
    }
    closeAddRootModal();
    state.roots = res.roots || state.roots;
    state.libraryStatus = res.library_status || state.libraryStatus;
    if (res.settings) {
      state.settings = { ...state.settings, ...res.settings };
      state.dropWorkers = Number(state.settings.workers || 0);
      state.dropPdfDest = state.settings.pdf_dest_mode || "album";
      state.dropCustomPdfDir = state.settings.custom_pdf_dir || "";
    }
    renderSidebarChrome();
    const q = res.scan ? res.scan.queued : 0;
    showToast(q > 0 ? `Indexing ${q} album${q === 1 ? "" : "s"} in background…` : "Folder added · already up to date");
    await refreshCurrentView();
  } finally {
    btn.disabled = false;
  }
}

function openScanActivityModal() {
  renderScanActivityModal();
  $("#activity-modal").classList.add("open");
}

function closeScanActivityModal() {
  $("#activity-modal").classList.remove("open");
}

function renderScanActivityModal() {
  const st = state.libraryStatus || {};
  const active = st.state === "scanning" || st.state === "pausing" || st.state === "paused";
  const dot = $("#act-dot");
  dot.classList.remove("scanning", "paused");
  if (st.state === "scanning" || st.state === "pausing") dot.classList.add("scanning");
  else if (st.state === "paused") dot.classList.add("paused");

  const stateLabel = st.state === "scanning"
    ? "Scanning"
    : (st.state === "pausing" ? "Pausing" : (st.state === "paused" ? "Paused" : "Idle"));
  $("#act-state-badge").textContent = stateLabel;

  const actRescan = $("#act-btn-rescan");
  const actStop = $("#act-btn-stop");
  if (st.state === "scanning") {
    actRescan.textContent = "Pause";
    actStop.hidden = false;
  } else if (st.state === "paused" || st.state === "pausing") {
    actRescan.textContent = "Resume";
    actStop.hidden = false;
  } else {
    actRescan.textContent = "Rescan library";
    actStop.hidden = true;
  }

  const total = Math.max(1, Number(st.total_albums || 1));
  const done = Number(st.done_albums || 0);
  const tTot = Math.max(1, Number(st.current_track_total || 1));
  const tCur = Number(st.current_track_index || 0);
  const pct = active
    ? Math.min(99, Math.max(4, Math.round(((done + tCur / tTot) / total) * 100)))
    : 100;
  const upNext = Array.isArray(st.up_next) ? st.up_next : [];
  const workers = Number(st.active_workers || state.settings.workers || Math.min(8, state.cpuCount));

  const summaryHTML = active ? `
    <div class="act-summary-card">
      <div class="drop-pdf-kicker">Currently scanning</div>
      <div class="act-album-title">${esc(st.current_album || "Discovering albums…")}</div>
      ${st.current_album_path ? `<div class="act-path" title="${esc(st.current_album_path)}">${esc(st.current_album_path)}</div>` : ""}
      <div class="act-track-row">
        <span class="act-track-name" title="${esc(st.current_track_file || "")}">
          Track ${Math.min(tCur, tTot)}/${tTot}${st.current_track_file ? ` · ${esc(st.current_track_file)}` : ""}
        </span>
        <span>${pct}%</span>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
      <div class="act-meta-pills">
        <span class="act-pill">Album ${Math.min(done + 1, total)} of ${total}</span>
        <span class="act-pill">${workers} worker${workers === 1 ? "" : "s"}</span>
        <span class="act-pill">${upNext.length} queued</span>
      </div>
    </div>` : `
    <div class="act-summary-card">
      <div class="act-album-title">${state.counts.albums ? "Library is up to date" : "No library folders indexed yet"}</div>
      <div class="settings-sub">${state.counts.albums} album${state.counts.albums === 1 ? "" : "s"} · ${state.counts.tracks} tracks · Engine ${esc(state.engineVersion)}</div>
    </div>`;

  const queueHTML = upNext.length ? `
    <div>
      <div class="drop-pdf-kicker" style="margin-bottom:6px">Up next (${upNext.length})</div>
      <div class="upnext-list">
        ${upNext.map((item, idx) => `
          <div class="upnext-row" title="${esc(item.folder_path || item.title)}">
            <span class="upnext-name"><span class="mono" style="color:var(--text-tertiary);margin-right:8px">${String(idx + 1).padStart(2, "0")}</span>${esc(item.title)}</span>
            <span class="mono" style="font-size:11px;color:var(--text-tertiary);flex-shrink:0">${item.track_count || 0} trk</span>
          </div>
        `).join("")}
      </div>
    </div>` : (active ? `<div class="settings-sub">Final album in current scan queue.</div>` : "");

  $("#act-body").innerHTML = summaryHTML + queueHTML;
}

function openSettings() {
  renderSettingsModal();
  $("#settings-modal").classList.add("open");
}

function closeSettings() {
  $("#settings-modal").classList.remove("open");
}

function renderSettingsModal() {
  $("#sel-settings-workers").innerHTML = buildWorkerOptionsHTML(state.settings.workers);
  $("#chk-settings-lib-pdf").checked = Boolean(state.settings.library_pdf);
  const dest = state.settings.pdf_dest_mode === "custom" ? "custom" : "album";
  $("#sel-settings-pdf-dest").value = dest;
  $("#settings-pdf-dir-row").hidden = dest !== "custom";
  $("#inp-settings-pdf-dir").value = state.settings.custom_pdf_dir || "";

  $("#chk-offline-mode").checked = Boolean(state.settings.offline_mode);
  const info = state.updateInfo;
  const installBtn = $("#btn-install-update");
  if (info && info.available) {
    $("#upd-title").textContent = `Update v${info.version} available`;
    $("#upd-sub").textContent = state.portable || info.is_portable
      ? "Opens the official GitHub Release page to download the new release."
      : "Downloads from GitHub Releases, verifies SHA-256, and preserves library.db.";
    installBtn.textContent = state.portable || info.is_portable ? "Open GitHub Release" : "Install & Restart";
    installBtn.hidden = false;
    installBtn.disabled = false;
  } else {
    $("#upd-title").textContent = `FLAC Verifier v${state.appVersion}`;
    $("#upd-sub").textContent = state.settings.offline_mode
      ? "Offline mode enabled — automatic update checks are disabled."
      : "Verified via HTTPS + SHA-256 against official GitHub Releases.";
    installBtn.hidden = true;
  }
}

function openCmd() {
  $("#cmd-modal").classList.add("open");
  $("#cmd-input").value = "";
  state.cmdActiveIdx = 0;
  renderCmd("");
  $("#cmd-input").focus();
}

function closeCmd() {
  $("#cmd-modal").classList.remove("open");
}

function renderCmd(query) {
  const lq = String(query || "").toLowerCase().trim();
  const base = [
    { kind: "Action", label: "Add Music Library Folder…", meta: "Library", run: () => triggerAddLibraryFolder() },
    { kind: "Action", label: "Toggle Dark / Light Theme", meta: "Appearance", run: () => toggleTheme() },
    { kind: "Action", label: "Settings & Updates", meta: state.settings.offline_mode ? "Offline" : `v${state.appVersion}`, run: () => openSettings() },
    { kind: "View", label: "Open Albums", meta: `${state.counts.albums} albums`, run: () => navigateView("albums") },
    { kind: "View", label: "Open Needs Review Inbox", meta: `${state.counts.needs_review} flagged`, run: () => navigateView("needs_review") },
    { kind: "View", label: "Open Quick Drop", meta: "One-off audit", run: () => navigateView("drop") },
    { kind: "Action", label: "Run Incremental Library Scan", meta: `Engine ${state.engineVersion}`, run: () => api() && api().rescan_library(null, false) },
  ];
  const albumCmds = state.albums.slice(0, 20).map((a) => ({
    kind: "Album",
    label: `${a.artist} — ${a.title}`,
    meta: formatAlbumFormat(a.bit_depth, a.sample_rate),
    run: () => {
      state.view = "album_detail";
      state.albumId = a.id;
      state.smartFilter = null;
      state.pill = "all";
      updatePillUI();
      refreshCurrentView();
    },
  }));
  const trackCmds = state.tracks.slice(0, 20).map((t) => ({
    kind: "Track",
    label: t.title,
    meta: t.primary_finding || t.verdict,
    run: () => selectLibraryTrack(t.id),
  }));

  const items = [...base, ...albumCmds, ...trackCmds]
    .filter((i) => !lq || i.label.toLowerCase().includes(lq) || String(i.meta).toLowerCase().includes(lq))
    .slice(0, 10);

  state.cmdItems = items;
  state.cmdActiveIdx = Math.min(state.cmdActiveIdx, Math.max(0, items.length - 1));
  $("#cmd-results").innerHTML = items.map((i, idx) => `
    <button class="cmd-row ${idx === state.cmdActiveIdx ? "active" : ""}" data-cmd="${idx}">
      <span><strong>${esc(i.label)}</strong> <span style="color:var(--text-tertiary);margin-left:6px">${esc(i.kind)}</span></span>
      <span class="mono" style="font-size:11px;color:var(--text-tertiary)">${esc(i.meta)}</span>
    </button>
  `).join("");
  $$("[data-cmd]").forEach((b) => {
    b.onclick = () => {
      const item = state.cmdItems[Number(b.dataset.cmd)];
      if (item) item.run();
      closeCmd();
    };
  });
}

function bindStaticEvents() {
  $$(".nav-item[data-view]").forEach((btn) => {
    btn.onclick = () => navigateView(btn.dataset.view);
  });
  $$(".nav-item[data-smart]").forEach((btn) => {
    btn.onclick = () => {
      state.view = "tracks";
      state.smartFilter = btn.dataset.smart;
      state.rootFilter = null;
      state.albumId = null;
      state.pill = "all";
      updatePillUI();
      refreshCurrentView();
    };
  });

  $("#btn-add-root-cta").onclick = triggerAddLibraryFolder;
  $("#btn-add-folder").onclick = triggerAddLibraryFolder;

  $("#btn-back").onclick = () => {
    state.view = "albums";
    state.albumId = null;
    state.pill = "all";
    updatePillUI();
    refreshCurrentView();
  };

  $$("#filter-pills .seg-btn").forEach((b) => {
    b.onclick = () => {
      state.pill = b.dataset.pill;
      updatePillUI();
      refreshCurrentView();
    };
  });

  $$("#albums-view-toggle [data-albums-view]").forEach((b) => {
    b.onclick = () => {
      const next = b.dataset.albumsView === "list" ? "list" : "grid";
      if (state.albumsView === next) return;
      state.albumsView = next;
      state.settings.albums_view = next;
      updateAlbumsViewToggleUI();
      if (api()) api().save_preferences({ albums_view: next });
      if (state.view === "albums") renderAlbumsView();
    };
  });

  $("#sel-albums-sort").onchange = (e) => {
    const key = e.target.value || "artist";
    setAlbumsSort(key, Boolean(DEFAULT_ALBUM_SORT_DESC[key]));
  };
  $("#btn-albums-sort-dir").onclick = () => {
    setAlbumsSort(state.albumsSort, !state.albumsSortDesc);
  };

  // Expanded Spectrogram Lightbox bindings
  $("#btn-close-spec-modal").onclick = closeSpectrogramLightbox;
  $("#spec-modal").onclick = (e) => { if (e.target.id === "spec-modal") closeSpectrogramLightbox(); };
  $("#lb-zoom-full").onclick = () => {
    state.specZoom = "full";
    drawLightboxSpectrogram();
    if (state.selectedTrack) renderInspector(state.selectedTrack);
  };
  $("#lb-zoom-high").onclick = () => {
    state.specZoom = "high";
    drawLightboxSpectrogram();
    if (state.selectedTrack) renderInspector(state.selectedTrack);
  };
  $("#lb-markers-btn").onclick = () => {
    if ($("#lb-markers-btn").disabled) return;
    state.specOverlay = !state.specOverlay;
    drawLightboxSpectrogram();
    if (state.selectedTrack) renderInspector(state.selectedTrack);
  };

  // Scan Activity modal bindings
  $("#btn-scan-activity").onclick = (e) => {
    if (e.target.closest("#idx-btn") || e.target.closest("#idx-cancel")) return;
    openScanActivityModal();
  };
  $("#btn-scan-activity").onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openScanActivityModal();
    }
  };
  $("#btn-close-activity").onclick = closeScanActivityModal;
  $("#act-btn-close").onclick = closeScanActivityModal;
  $("#activity-modal").onclick = (e) => { if (e.target.id === "activity-modal") closeScanActivityModal(); };
  $("#act-btn-rescan").onclick = async () => {
    $("#idx-btn").click();
  };
  $("#act-btn-stop").onclick = async () => {
    if (api()) await api().cancel_library_scan();
  };

  // Add Library Folder modal bindings
  $("#btn-close-add-root").onclick = closeAddRootModal;
  $("#btn-cancel-add-root").onclick = closeAddRootModal;
  $("#add-root-modal").onclick = (e) => { if (e.target.id === "add-root-modal") closeAddRootModal(); };
  $("#btn-add-root-change").onclick = async () => {
    if (!api()) return;
    const picked = await api().pick_folder(state.addRootDraft.path);
    if (picked && picked.ok && picked.path) {
      state.addRootDraft.path = picked.path;
      renderAddRootModal();
    }
  };
  $("#sel-add-root-workers").onchange = (e) => {
    state.addRootDraft.workers = Number(e.target.value || 0);
  };
  $("#chk-add-root-pdf").onchange = (e) => {
    state.addRootDraft.pdf = e.target.checked;
    renderAddRootModal();
  };
  $("#chk-add-root-nocache").onchange = (e) => {
    state.addRootDraft.no_cache = e.target.checked;
  };
  $$("input[name='add-root-pdf-dest']").forEach((r) => {
    r.onchange = (e) => {
      state.addRootDraft.pdf_dest_mode = e.target.value === "custom" ? "custom" : "album";
      renderAddRootModal();
    };
  });
  $("#inp-add-root-pdf-dir").oninput = (e) => {
    state.addRootDraft.custom_pdf_dir = e.target.value.trim();
  };
  $("#btn-add-root-pick-pdf").onclick = async () => {
    if (!api()) return;
    const picked = await api().pick_folder(state.addRootDraft.custom_pdf_dir);
    if (picked && picked.ok && picked.path) {
      state.addRootDraft.custom_pdf_dir = picked.path;
      renderAddRootModal();
    }
  };
  $("#btn-confirm-add-root").onclick = confirmAddLibraryFolder;

  // Settings modal preferences bindings
  $("#sel-settings-workers").onchange = async (e) => {
    const val = Number(e.target.value || 0);
    state.settings.workers = val;
    state.dropWorkers = val;
    if (api()) await api().save_preferences({ workers: val });
  };
  $("#chk-settings-lib-pdf").onchange = async (e) => {
    const enabled = e.target.checked;
    state.settings.library_pdf = enabled;
    if (api()) await api().save_preferences({ library_pdf: enabled });
  };
  $("#sel-settings-pdf-dest").onchange = async (e) => {
    const mode = e.target.value === "custom" ? "custom" : "album";
    state.settings.pdf_dest_mode = mode;
    state.dropPdfDest = mode;
    renderSettingsModal();
    if (api()) await api().save_preferences({ pdf_dest_mode: mode });
  };
  $("#inp-settings-pdf-dir").onchange = async (e) => {
    const dir = e.target.value.trim();
    state.settings.custom_pdf_dir = dir;
    state.dropCustomPdfDir = dir;
    if (api()) await api().save_preferences({ custom_pdf_dir: dir });
  };
  $("#btn-settings-pick-pdf").onclick = async () => {
    if (!api()) return;
    const picked = await api().pick_folder(state.settings.custom_pdf_dir || "");
    if (picked && picked.ok && picked.path) {
      state.settings.custom_pdf_dir = picked.path;
      state.dropCustomPdfDir = picked.path;
      renderSettingsModal();
      await api().save_preferences({ custom_pdf_dir: picked.path });
    }
  };

  $("#search-input").addEventListener("input", (e) => {
    state.search = e.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => refreshCurrentView(), 110);
  });

  $("#btn-theme").onclick = toggleTheme;
  $("#btn-settings").onclick = openSettings;
  $("#btn-update-pill").onclick = openSettings;
  $("#btn-close-settings").onclick = closeSettings;
  $("#settings-modal").onclick = (e) => { if (e.target.id === "settings-modal") closeSettings(); };
  $("#chk-offline-mode").onchange = async (e) => {
    const enabled = e.target.checked;
    state.settings.offline_mode = enabled;
    if (enabled) state.updateInfo = null;
    if (api()) await api().set_offline_mode(enabled);
    renderSidebarChrome();
    renderSettingsModal();
    showToast(enabled ? "Offline mode enabled" : "Automatic update checks enabled");
  };
  $("#btn-check-updates").onclick = async () => {
    if (!api()) return;
    $("#upd-sub").textContent = "Checking GitHub Releases…";
    const res = await api().check_updates(true);
    if (res && res.available) {
      state.updateInfo = res;
      renderSidebarChrome();
      renderSettingsModal();
    } else if (res && res.ok) {
      $("#upd-sub").textContent = res.message || `You are up to date (v${state.appVersion}).`;
      showToast("FLAC Verifier is up to date");
    } else {
      $("#upd-sub").textContent = (res && res.error) || "Could not reach GitHub Releases";
    }
  };
  $("#btn-install-update").onclick = async () => {
    if (!api()) return;
    const res = await api().install_update();
    if (res && res.mode === "portable") {
      showToast("Opened official GitHub Release page");
      closeSettings();
    } else if (res && !res.ok) {
      showToast(res.error || "Update failed");
    }
  };
  $("#btn-close-insp").onclick = closeInspector;

  $("#btn-mark-reviewed").onclick = async () => {
    const t = state.selectedTrack;
    if (!t || typeof t.id !== "number" || !api()) return;
    const res = await api().set_track_reviewed(t.id, !t.reviewed, t.reviewed_note || "");
    if (res.ok) {
      state.counts = res.counts || state.counts;
      state.selectedTrack = { ...t, ...res.track };
      renderInspector(state.selectedTrack);
      await refreshCurrentView();
      showToast(state.selectedTrack.reviewed ? `Marked "${t.title}" as reviewed` : `Restored "${t.title}" to Needs Review`);
    }
  };

  $("#btn-pdf").onclick = async () => {
    const t = state.selectedTrack;
    if (!t || !api()) return;
    if (typeof t.album_id === "number") {
      showToast("Generating album PDF report…");
      await api().generate_album_pdf(t.album_id, "album", "");
    } else if (state.dropPdfs[0]) {
      await api().open_external_path(state.dropPdfs[0]);
    } else {
      showToast("Enable 'Generate PDF report' in Quick Drop to export PDFs");
    }
  };

  $("#btn-reveal").onclick = async () => {
    const t = state.selectedTrack;
    if (!t || !t.file_path || !api()) return;
    const res = await api().reveal_in_explorer(t.file_path);
    if (!res.ok) showToast(res.error || "Could not open Explorer");
  };

  $("#btn-export-csv").onclick = async () => {
    if (!api()) return;
    const viewKey = state.smartFilter || (state.view === "needs_review" ? "needs_review" : "all");
    const res = await api().export_tracks(
      "csv", viewKey, state.view === "album_detail" ? state.albumId : null,
      state.rootFilter, state.pill, state.search, null
    );
    if (res && res.ok) showToast(`Exported ${res.count} rows to CSV`);
    else if (res && !res.cancelled) showToast(res.error || "Export failed");
  };

  $("#idx-btn").onclick = async (e) => {
    e.stopPropagation();
    if (!api()) return;
    const st = state.libraryStatus.state;
    if (st === "scanning") await api().pause_library_scan();
    else if (st === "paused" || st === "pausing") await api().resume_library_scan();
    else {
      const res = await api().rescan_library(null, false);
      if (res && res.scan && res.scan.queued === 0) {
        showToast("Library already up to date");
      }
    }
  };
  $("#idx-cancel").onclick = async (e) => {
    e.stopPropagation();
    if (api()) await api().cancel_library_scan();
  };

  $("#btn-reverify-flagged").onclick = async () => {
    if (!api()) return;
    const res = await api().rescan_outdated(true);
    if (res.ok) showToast(`Queued ${res.queued} flagged album(s) for Engine ${state.engineVersion}`);
  };
  $("#btn-reverify-all").onclick = async () => {
    if (!api()) return;
    const res = await api().rescan_outdated(false);
    if (res.ok) showToast(`Queued ${res.queued} album(s) for Engine ${state.engineVersion}`);
  };
  $("#btn-dismiss-banner").onclick = () => {
    state.bannerDismissed = true;
    $("#engine-banner").hidden = true;
  };

  // Command palette
  $("#cmd-input").oninput = (e) => {
    state.cmdActiveIdx = 0;
    renderCmd(e.target.value);
  };
  $("#cmd-modal").onclick = (e) => { if (e.target.id === "cmd-modal") closeCmd(); };

  // Keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    const cmdOpen = $("#cmd-modal").classList.contains("open");
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      cmdOpen ? closeCmd() : openCmd();
      return;
    }
    if (cmdOpen) {
      if (e.key === "Escape") { closeCmd(); return; }
      if (e.key === "ArrowDown" && state.cmdItems.length) {
        e.preventDefault();
        state.cmdActiveIdx = (state.cmdActiveIdx + 1) % state.cmdItems.length;
        renderCmd($("#cmd-input").value);
      } else if (e.key === "ArrowUp" && state.cmdItems.length) {
        e.preventDefault();
        state.cmdActiveIdx = (state.cmdActiveIdx - 1 + state.cmdItems.length) % state.cmdItems.length;
        renderCmd($("#cmd-input").value);
      } else if (e.key === "Enter" && state.cmdItems[state.cmdActiveIdx]) {
        e.preventDefault();
        state.cmdItems[state.cmdActiveIdx].run();
        closeCmd();
      }
      return;
    }
    if (e.key === "Escape") {
      if ($("#spec-modal").classList.contains("open")) closeSpectrogramLightbox();
      else if ($("#activity-modal").classList.contains("open")) closeScanActivityModal();
      else if ($("#add-root-modal").classList.contains("open")) closeAddRootModal();
      else if ($("#settings-modal").classList.contains("open")) closeSettings();
      else if (state.selectedTrack) closeInspector();
      return;
    }
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && state.tracks.length && state.view !== "albums" && state.view !== "drop") {
      if (document.activeElement && document.activeElement.tagName === "INPUT") return;
      e.preventDefault();
      const curIdx = state.selectedTrack ? state.tracks.findIndex((r) => r.id === state.selectedTrack.id) : -1;
      const nextIdx = curIdx < 0
        ? 0
        : Math.max(0, Math.min(state.tracks.length - 1, curIdx + (e.key === "ArrowDown" ? 1 : -1)));
      selectLibraryTrack(state.tracks[nextIdx].id);
    }
  });

  // Visual drag-over overlay (actual full paths arrive via window.__flacOnNativeDrop)
  document.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth += 1;
    $("#drop-scrim").hidden = false;
  });
  document.addEventListener("dragover", (e) => { e.preventDefault(); });
  document.addEventListener("dragleave", () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) $("#drop-scrim").hidden = true;
  });
  document.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    $("#drop-scrim").hidden = true;
  });
}

/* ==========================================================================
   INITIALIZATION
   ========================================================================== */
async function initApp() {
  bindStaticEvents();
  bindGlobalTooltips();

  if (api()) {
    const boot = await api().bootstrap();
    if (boot && boot.ok) {
      state.appVersion = boot.app_version || state.appVersion;
      state.engineVersion = boot.engine_version || state.engineVersion;
      state.portable = Boolean(boot.portable);
      state.cpuCount = Number(boot.cpu_count || 8);
      state.settings = { ...state.settings, ...(boot.settings || {}) };
      state.counts = boot.counts || state.counts;
      state.roots = boot.roots || [];
      state.outdated = boot.outdated || state.outdated;
      state.libraryStatus = boot.library_status || state.libraryStatus;
      state.recentQuickDrops = boot.recent_quick_drops || [];
      state.updateInfo = boot.update_info || null;

      state.dropWorkers = Number(state.settings.workers || 0);
      state.dropPdfDest = state.settings.pdf_dest_mode || "album";
      state.dropCustomPdfDir = state.settings.custom_pdf_dir || "";
      state.albumsView = state.settings.albums_view === "list" ? "list" : "grid";
      state.albumsSort = state.settings.albums_sort || "artist";
      state.albumsSortDesc = Boolean(state.settings.albums_sort_desc);

      applyThemeUI(boot.theme || "dark");

      if (boot.initial_path) {
        await launchQuickDrop(boot.initial_path);
        return;
      }
    }
  }

  // Optional deep-link query params for deterministic inspection/audits
  const qp = new URLSearchParams(window.location.search);
  if (qp.get("theme") === "light") applyThemeUI("light");
  if (qp.get("albums_view") === "list") state.albumsView = "list";
  else if (qp.get("albums_view") === "grid") state.albumsView = "grid";
  if (qp.get("albums_sort")) state.albumsSort = qp.get("albums_sort");
  if (qp.get("pdf") === "1") state.dropPdf = true;
  if (qp.get("pdf_dest") === "custom") {
    state.dropPdf = true;
    state.dropPdfDest = "custom";
    if (!state.dropCustomPdfDir) state.dropCustomPdfDir = "D:\\Reports\\FLAC_Verifier";
  }
  if (qp.get("album")) {
    state.view = "album_detail";
    state.albumId = Number(qp.get("album"));
  } else if (qp.get("view")) {
    state.view = qp.get("view");
  }

  await refreshCurrentView();
  if (qp.get("track")) {
    await selectLibraryTrack(Number(qp.get("track")));
  }
  if (qp.get("modal") === "add_root") {
    const samplePath = qp.get("folder") || "Z:\\Rips_FLAC_Verifier";
    openAddRootModal(samplePath);
    if (qp.get("modal_pdf") === "1") {
      state.addRootDraft.pdf = true;
      state.addRootDraft.pdf_dest_mode = "custom";
      state.addRootDraft.custom_pdf_dir = "D:\\Reports\\FLAC_Verifier";
      state.addRootDraft.workers = 4;
      renderAddRootModal();
    }
  } else if (qp.get("modal") === "lightbox") {
    setTimeout(() => openSpectrogramLightbox(state.selectedTrack), 900);
  } else if (qp.get("modal") === "activity") {
    state.libraryStatus = {
      state: "scanning",
      paused: false,
      queued_albums: 4,
      total_albums: 10,
      done_albums: 1,
      current_album: "Alma de Bolero Disco 1",
      current_album_path: "Z:\\Rips_FLAC_Verifier\\Alma de Bolero Disco 1",
      current_track_file: "07 - Historia de un Amor.flac",
      current_track_index: 7,
      current_track_total: 20,
      active_workers: 4,
      up_next: [
        { title: "Alma de Bolero Disco 2", folder_path: "Z:\\Rips_FLAC_Verifier\\Alma de Bolero Disco 2", track_count: 18 },
        { title: "Steven Wilson - Grace for Drowning (24-96)", folder_path: "Z:\\Rips_FLAC_Verifier\\Grace for Drowning", track_count: 12 },
        { title: "Pink Floyd - Wish You Were Here", folder_path: "Z:\\Rips_FLAC_Verifier\\Wish You Were Here", track_count: 5 },
        { title: "Miles Davis - Kind of Blue", folder_path: "Z:\\Rips_FLAC_Verifier\\Kind of Blue", track_count: 6 },
      ],
    };
    renderIndexerFooter();
    openScanActivityModal();
  }
}

if (window.pywebview && window.pywebview.api) {
  initApp();
} else {
  window.addEventListener("pywebviewready", initApp, { once: true });
}
