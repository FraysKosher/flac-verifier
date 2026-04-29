import { useState } from "react";
import { convertFileSrc } from "@tauri-apps/api/core";
import { openPath } from "@tauri-apps/plugin-opener";
import { VerdictBadge } from "./VerdictBadge";
import { MetricPanel, type MetricKey } from "./MetricPanel";
import { useLang } from "../context/LanguageContext";
import type { AnalysisResult } from "../types";

interface ResultCardProps {
  result: AnalysisResult;
  index: number;
}

function getScoreClass(v: string | undefined): string {
  if (!v) return "text-muted";
  if (v === "LOSSLESS GENUINO")       return "score-genuine";
  if (v === "PROBABLEMENTE LOSSLESS") return "score-probable";
  if (v === "DUDOSO")                 return "score-doubtful";
  if (v === "PROBABLE UPSCALE")       return "score-upscale";
  return "text-muted";
}

function getCardBorderClass(v: string | undefined): string {
  if (!v) return "border-border";
  if (v === "LOSSLESS GENUINO")       return "border-genuine/25";
  if (v === "PROBABLEMENTE LOSSLESS") return "border-probable/25";
  if (v === "DUDOSO")                 return "border-doubtful/25";
  if (v === "PROBABLE UPSCALE")       return "border-upscale/25";
  return "border-border";
}

function formatHz(hz: number): string {
  return hz >= 1000 ? `${(hz / 1000).toFixed(1)} kHz` : `${hz} Hz`;
}

