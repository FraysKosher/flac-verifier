import { useEffect, useRef } from "react";
import { useLang } from "../context/LanguageContext";
import type { AnalysisResult } from "../types";
import type { Lang } from "../i18n";

export type MetricKey = "spectralCeiling" | "dynamicRange" | "clipping" | "bitAnalysis";

interface MetricPanelProps {
  metricKey: MetricKey;
  result: AnalysisResult;
  onClose: () => void;
}

function getImplication(key: MetricKey, result: AnalysisResult, lang: Lang): string {
  const { esp, dr, clip, bdi, meta } = result;
  if (lang === "es") {
    switch (key) {
      case "spectralCeiling": {
        const techo = esp?.techo_hz ?? 0;
        const nyquist = meta?.sample_rate ? meta.sample_rate / 2 : 22050;
        const pct = techo / nyquist;
        if (pct >= 0.95) return `${(techo / 1000).toFixed(1)} kHz está cerca del límite de Nyquist (${(nyquist / 1000).toFixed(0)} kHz). Fuerte indicador de lossless genuino.`;
        if (pct >= 0.80) return `${(techo / 1000).toFixed(1)} kHz está algo por debajo del límite esperado. Podría ser un master filtrado o levemente sospechoso.`;
        return `${(techo / 1000).toFixed(1)} kHz está muy por debajo del límite de Nyquist — fuerte evidencia de upscaling desde fuente lossy.`;
      }
      case "dynamicRange": {
        if (dr == null) return "No hay datos de rango dinámico disponibles.";
        if (dr > 14) return `${dr} dB — Excelente. Sugiere audio sin comprimir o con masterización ligera.`;
        if (dr > 10) return `${dr} dB — Bueno. Típico de producciones comerciales de calidad.`;
        if (dr > 6)  return `${dr} dB — Moderado. Se aplicó normalización de loudness fuerte.`;
        return `${dr} dB — Muy bajo. Podría indicar una fuente lossy o fuertemente comprimida.`;
      }
      case "clipping": {
        const runs = clip?.runs_clip ?? 0;
        if (runs === 0) return "No se detectó clipping. La envolvente dinámica está limpia.";
        if (runs < 5) return `${runs} racha${runs !== 1 ? "s" : ""} de clipping — menor. Posible artefacto de normalización.`;
        return `${runs} rachas de clipping — significativo. Revisar la cadena de masterización o calidad de la fuente.`;
      }
      case "bitAnalysis": {
        if (!bdi?.aplica) return "Este archivo es de 16 bits o menos; el análisis de 24 bits no aplica.";
        if (bdi.genuino_24 === true) return "Precisión sub-16-bit activa detectada — este es audio de 24 bits genuino.";
        if (bdi.genuino_24 === false) return `${bdi.fraccion_16bit_grid ?? "?"}% de las muestras caen en la cuadrícula de 16 bits — fuerte evidencia de upscaling.`;
        return `${bdi.fraccion_16bit_grid ?? "?"}% en cuadrícula 16-bit — posible dither mínimo sobre base de 16 bits.`;
      }
    }
  }
  // English (default)
  switch (key) {
    case "spectralCeiling": {
      const techo = esp?.techo_hz ?? 0;
      const nyquist = meta?.sample_rate ? meta.sample_rate / 2 : 22050;
      const pct = techo / nyquist;
      if (pct >= 0.95) return `${(techo / 1000).toFixed(1)} kHz is close to the Nyquist limit (${(nyquist / 1000).toFixed(0)} kHz). Strong indicator of genuine lossless.`;
      if (pct >= 0.80) return `${(techo / 1000).toFixed(1)} kHz is below the expected Nyquist limit. Could be a filtered master or mildly suspicious.`;
      return `${(techo / 1000).toFixed(1)} kHz is well below the Nyquist limit — strong evidence of upscaling from a lossy source.`;
    }
    case "dynamicRange": {
      if (dr == null) return "No dynamic range data available.";
      if (dr > 14) return `${dr} dB — Excellent. Suggests uncompressed or lightly mastered audio.`;
      if (dr > 10) return `${dr} dB — Good. Typical of quality commercial releases.`;
      if (dr > 6)  return `${dr} dB — Moderate. Heavy loudness normalization applied.`;
      return `${dr} dB — Very low. Could indicate a lossy or heavily compressed source.`;
    }
    case "clipping": {
      const runs = clip?.runs_clip ?? 0;
      if (runs === 0) return "No clipping detected. The dynamic envelope is clean.";
      if (runs < 5) return `${runs} clipping run${runs !== 1 ? "s" : ""} — minor. Possible normalization artifact.`;
      return `${runs} clipping runs — significant. Check the mastering chain or source quality.`;
    }
    case "bitAnalysis": {
      if (!bdi?.aplica) return "This file is 16-bit or lower; 24-bit analysis does not apply.";
      if (bdi.genuino_24 === true) return "Active sub-16-bit precision detected — this is genuine 24-bit audio.";
      if (bdi.genuino_24 === false) return `${bdi.fraccion_16bit_grid ?? "?"}% of samples fall on the 16-bit grid — strong evidence of upscaling.`;
      return `${bdi.fraccion_16bit_grid ?? "?"}% on 16-bit grid — possible minimal dither over a 16-bit base.`;
    }
  }
}

