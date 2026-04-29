import { useState, useRef, useEffect } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { AppLogo } from "./AppLogo";
import { useLang } from "../context/LanguageContext";
import type { Translations } from "../i18n";
import type { AnalysisMode } from "../types";

interface SidebarProps {
  isRunning: boolean;
  progress: number;
  done: number;
  total: number;
  onAnalyze: (path: string, mode: AnalysisMode, png: boolean, pdf: boolean, seg?: number) => void;
  onReset: () => void;
}

export function Sidebar({ isRunning, progress, done, total, onAnalyze, onReset }: SidebarProps) {
  const { t } = useLang();
  const [path, setPath]             = useState("");
  const [mode, setMode]             = useState<AnalysisMode>("centro");
  const [png, setPng]               = useState(false);
  const [pdf, setPdf]               = useState(true);
  const [seg, setSeg]               = useState(15);
  const [isDragOver, setIsDragOver] = useState(false);
  const dropZoneRef                 = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let unlisten: (() => void) | null = null;
    const setup = async () => {
      try {
        const appWindow = getCurrentWindow();
        unlisten = await appWindow.onDragDropEvent((event) => {
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          const payload = event.payload as any;
          if (payload.type === "drop") {
            const paths: string[] = payload.paths ?? [];
            if (paths.length > 0) { setPath(paths[0]); setIsDragOver(false); }
          } else if (payload.type === "enter" || payload.type === "over") {
            setIsDragOver(true);
          } else if (payload.type === "leave" || payload.type === "cancelled") {
            setIsDragOver(false);
          }
        });
      } catch (e) {
        console.warn("Drag-drop setup failed:", e);
      }
    };
    setup();
    return () => { if (unlisten) unlisten(); };
  }, []);

  const handleBrowse = async () => {
    const selected = await open({ directory: true, multiple: false, title: t("source") });
    if (selected && typeof selected === "string") setPath(selected);
  };

  const handleBrowseFile = async () => {
    const selected = await open({ filters: [{ name: "FLAC Audio", extensions: ["flac"] }], multiple: false });
    if (selected && typeof selected === "string") setPath(selected);
  };

  const handleAnalyze = () => {
    if (!path.trim() || isRunning) return;
    onAnalyze(path.trim(), mode, png, pdf, mode === "segundos" ? seg : undefined);
  };

  const canAnalyze = path.trim().length > 0 && !isRunning;

  const modes: { value: AnalysisMode; labelKey: keyof Translations; subKey: keyof Translations }[] = [
    { value: "segundos", labelKey: "first15s",  subKey: "fastest" },
    { value: "centro",   labelKey: "center30s", subKey: "recommended" },
    { value: "completo", labelKey: "fullTrack", subKey: "mostAccurate" },
  ];

  return (
    <aside className="flex h-full w-[260px] flex-shrink-0 flex-col border-r border-border bg-surface overflow-y-auto">
      {/* Logo */}
      <div className="border-b border-border px-5 py-5">
        <div className="flex items-center gap-2.5">
          <AppLogo size={28} color="#ffffff" />
          <div>
            <p className="text-sm font-bold tracking-tight text-white">FLAC Verifier</p>
            <p className="text-[10px] text-muted font-mono tracking-wider">AUTHENTICITY ANALYZER</p>
          </div>
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-5 p-4">
        {/* Drop Zone */}
        <div>
          <label className="mb-1.5 block text-[10px] font-semibold uppercase tracking-widest text-muted">
            {t("source")}
          </label>
          <div
            ref={dropZoneRef}
            className={`relative rounded-lg border-2 border-dashed p-3 transition-all ${
              isDragOver ? "border-accent bg-accent/5" : "border-border2 bg-surface2 hover:border-border"
            }`}
          >
            {path ? (
              <div>
                <p className="text-[10px] text-muted mb-1">{t("selectedPath")}</p>
                <p className="text-xs font-mono text-white/80 break-all leading-relaxed">{path}</p>
                <button onClick={() => setPath("")}
                  className="mt-2 text-[10px] text-muted hover:text-upscale transition-colors">
                  {t("clearPath")}
                </button>
              </div>
            ) : (
              <div className="flex flex-col items-center gap-1.5 py-3 text-center">
                <svg className={`h-7 w-7 ${isDragOver ? "text-accent" : "text-muted"} transition-colors`}
                  fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
                    d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                </svg>
                <p className={`text-xs ${isDragOver ? "text-accent" : "text-muted"}`}>{t("dropFolder")}</p>
                <p className="text-[10px] text-muted/50">{t("orUseButtons")}</p>
              </div>
            )}
          </div>
          <div className="mt-1.5 flex gap-1.5">
            <button onClick={handleBrowse} disabled={isRunning}
              className="flex-1 rounded-md border border-border2 bg-surface2 px-2 py-1.5 text-[11px] font-medium text-white/70 hover:border-accent/40 hover:text-white transition-all disabled:opacity-40">
              {t("folderBtn")}
            </button>
            <button onClick={handleBrowseFile} disabled={isRunning}
              className="flex-1 rounded-md border border-border2 bg-surface2 px-2 py-1.5 text-[11px] font-medium text-white/70 hover:border-accent/40 hover:text-white transition-all disabled:opacity-40">
              {t("fileBtn")}
            </button>
          </div>
        </div>

        {/* Analysis Mode */}
        <div>
          <label className="mb-2 block text-[10px] font-semibold uppercase tracking-widest text-muted">
            {t("analysisMode")}
          </label>
          <div className="space-y-1.5">
            {modes.map(({ value, labelKey, subKey }) => (
              <label key={value}
                className={`flex cursor-pointer items-center justify-between rounded-lg px-3 py-2 border transition-all ${
                  mode === value
                    ? "border-accent/40 bg-accent/8 text-white"
                    : "border-border bg-surface2 text-white/60 hover:border-border2 hover:text-white/80"
                }`}>
                <div className="flex items-center gap-2.5">
                  <div className={`h-3.5 w-3.5 rounded-full border-2 flex items-center justify-center flex-shrink-0 ${
                    mode === value ? "border-accent" : "border-muted"
                  }`}>
                    {mode === value && <div className="h-1.5 w-1.5 rounded-full bg-accent" />}
                  </div>
                  <input type="radio" className="sr-only" name="mode" value={value}
                    checked={mode === value} onChange={() => setMode(value)} disabled={isRunning} />
                  <span className="text-xs font-medium">{t(labelKey)}</span>
                </div>
                <span className="text-[10px] text-muted">{t(subKey)}</span>
              </label>
            ))}
          </div>
          {mode === "segundos" && (
            <div className="mt-2 flex items-center gap-2">
              <label className="text-[10px] text-muted">{t("seconds")}</label>
              <input type="number" min={5} max={120} value={seg}
                onChange={(e) => setSeg(Number(e.target.value))} disabled={isRunning}
                className="w-16 rounded border border-border2 bg-surface2 px-2 py-1 text-xs text-white font-mono focus:border-accent focus:outline-none" />
            </div>
          )}
        </div>

        {/* Options */}
        <div className="space-y-2">
          {/* PNG checkbox */}
          <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-border px-3 py-2 bg-surface2 hover:border-border2 transition-all">
            <div onClick={() => !isRunning && setPng(!png)}
              className={`h-4 w-4 rounded border flex items-center justify-center flex-shrink-0 transition-all ${
                png ? "border-accent bg-accent" : "border-muted bg-transparent"
              }`}>
              {png && (
                <svg className="h-2.5 w-2.5 text-bg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              )}
            </div>
            <input type="checkbox" className="sr-only" checked={png}
              onChange={(e) => setPng(e.target.checked)} disabled={isRunning} />
            <div>
              <p className="text-xs font-medium text-white/80">{t("saveSpectrograms")}</p>
              <p className="text-[10px] text-muted">{t("pngFolder")}</p>
            </div>
          </label>

          {/* PDF checkbox */}
          <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-border px-3 py-2 bg-surface2 hover:border-border2 transition-all">
            <div onClick={() => !isRunning && setPdf(!pdf)}
              className={`h-4 w-4 rounded border flex items-center justify-center flex-shrink-0 transition-all ${
                pdf ? "border-accent bg-accent" : "border-muted bg-transparent"
              }`}>
              {pdf && (
                <svg className="h-2.5 w-2.5 text-bg" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              )}
            </div>
            <input type="checkbox" className="sr-only" checked={pdf}
              onChange={(e) => setPdf(e.target.checked)} disabled={isRunning} />
            <div>
              <p className="text-xs font-medium text-white/80">PDF Report</p>
              <p className="text-[10px] text-muted">FLAC_Report_YYYY-MM-DD.pdf</p>
            </div>
          </label>
        </div>

        <div className="flex-1" />

        {/* Progress Bar */}
        {(isRunning || total > 0) && (
          <div>
            <div className="mb-1.5 flex items-center justify-between">
              <span className="text-[10px] font-semibold uppercase tracking-wider text-muted">
                {isRunning ? t("analyzingBtn") : t("complete")}
              </span>
              <span className="text-[10px] font-mono text-accent">{done}/{total}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-border2">
              <div
                className={`h-full rounded-full bg-accent transition-all duration-300 ease-out ${isRunning ? "progress-shimmer" : ""}`}
                style={{ width: `${progress}%` }}
              />
            </div>
            <p className="mt-1 text-right text-[10px] font-mono text-muted">{progress}%</p>
          </div>
        )}

        {/* Analyze Button */}
        <div className="space-y-2">
          <button onClick={handleAnalyze} disabled={!canAnalyze}
            className={`btn-primary w-full rounded-lg px-4 py-3 text-sm font-semibold tracking-wide transition-all ${
              canAnalyze
                ? "bg-accent text-bg hover:bg-accent-dim cursor-pointer shadow-lg shadow-accent/20"
                : "bg-surface2 text-muted cursor-not-allowed border border-border"
            }`}>
            {isRunning ? (
              <span className="flex items-center justify-center gap-2">
                <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                </svg>
                {t("analyzingBtn")}
              </span>
            ) : t("analyze")}
          </button>
          {!isRunning && total > 0 && (
            <button onClick={onReset}
              className="w-full rounded-lg border border-border px-4 py-2 text-xs font-medium text-muted hover:border-border2 hover:text-white/70 transition-all">
              {t("clearResults")}
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}
