import { useLang } from "../context/LanguageContext";
import type { AnalysisSummary } from "../types";

interface SummaryBarProps {
  summary: AnalysisSummary;
  total: number;
}

export function SummaryBar({ summary, total }: SummaryBarProps) {
  const { t } = useLang();
  const filesLabel = total === 1 ? t("filesOne") : t("filesMany");

  return (
    <div className="animate-slide-in mx-auto max-w-2xl" style={{ animationDelay: "0ms", opacity: 0 }}>
      <div className="rounded-xl border border-border2 bg-surface2 p-5 text-center">
        <p className="mb-3 text-xs font-semibold uppercase tracking-widest text-accent">
          {t("analysisComplete")} — {total} {filesLabel}
        </p>
        <div className="flex flex-wrap items-center justify-center gap-6">
          {summary.genuine > 0 && (
            <div className="flex flex-col items-center">
              <span className="text-3xl font-bold text-genuine">{summary.genuine}</span>
              <span className="mt-0.5 text-xs text-genuine/70">{t("summaryGenuine")}</span>
            </div>
          )}
          {summary.probable > 0 && (
            <div className="flex flex-col items-center">
              <span className="text-3xl font-bold text-probable">{summary.probable}</span>
              <span className="mt-0.5 text-xs text-probable/70">{t("summaryProbable")}</span>
            </div>
          )}
          {summary.doubtful > 0 && (
            <div className="flex flex-col items-center">
              <span className="text-3xl font-bold text-doubtful">{summary.doubtful}</span>
              <span className="mt-0.5 text-xs text-doubtful/70">{t("summaryDoubtful")}</span>
            </div>
          )}
          {summary.upscale > 0 && (
            <div className="flex flex-col items-center">
              <span className="text-3xl font-bold text-upscale">{summary.upscale}</span>
              <span className="mt-0.5 text-xs text-upscale/70">{t("summaryUpscale")}</span>
            </div>
          )}
          {summary.errors > 0 && (
            <div className="flex flex-col items-center">
              <span className="text-3xl font-bold text-muted">{summary.errors}</span>
              <span className="mt-0.5 text-xs text-muted">{t("summaryError")}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
