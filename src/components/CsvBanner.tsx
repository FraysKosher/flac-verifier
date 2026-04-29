import { useLang } from "../context/LanguageContext";
import { openPath } from "@tauri-apps/plugin-opener";

interface CsvBannerProps {
  csvPath: string | null;
  csvError: string | null;
  onDismiss: () => void;
}

export function CsvBanner({ csvPath, csvError, onDismiss }: CsvBannerProps) {
  const { t } = useLang();

  if (!csvPath && !csvError) return null;

  const handleOpen = async () => {
    if (csvPath) {
      try { await openPath(csvPath); } catch (e) { console.error("open csv failed:", e); }
    }
  };

  if (csvError) {
    return (
      <div
        className="flex items-center gap-2 rounded bg-doubtful/10 border border-doubtful/20 px-3 py-1 text-xs text-doubtful max-w-xs"
        title={csvError}
      >
        <span className="shrink-0">⚠ CSV:</span>
        <span className="truncate">{csvError}</span>
        <button onClick={onDismiss} className="shrink-0 ml-1 opacity-60 hover:opacity-100">×</button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 rounded bg-accent/8 border border-accent/15 px-3 py-1 text-xs text-accent max-w-xs">
      <span className="shrink-0">📊 {t("csvSaved")}</span>
      <button
        onClick={handleOpen}
        className="shrink-0 underline underline-offset-2 hover:text-white transition-colors"
      >
        {t("openCsv")}
      </button>
      <button onClick={onDismiss} className="shrink-0 ml-auto opacity-60 hover:opacity-100">×</button>
    </div>
  );
}
