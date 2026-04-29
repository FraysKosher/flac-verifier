import { useState, useEffect, useMemo, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import { openPath } from "@tauri-apps/plugin-opener";
import { useLang } from "../context/LanguageContext";
import type { Lang } from "../i18n";
import type { HistoryRow } from "../types";

// ── Verdict translation map ──────────────────────────────────────────────────
// Verdicts are always stored in Spanish in historial.csv.
// This map translates them for display without changing stored data.
const VERDICT_MAP: Record<string, Record<Lang, string>> = {
  "LOSSLESS GENUINO":      { en: "LOSSLESS GENUINE",   es: "LOSSLESS GENUINO" },
  "PROBABLEMENTE LOSSLESS":{ en: "PROBABLY LOSSLESS",  es: "PROBABLEMENTE LOSSLESS" },
  "DUDOSO":                { en: "DOUBTFUL",           es: "DUDOSO" },
  "PROBABLE UPSCALE":      { en: "PROBABLE UPSCALE",   es: "PROBABLE UPSCALE" },
  "indeterminado":         { en: "INCONCLUSIVE",       es: "INDETERMINADO" },
};

function translateVerdict(raw: string, lang: Lang): string {
  return VERDICT_MAP[raw]?.[lang] ?? raw;
}

// ── Lightweight CSV parser (handles quoted fields, BOM, CRLF) ───────────────
function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let cur = "";
  let inQ = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (inQ && line[i + 1] === '"') { cur += '"'; i++; }
      else { inQ = !inQ; }
    } else if (ch === "," && !inQ) { fields.push(cur); cur = ""; }
    else { cur += ch; }
  }
  fields.push(cur);
  return fields;
}

function parseCsv(raw: string): HistoryRow[] {
  const lines = raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim().split("\n");
  if (lines.length < 2) return [];
  const headerLine = lines[0].replace(/^\uFEFF/, "");
  const headers = parseCsvLine(headerLine);
  return lines.slice(1).map((line) => {
    const vals = parseCsvLine(line);
    const row: Record<string, string> = {};
    headers.forEach((h, i) => { row[h] = vals[i] ?? ""; });
    return row as unknown as HistoryRow;
  });
}

