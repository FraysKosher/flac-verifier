import { useLang } from "../context/LanguageContext";
import { openPath } from "@tauri-apps/plugin-opener";

interface PdfBannerProps {
  pdfPath: string;
  onDismiss: () => void;
}

export function PdfBanner({ pdfPath, onDismiss }: PdfBannerProps) {
  const { t } = useLang();
  const filename = pdfPath.split(/[\\/]/).pop() ?? pdfPath;

  const handleOpen = async () => {
    try {
      await openPath(pdfPath);
    } catch (e) {
      console.error("Failed to open PDF:", e);
    }
  };

  return (
    <div className="animate-slide-in mb-4 flex items-center justify-between gap-3 rounded-xl border border-genuine/30 bg-genuine/8 px-4 py-3"
      style={{ animationDelay: "0ms", opacity: 0 }}>
      <div className="flex items-center gap-3 min-w-0">
        <div className="flex-shrink-0 flex h-8 w-8 items-center justify-center rounded-lg bg-genuine/15 border border-genuine/25">
          <svg className="h-4 w-4 text-genuine" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
        </div>
        <div className="min-w-0">
          <p className="text-xs font-semibold text-genuine">{t("reportSaved")}</p>
          <p className="truncate text-[11px] font-mono text-genuine/70" title={pdfPath}>{filename}</p>
        </div>
      </div>
      <div className="flex flex-shrink-0 items-center gap-2">
        <button
          onClick={handleOpen}
          className="rounded-lg border border-genuine/30 bg-genuine/10 px-3 py-1.5 text-xs font-semibold text-genuine hover:bg-genuine/20 transition-all"
        >
          {t("openReport")}
        </button>
        <button
          onClick={onDismiss}
          className="rounded p-1 text-muted hover:text-white transition-colors"
          aria-label="Dismiss"
        >
          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>
    </div>
  );
}