function formatDuration(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function md5Icon(md5: number | null | undefined): string {
  return (!md5 || md5 === 0) ? "⚠" : "✓";
}

interface MetricCellProps {
  label: string;
  value: string;
  valueClass?: string;
  sub?: string;
  onClick?: () => void;
  active?: boolean;
}

function MetricCell({ label, value, valueClass = "text-white/80", sub, onClick, active }: MetricCellProps) {
  return (
    <div
      onClick={onClick}
      className={`rounded-lg bg-surface2 px-3.5 py-3.5 border transition-all ${
        onClick ? "cursor-pointer hover:border-accent/40 hover:bg-accent/5" : ""
      } ${active ? "border-accent/40 bg-accent/5" : "border-border"}`}
      title={onClick ? "Click to learn more" : undefined}
    >
      <div className="flex items-center justify-between mb-1">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-muted">{label}</p>
        {onClick && (
          <span className={`text-[11px] leading-none transition-colors ${active ? "text-accent" : "text-muted/50 hover:text-accent/70"}`}>?</span>
        )}
      </div>
      <p className={`text-lg font-bold font-mono leading-tight ${valueClass}`}>{value}</p>
      {sub && <p className="text-[10px] text-muted font-mono mt-1">{sub}</p>}
    </div>
  );
}

export function ResultCard({ result, index }: ResultCardProps) {
  const { t } = useLang();
  const { archivo, error, meta, score, veredicto, problemas, clip, dr, bdi, esp } = result;

  const [activeMetric, setActiveMetric] = useState<MetricKey | null>(null);
  const [spectroExpanded, setSpectroExpanded] = useState(false);
  const [spectroError, setSpectroError] = useState<string | null>(null);

  const scorePercent = score !== undefined ? Math.round(score * 100) : null;
  const borderClass  = getCardBorderClass(veredicto);
  const scoreClass   = getScoreClass(veredicto);
  const delay        = Math.min(index * 40, 400);

  const toggleMetric = (key: MetricKey) => {
    setActiveMetric((prev) => (prev === key ? null : key));
  };

  const handleOpenSpectrogram = async () => {
    if (!esp?.espectrograma) return;
    try {
      await openPath(esp.espectrograma);
    } catch (e) {
      console.error("openPath failed:", e);
      setSpectroError(`Could not open file: ${e}`);
    }
  };

  return (
    <div
      className={`animate-slide-in rounded-xl border bg-surface p-5 transition-all ${borderClass}`}
      style={{ animationDelay: `${delay}ms`, opacity: 0 }}
    >
      {/* ── Header Row ── */}
      <div className="mb-4 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-mono text-muted">#{index + 1}</span>
            {error ? (
              <span className="inline-flex items-center rounded bg-upscale/10 px-2 py-0.5 text-[10px] font-semibold text-upscale border border-upscale/25">
                {t("verdictError")}
              </span>
            ) : veredicto ? (
              <VerdictBadge veredicto={veredicto} />
            ) : null}
          </div>
          <h3 className="truncate text-sm font-semibold text-white/90 font-mono" title={archivo}>
            {archivo}
          </h3>
        </div>
        {scorePercent !== null && !error && (
          <div className={`flex-shrink-0 text-right ${scoreClass}`}>
            <span className="text-3xl font-bold tabular-nums leading-none">{scorePercent}</span>
            <span className="text-base font-semibold">%</span>
          </div>
        )}
      </div>

      {/* ── Error State ── */}
      {error ? (
        <p className="text-xs text-upscale/80 bg-upscale/5 rounded px-3 py-2 border border-upscale/15">{error}</p>
      ) : (
        <>
          {/* ── Metadata Chips ── */}
          {meta && (
            <div className="mb-4 flex flex-wrap gap-1.5">
              <span className="chip">{formatHz(meta.sample_rate)}</span>
              <span className="chip">{meta.bits_per_sample}-bit</span>
              <span className="chip">{meta.canales === 1 ? "Mono" : meta.canales === 2 ? "Stereo" : `${meta.canales}ch`}</span>
              <span className="chip">{formatDuration(meta.duracion)}</span>
              <span className={`chip ${meta.md5 && meta.md5 !== 0 ? "!text-genuine/80 !border-genuine/20" : "!text-doubtful/80 !border-doubtful/20"}`}>
                MD5 {md5Icon(meta.md5)}
              </span>
            </div>
          )}

          {/* ── Details Grid (clickable) ── */}
          <div className="mb-2 grid grid-cols-2 gap-2 sm:grid-cols-4">
            {esp && !esp.error && esp.techo_hz !== undefined && (
              <MetricCell
                label={t("spectralCeiling")}
                value={`${formatHz(esp.techo_hz)}${esp.corte_artificial ? ` ⊘ ${t("cutLabel")}` : ""}`}
                valueClass={esp.corte_artificial ? "text-upscale" : "text-white/80"}
                onClick={() => toggleMetric("spectralCeiling")}
                active={activeMetric === "spectralCeiling"}
              />
            )}
            {dr !== undefined && dr !== null && (
              <MetricCell
                label={t("dynamicRange")}
                value={`${dr} dB`}
                valueClass={dr > 12 ? "text-genuine" : dr > 6 ? "text-probable" : "text-upscale"}
                onClick={() => toggleMetric("dynamicRange")}
                active={activeMetric === "dynamicRange"}
              />
            )}
            {clip !== undefined && clip !== null && (
              <MetricCell
                label={t("clipping")}
                value={clip.hay_clipping ? `${clip.runs_clip} run${clip.runs_clip !== 1 ? "s" : ""}` : t("noneLabel")}
                valueClass={clip.hay_clipping ? "text-upscale" : "text-genuine"}
                onClick={() => toggleMetric("clipping")}
                active={activeMetric === "clipping"}
              />
            )}
            {bdi?.aplica && (
              <MetricCell
                label={t("bitAnalysis")}
                value={bdi.genuino_24 === true ? t("genuine24") : bdi.genuino_24 === false ? t("upscale24") : t("dithered24")}
                valueClass={bdi.genuino_24 === true ? "text-genuine" : bdi.genuino_24 === false ? "text-upscale" : "text-probable"}
                sub={bdi.fraccion_16bit_grid !== undefined ? `${bdi.fraccion_16bit_grid}${t("onBitGrid")}` : undefined}
                onClick={() => toggleMetric("bitAnalysis")}
                active={activeMetric === "bitAnalysis"}
              />
            )}
          </div>

          {/* ── Metric Explanation Panel ── */}
          {activeMetric && (
            <MetricPanel
              metricKey={activeMetric}
              result={result}
              onClose={() => setActiveMetric(null)}
            />
          )}

          {/* ── Spectral cut warning ── */}
          {esp?.corte_artificial && esp.frecuencia_corte && (
            <div className="mt-3 mb-2 flex items-center gap-2 rounded-lg bg-upscale/5 px-3 py-2 border border-upscale/20 text-xs text-upscale/90">
              <span className="text-base">⊘</span>
              {t("artificialCutAt")} {formatHz(esp.frecuencia_corte)} {t("lossySignature")}
            </div>
          )}

          {/* ── Issues List ── */}
          {problemas && problemas.length > 0 && (
            <div className="mt-3 space-y-1">
              <p className="text-[10px] font-semibold uppercase tracking-wider text-muted mb-1.5">
                {t("detectedIssues")}
              </p>
              {problemas.map((issue, i) => (
                <div key={i} className="flex items-start gap-2 rounded bg-doubtful/5 px-3 py-1.5 text-xs text-doubtful/90 border border-doubtful/15">
                  <span className="mt-px flex-shrink-0">⚠</span>
                  <span>{issue}</span>
                </div>
              ))}
            </div>
          )}

          {/* ── Spectrogram Section ── */}
          {esp?.espectrograma && (
            <div className="mt-3">
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setSpectroExpanded((v) => !v)}
                  className="inline-flex items-center gap-1.5 text-xs text-accent hover:text-accent-dim transition-colors"
                >
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
                  </svg>
                  {spectroExpanded ? t("hideSpectrogram") : t("showSpectrogram")}
                  <svg
                    className={`h-3 w-3 transition-transform ${spectroExpanded ? "rotate-180" : ""}`}
                    fill="none" stroke="currentColor" viewBox="0 0 24 24"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                <span className="text-muted/40 text-xs">·</span>
                <button
                  onClick={handleOpenSpectrogram}
                  className="text-xs text-muted hover:text-white transition-colors"
                >
                  {t("openExternal")} ↗
                </button>
              </div>

              {spectroExpanded && (
                <div className="mt-2 animate-fade-in overflow-hidden rounded-lg border border-border2 bg-surface2">
                  <img
                    src={convertFileSrc(esp.espectrograma)}
                    alt="Spectrogram"
                    className="w-full block"
                    loading="lazy"
                    onError={() => setSpectroError("Inline preview unavailable — use 'Open externally'.")}
                  />
                  {spectroError && (
                    <p className="px-3 py-2 text-xs text-muted italic">{spectroError}</p>
                  )}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