// ── Badge for verdict column ────────────────────────────────────────────────
// `raw` = Spanish key for colour matching; `display` = translated label
function VerdictPill({ raw, display }: { raw: string; display: string }) {
  const cls =
    raw.includes("GENUINO")  || raw.includes("GENUINE")  ? "badge-genuine" :
    raw.includes("PROBABLE") && !raw.includes("UPSCALE")  ? "badge-probable" :
    raw.includes("DUDOSO")   || raw.includes("DOUBTFUL")  ? "badge-doubtful" :
    raw.includes("UPSCALE")                               ? "badge-upscale"  : "text-muted";
  return <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${cls}`}>{display}</span>;
}

// ── Sort helper ─────────────────────────────────────────────────────────────
type SortDir = "asc" | "desc";

function sortRows(rows: HistoryRow[], col: keyof HistoryRow, dir: SortDir): HistoryRow[] {
  return [...rows].sort((a, b) => {
    const av = a[col] ?? "";
    const bv = b[col] ?? "";
    const cmp = av < bv ? -1 : av > bv ? 1 : 0;
    return dir === "asc" ? cmp : -cmp;
  });
}

// ── Component ───────────────────────────────────────────────────────────────
export function HistoryTab() {
  const { t, lang } = useLang();
  const [rows, setRows]       = useState<HistoryRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);
  const [sortCol, setSortCol] = useState<keyof HistoryRow>("fecha");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [search, setSearch]   = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const raw = await invoke<string>("read_history");
      setRows(raw ? parseCsv(raw) : []);
    } catch (e) { setError(String(e)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSort = (col: keyof HistoryRow) => {
    if (col === sortCol) setSortDir((d) => d === "asc" ? "desc" : "asc");
    else { setSortCol(col); setSortDir("asc"); }
  };

  const handleClear = async () => {
    if (!window.confirm(t("clearHistoryConfirm"))) return;
    try { await invoke("clear_history"); setRows([]); }
    catch (e) { setError(String(e)); }
  };

  const handleOpenCsv = async () => {
    try {
      const p = await invoke<string>("get_history_path");
      await openPath(p);
    } catch (e) { setError(String(e)); }
  };

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    const base = q
      ? rows.filter((r) => Object.values(r).some((v) => v.toLowerCase().includes(q)))
      : rows;
    return sortRows(base, sortCol, sortDir);
  }, [rows, search, sortCol, sortDir]);

  const Th = ({ label, col }: { label: string; col: keyof HistoryRow }) => (
    <th
      onClick={() => handleSort(col)}
      className="cursor-pointer select-none whitespace-nowrap px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wider text-muted hover:text-white transition-colors"
    >
      {label}
      {sortCol === col && <span className="ml-1 opacity-60">{sortDir === "asc" ? "↑" : "↓"}</span>}
    </th>
  );

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-5 py-3 border-b border-border shrink-0">
        <input
          type="text"
          placeholder="Filter…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 max-w-xs rounded-md bg-surface2 border border-border px-3 py-1.5 text-xs text-white placeholder-muted outline-none focus:border-accent/50 transition-colors"
        />
        <span className="text-xs text-muted ml-1">{filtered.length} rows</span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={load}
            className="rounded-md bg-surface2 border border-border px-3 py-1.5 text-xs text-muted hover:text-white hover:border-accent/40 transition-all"
          >
            ↺ {t("refreshHistory")}
          </button>
          <button
            onClick={handleOpenCsv}
            disabled={rows.length === 0}
            className="rounded-md bg-surface2 border border-border px-3 py-1.5 text-xs text-accent hover:border-accent/40 hover:bg-accent/5 transition-all disabled:opacity-30 disabled:cursor-not-allowed"
          >
            📊 {t("openHistoryCsv")}
          </button>
          <button
            onClick={handleClear}
            disabled={rows.length === 0}
            className="rounded-md bg-upscale/10 border border-upscale/20 px-3 py-1.5 text-xs text-upscale hover:bg-upscale/20 transition-all disabled:opacity-30 disabled:cursor-not-allowed"
          >
            🗑 {t("clearHistory")}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="mx-5 mt-3 rounded-md bg-upscale/10 border border-upscale/20 px-3 py-2 text-xs text-upscale">
          {error}
        </div>
      )}

      {/* Body */}
      {loading ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted animate-pulse">
          Loading…
        </div>
      ) : rows.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center px-8">
          <span className="text-4xl opacity-20">📋</span>
          <p className="text-base font-semibold text-white/60">{t("historyEmpty")}</p>
          <p className="text-xs text-muted max-w-sm">{t("historyEmptyDesc")}</p>
        </div>
      ) : (
        <div className="flex-1 overflow-auto">
          <table className="w-full border-collapse text-xs">
            <thead className="sticky top-0 z-10 bg-surface border-b border-border">
              <tr>
                <Th label={t("histColDate")}    col="fecha" />
                <Th label={t("histColFolder")}  col="carpeta" />
                <Th label={t("histColFile")}    col="archivo" />
                <Th label={t("histColVerdict")} col="veredicto" />
                <Th label={t("histColScore")}   col="score" />
                <Th label={t("histColSpectral")}col="techo_espectral_khz" />
                <Th label={t("histColDR")}      col="rango_dinamico_db" />
              </tr>
            </thead>
            <tbody>
              {filtered.map((row, i) => (
                <tr
                  key={i}
                  className="border-b border-border/40 hover:bg-surface2 transition-colors"
                >
                  <td className="px-3 py-2 text-muted font-mono whitespace-nowrap">{row.fecha}</td>
                  <td className="px-3 py-2 text-white/50 max-w-[180px] truncate" title={row.carpeta}>
                    {row.carpeta.split(/[\\/]/).slice(-1)[0] ?? row.carpeta}
                  </td>
                  <td className="px-3 py-2 text-white/80 max-w-[220px] truncate" title={row.archivo}>
                    {row.archivo}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <VerdictPill raw={row.veredicto} display={translateVerdict(row.veredicto, lang)} />
                  </td>
                  <td className="px-3 py-2 font-mono text-right text-white/70">{row.score}</td>
                  <td className="px-3 py-2 font-mono text-right text-white/70">
                    {row.techo_espectral_khz ? `${row.techo_espectral_khz} kHz` : "—"}
                  </td>
                  <td className="px-3 py-2 font-mono text-right text-white/70">
                    {row.rango_dinamico_db ? `${row.rango_dinamico_db} dB` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
