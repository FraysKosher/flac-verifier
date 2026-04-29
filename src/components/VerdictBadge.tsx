import { useLang } from "../context/LanguageContext";
import type { Verdict } from "../types";

interface VerdictBadgeProps {
  veredicto: Verdict;
  size?: "sm" | "md" | "lg";
}

function getBadgeClass(v: Verdict): string {
  if (v === "LOSSLESS GENUINO")         return "badge-genuine";
  if (v === "PROBABLEMENTE LOSSLESS")   return "badge-probable";
  if (v === "DUDOSO")                   return "badge-doubtful";
  if (v === "PROBABLE UPSCALE")         return "badge-upscale";
  return "bg-white/5 text-white/50 border border-white/10";
}

export function VerdictBadge({ veredicto, size = "md" }: VerdictBadgeProps) {
  const { t } = useLang();

  function getLabel(v: Verdict): string {
    if (v === "LOSSLESS GENUINO")       return t("verdictGenuine");
    if (v === "PROBABLEMENTE LOSSLESS") return t("verdictProbable");
    if (v === "DUDOSO")                 return t("verdictDoubtful");
    if (v === "PROBABLE UPSCALE")       return t("verdictUpscale");
    return v;
  }

  const sizeClass =
    size === "sm" ? "text-[10px] px-2 py-0.5 rounded" :
    size === "lg" ? "text-sm px-4 py-1.5 rounded-md font-bold tracking-widest" :
                   "text-xs px-3 py-1 rounded font-semibold tracking-wide";

  return (
    <span className={`inline-flex items-center ${sizeClass} ${getBadgeClass(veredicto)}`}>
      {getLabel(veredicto)}
    </span>
  );
}
