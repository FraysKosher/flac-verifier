import { useState } from "react";
import "./index.css";
import { useAnalysis } from "./hooks/useAnalysis";
import { Sidebar } from "./components/Sidebar";
import { ResultCard } from "./components/ResultCard";
import { SummaryBar } from "./components/SummaryBar";
import { PdfBanner } from "./components/PdfBanner";
import { CsvBanner } from "./components/CsvBanner";
import { HistoryTab } from "./components/HistoryTab";
import { useLang } from "./context/LanguageContext";
import { useTheme } from "./context/ThemeContext";
import type { AnalysisMode } from "./types";

function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";
  return (
    <button
      onClick={toggleTheme}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
      className="flex h-8 w-8 items-center justify-center rounded-full border border-border2 bg-surface2 text-muted hover:border-accent/40 hover:text-white transition-all"
    >
      <span key={theme} className="animate-theme-icon block leading-none text-[15px]">
        {isDark ? "☀" : "🌙"}
      </span>
    </button>
  );
}

function LangToggle() {
  const { lang, setLang } = useLang();
  return (
    <button
      onClick={() => setLang(lang === "en" ? "es" : "en")}
      className="flex items-center gap-1.5 rounded-full border border-border2 bg-surface2 px-3 py-1 text-[11px] font-bold tracking-widest text-muted hover:border-accent/40 hover:text-white transition-all"
      title={lang === "en" ? "Cambiar a español" : "Switch to English"}
    >
      <span className={lang === "en" ? "text-accent" : "text-muted/50"}>EN</span>
      <span className="text-border2">|</span>
      <span className={lang === "es" ? "text-accent" : "text-muted/50"}>ES</span>
    </button>
  );
}

function EmptyState() {
  const { t } = useLang();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-4 text-center animate-fade-in">
      <div className="relative">
        <div className="flex h-20 w-20 items-center justify-center rounded-2xl border border-border2 bg-surface2">
          <svg className="h-10 w-10 text-muted/40" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.2}
              d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3" />
          </svg>
        </div>
        <div className="absolute -right-1 -top-1 h-4 w-4 rounded-full bg-accent/30 border border-accent/50 animate-pulse-slow" />
      </div>
      <div>
        <h2 className="text-base font-semibold text-white/60">{t("noResultsYet")}</h2>
        <p className="mt-1 text-xs text-muted max-w-xs">
          {t("noResultsDesc")} <strong className="text-accent">{t("pressAnalyze")}</strong>.
        </p>
      </div>
      <div className="mt-2 flex flex-wrap justify-center gap-2">
        {([t("featureSpectral"), t("featureBit"), t("featureClipping"), t("featureDR")] as string[]).map((f) => (
          <span key={f} className="chip !border-border !text-muted/60">{f}</span>
        ))}
      </div>
    </div>
  );
}

type Tab = "results" | "history";

function App() {
  const {
    results, isRunning, progress, total, done,
    summary, errorMessage,
    pdfPath, pdfError, csvPath, csvError,
    startAnalysis, resetAnalysis, dismissPdf, dismissCsv,
  } = useAnalysis();

  const { t } = useLang();
  const [activeTab, setActiveTab] = useState<Tab>("results");

  const handleAnalyze = (path: string, mode: AnalysisMode, png: boolean, pdf: boolean, seg?: number) => {
    startAnalysis(path, mode, png, pdf, seg);
    setActiveTab("results");
  };

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-bg text-white">
      <Sidebar
        isRunning={isRunning}
        progress={progress}
        done={done}
        total={total}
        onAnalyze={handleAnalyze}
        onReset={resetAnalysis}
      />

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* Header bar */}
        <div className="flex h-12 flex-shrink-0 items-center justify-between border-b border-border bg-surface px-5">
          {/* Tab switcher */}
          <div className="flex items-center gap-1">
            <button
              onClick={() => setActiveTab("results")}
              className={`flex items-center gap-1.5 rounded-md px-3 py-1 text-sm font-semibold transition-all ${
                activeTab === "results"
                  ? "bg-accent/10 text-accent border border-accent/20"
                  : "text-white/50 hover:text-white/80"
              }`}
            >
              {t("results")}
              {results.length > 0 && (
                <span className="rounded-full bg-accent/20 px-1.5 py-0.5 text-[10px] font-semibold text-accent">
                  {results.length}
                </span>
              )}
            </button>
            <button
              onClick={() => setActiveTab("history")}
              className={`rounded-md px-3 py-1 text-sm font-semibold transition-all ${
                activeTab === "history"
                  ? "bg-accent/10 text-accent border border-accent/20"
                  : "text-white/50 hover:text-white/80"
              }`}
            >
              {t("historyTab")}
            </button>
          </div>

          {/* Right side: status + banners + lang */}
          <div className="flex items-center gap-2">
            {errorMessage && (
              <div className="max-w-sm truncate rounded bg-upscale/10 px-3 py-1 text-xs text-upscale border border-upscale/20">
                ⊘ {errorMessage}
              </div>
            )}
            {pdfError && (
              <div
                className="max-w-xs rounded bg-doubtful/10 px-3 py-1 text-xs text-doubtful border border-doubtful/20 flex items-center gap-2"
                title={pdfError}
              >
                <span className="shrink-0">⚠ PDF:</span>
                <span className="truncate">{pdfError}</span>
                <button onClick={dismissPdf} className="shrink-0 ml-1 opacity-60 hover:opacity-100">×</button>
              </div>
            )}
            <CsvBanner csvPath={csvPath} csvError={csvError} onDismiss={dismissCsv} />
            {isRunning && (
              <div className="flex items-center gap-2 text-xs text-accent">
                <div className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse-slow" />
                {t("processingLabel")} {done}/{total}
              </div>
            )}
            <ThemeToggle />
            <LangToggle />
          </div>
        </div>

        {/* Tab content */}
        {activeTab === "history" ? (
          <HistoryTab />
        ) : (
          <div className="flex-1 overflow-y-auto px-5 py-5">
            {results.length === 0 && !isRunning ? (
              <EmptyState />
            ) : (
              <div className="mx-auto max-w-3xl space-y-3">
                {/* Report banners */}
                {pdfPath && <PdfBanner pdfPath={pdfPath} onDismiss={dismissPdf} />}
                {csvPath && !csvError && (
                  <CsvBanner csvPath={csvPath} csvError={null} onDismiss={dismissCsv} />
                )}

                {/* Summary bar */}
                {summary && !isRunning && (
                  <div className="mb-5">
                    <SummaryBar summary={summary} total={total} />
                  </div>
                )}

                {/* Result cards */}
                {results.map((result, index) => (
                  <ResultCard key={result.id} result={result} index={index} />
                ))}

                {/* Analyzing placeholder */}
                {isRunning && (
                  <div className="animate-pulse rounded-xl border border-border bg-surface p-5">
                    <div className="flex items-center gap-3">
                      <div className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse" />
                      <span className="text-xs text-muted font-mono">{t("analyzingNext")}</span>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