const METRIC_KEYS: Record<MetricKey, { title: keyof import("../i18n").Translations; what: keyof import("../i18n").Translations; why: keyof import("../i18n").Translations }> = {
  spectralCeiling: { title: "mpSpectralTitle", what: "mpSpectralWhat", why: "mpSpectralWhy" },
  dynamicRange:    { title: "mpDRTitle",       what: "mpDRWhat",       why: "mpDRWhy" },
  clipping:        { title: "mpClipTitle",     what: "mpClipWhat",     why: "mpClipWhy" },
  bitAnalysis:     { title: "mpBitTitle",      what: "mpBitWhat",      why: "mpBitWhy" },
};

export function MetricPanel({ metricKey, result, onClose }: MetricPanelProps) {
  const { t, lang } = useLang();
  const keys = METRIC_KEYS[metricKey];
  const implication = getImplication(metricKey, result, lang);
  const panelRef = useRef<HTMLDivElement>(null);

  // Close on Escape
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <div
      ref={panelRef}
      className="mt-3 animate-slide-in overflow-hidden rounded-lg border border-accent/20 bg-surface2"
      style={{ animationDelay: "0ms", opacity: 0 }}
    >
      <div className="flex items-center justify-between border-b border-accent/15 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <div className="h-1.5 w-1.5 rounded-full bg-accent" />
          <span className="text-xs font-bold uppercase tracking-wider text-accent">
            {t(keys.title)}
          </span>
        </div>
        <button
          onClick={onClose}
          className="rounded p-0.5 text-muted hover:text-white transition-colors"
          aria-label={t("mpClose")}
        >
          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <div className="space-y-4 px-4 py-3 text-xs leading-relaxed">
        <div>
          <p className="mb-1 font-semibold text-white/50 uppercase tracking-wider text-[10px]">{t("mpWhatLabel")}</p>
          <p className="text-white/70">{t(keys.what)}</p>
        </div>
        <div>
          <p className="mb-1 font-semibold text-white/50 uppercase tracking-wider text-[10px]">{t("mpWhyLabel")}</p>
          <p className="text-white/70">{t(keys.why)}</p>
        </div>
        <div className="rounded-md bg-accent/8 border border-accent/15 px-3 py-2.5">
          <p className="mb-1 font-semibold text-accent uppercase tracking-wider text-[10px]">{t("mpCurrentValue")}</p>
          <p className="text-white/90 font-medium">{implication}</p>
        </div>
      </div>
    </div>
  );
}
