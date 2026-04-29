export type Lang = "en" | "es";

export interface Translations {
  // Sidebar
  source: string;
  analysisMode: string;
  first15s: string;
  fastest: string;
  center30s: string;
  recommended: string;
  fullTrack: string;
  mostAccurate: string;
  seconds: string;
  saveSpectrograms: string;
  pngFolder: string;
  analyze: string;
  analyzingBtn: string;
  clearResults: string;
  dropFolder: string;
  orUseButtons: string;
  selectedPath: string;
  clearPath: string;
  folderBtn: string;
  fileBtn: string;
  complete: string;
  // Header
  results: string;
  // Empty state
  noResultsYet: string;
  noResultsDesc: string;
  pressAnalyze: string;
  featureSpectral: string;
  featureBit: string;
  featureClipping: string;
  featureDR: string;
  // Result card
  spectralCeiling: string;
  dynamicRange: string;
  clipping: string;
  bitAnalysis: string;
  detectedIssues: string;
  spectrogramSaved: string;
  showSpectrogram: string;
  hideSpectrogram: string;
  openExternal: string;
  cutLabel: string;
  noneLabel: string;
  genuine24: string;
  upscale24: string;
  dithered24: string;
  onBitGrid: string;
  artificialCutAt: string;
  lossySignature: string;
  // Verdict badges
  verdictGenuine: string;
  verdictProbable: string;
  verdictDoubtful: string;
  verdictUpscale: string;
  verdictError: string;
  // Summary bar
  analysisComplete: string;
  filesOne: string;
  filesMany: string;
  summaryGenuine: string;
  summaryProbable: string;
  summaryDoubtful: string;
  summaryUpscale: string;
  summaryError: string;
  // PDF/CSV banner
  reportSaved: string;
  openReport: string;
  reportError: string;
  csvSaved: string;
  openCsv: string;
  csvError: string;
  savePdf: string;
  // History tab
  historyTab: string;
  historyEmpty: string;
  historyEmptyDesc: string;
  clearHistory: string;
  clearHistoryConfirm: string;
  openHistoryCsv: string;
  refreshHistory: string;
  histColDate: string;
  histColFolder: string;
  histColFile: string;
  histColVerdict: string;
  histColScore: string;
  histColSpectral: string;
  histColDR: string;
  // Metric panel — Spectral Ceiling
  mpSpectralTitle: string;
  mpSpectralWhat: string;
  mpSpectralWhy: string;
  // Metric panel — Dynamic Range
  mpDRTitle: string;
  mpDRWhat: string;
  mpDRWhy: string;
  // Metric panel — Clipping
  mpClipTitle: string;
  mpClipWhat: string;
  mpClipWhy: string;
  // Metric panel — 24-bit
  mpBitTitle: string;
  mpBitWhat: string;
  mpBitWhy: string;
  // Metric panel implication prefixes
  mpCurrentValue: string;
  mpClose: string;
  mpWhatLabel: string;
  mpWhyLabel: string;
  // Analyzing placeholder
  analyzingNext: string;
  processingLabel: string;
  // Lang toggle
  langToggle: string;
  // Result filters
  filterAll: string;
  filterGenuine: string;
  filterFake: string;
  filterInconclusive: string;
  filterNoMatch: string;
}

export const i18n: Record<Lang, Translations> = {
  en: {
    source: "Source",
    analysisMode: "Analysis Mode",
    first15s: "First 15s",
    fastest: "fastest",
    center30s: "30s Center",
    recommended: "recommended",
    fullTrack: "Full Track",
    mostAccurate: "most accurate",
    seconds: "Seconds:",
    saveSpectrograms: "Save Spectrograms",
    pngFolder: "PNG in _espectrogramas/",
    analyze: "⚡ Analyze",
    analyzingBtn: "Analyzing…",
    clearResults: "Clear Results",
    dropFolder: "Drop folder here",
    orUseButtons: "or use buttons below",
    selectedPath: "Selected path:",
    clearPath: "✕ Clear",
    folderBtn: "📁 Folder",
    fileBtn: "🎵 File",
    complete: "Complete",
    results: "Results",
    noResultsYet: "No results yet",
    noResultsDesc: "Select a folder or FLAC file from the sidebar, choose an analysis mode, and press",
    pressAnalyze: "Analyze",
    featureSpectral: "Spectral Analysis",
    featureBit: "24-bit Verification",
    featureClipping: "Clipping Detection",
    featureDR: "Dynamic Range",
    spectralCeiling: "Spectral Ceiling",
    dynamicRange: "Dynamic Range",
    clipping: "Clipping",
    bitAnalysis: "24-bit Analysis",
    detectedIssues: "Detected Issues",
    spectrogramSaved: "Spectrogram saved",
    showSpectrogram: "Show spectrogram",
    hideSpectrogram: "Hide spectrogram",
    openExternal: "Open externally",
    cutLabel: "cut",
    noneLabel: "None",
    genuine24: "✓ Genuine",
    upscale24: "✗ Upscale",
    dithered24: "~ Dithered",
    onBitGrid: "% on 16-bit grid",
    artificialCutAt: "Artificial cut at",
    lossySignature: "— lossy codec signature detected",
    verdictGenuine: "✓ GENUINE",
    verdictProbable: "~ PROBABLE",
    verdictDoubtful: "⚠ DOUBTFUL",
    verdictUpscale: "✗ UPSCALE",
    verdictError: "⊘ ERROR",
    analysisComplete: "Analysis Complete",
    filesOne: "file",
    filesMany: "files",
    summaryGenuine: "✅ GENUINE",
    summaryProbable: "~ PROBABLE",
    summaryDoubtful: "⚠️ DOUBTFUL",
    summaryUpscale: "❌ UPSCALE",
    summaryError: "⊘ ERROR",
    reportSaved: "Report saved",
    openReport: "Open PDF",
    reportError: "Could not generate PDF report",
    csvSaved: "CSV saved",
    openCsv: "Open CSV",
    csvError: "Could not generate CSV",
    savePdf: "Save Report (PDF + CSV)",
    historyTab: "History",
    historyEmpty: "No history yet",
    historyEmptyDesc: "Run an analysis with the Report option enabled to start building your history.",
    clearHistory: "Clear history",
    clearHistoryConfirm: "Delete the entire analysis history? This cannot be undone.",
    openHistoryCsv: "Open master CSV",
    refreshHistory: "Refresh",
    histColDate: "Date",
    histColFolder: "Folder",
    histColFile: "File",
    histColVerdict: "Verdict",
    histColScore: "Score",
    histColSpectral: "Spectral",
    histColDR: "DR",
    mpSpectralTitle: "Spectral Ceiling",
    mpSpectralWhat: "The highest frequency containing significant audio energy (above −60 dB from peak). Measured from the short-time Fourier transform of the audio.",
    mpSpectralWhy: "Lossless CD files (44.1 kHz) should have content reaching ~22 kHz — close to the Nyquist limit. Lossy codecs like MP3 and AAC hard-limit frequencies at their encoding ceiling, leaving an abrupt spectral shelf. A ceiling well below the Nyquist frequency is a strong fingerprint of upscaling from a lossy source.",
    mpDRTitle: "Dynamic Range",
    mpDRWhat: "The ratio between the loudest peak and the average of the loudest 3-second RMS blocks, expressed in dB.",
    mpDRWhy: "High DR (> 14 dB) suggests uncompressed or lightly mastered audio. Modern commercial releases often land at DR 6–10 dB due to loudness normalization and limiting. Very low DR (< 6 dB) can indicate the source was a compressed stream, a heavily brick-walled master, or a re-encode of a lossy file.",
    mpClipTitle: "Clipping Detection",
    mpClipWhat: "Counts runs of 3 or more consecutive audio samples at the maximum digital level (amplitude ≥ 0.9999 of full scale), indicating flat-topped waveforms.",
    mpClipWhy: "Clipping causes audible distortion and is a sign that the audio was normalized beyond 0 dBFS or sourced from an already-clipped master. It can also occur when re-encoding a file with incorrect gain settings. While clipping alone does not prove a file is lossy, its presence alongside other issues is a red flag.",
    mpBitTitle: "24-bit Depth Analysis",
    mpBitWhat: "Checks whether a 24-bit FLAC truly uses the full 24-bit sample precision by measuring how many samples fall exactly on the 16-bit integer grid.",
    mpBitWhy: "When a 16-bit source (CD) is upscaled to 24-bit, sample values still land on the 16-bit grid — the extra 8 bits contain zeros or minimal dither. If more than 97% of samples align to the 16-bit grid, the file is almost certainly a fake 24-bit upscale. Genuine 24-bit recordings have significant sub-16-bit precision, so this fraction stays low.",
    mpCurrentValue: "Current value:",
    mpClose: "Close",
    mpWhatLabel: "WHAT IT MEASURES",
    mpWhyLabel: "WHY IT MATTERS",
    analyzingNext: "Analyzing next file…",
    processingLabel: "Processing",
    langToggle: "ES",
    filterAll: "All",
    filterGenuine: "Genuine",
    filterFake: "Fake",
    filterInconclusive: "Inconclusive",
    filterNoMatch: "No results match this filter.",
  },
  es: {
    source: "Fuente",
    analysisMode: "Modo de análisis",
    first15s: "Primeros 15s",
    fastest: "más rápido",
    center30s: "30s Central",
    recommended: "recomendado",
    fullTrack: "Pista completa",
    mostAccurate: "más preciso",
    seconds: "Segundos:",
    saveSpectrograms: "Guardar espectrogramas",
    pngFolder: "PNG en _espectrogramas/",
    analyze: "⚡ Analizar",
    analyzingBtn: "Analizando…",
    clearResults: "Borrar resultados",
    dropFolder: "Suelta carpeta aquí",
    orUseButtons: "o usa los botones abajo",
    selectedPath: "Ruta seleccionada:",
    clearPath: "✕ Borrar",
    folderBtn: "📁 Carpeta",
    fileBtn: "🎵 Archivo",
    complete: "Completado",
    results: "Resultados",
    noResultsYet: "Sin resultados aún",
    noResultsDesc: "Selecciona una carpeta o archivo FLAC en el panel lateral, elige un modo de análisis y presiona",
    pressAnalyze: "Analizar",
    featureSpectral: "Análisis espectral",
    featureBit: "Verificación 24 bits",
    featureClipping: "Detección de clipping",
    featureDR: "Rango dinámico",
    spectralCeiling: "Techo espectral",
    dynamicRange: "Rango dinámico",
    clipping: "Clipping",
    bitAnalysis: "Análisis 24 bits",
    detectedIssues: "Problemas detectados",
    spectrogramSaved: "Espectrograma guardado",
    showSpectrogram: "Mostrar espectrograma",
    hideSpectrogram: "Ocultar espectrograma",
    openExternal: "Abrir externo",
    cutLabel: "corte",
    noneLabel: "Ninguno",
    genuine24: "✓ Genuino",
    upscale24: "✗ Upscale",
    dithered24: "~ Dithered",
    onBitGrid: "% en cuadrícula 16-bit",
    artificialCutAt: "Corte artificial a",
    lossySignature: "— firma de codec lossy detectada",
    verdictGenuine: "✓ GENUINO",
    verdictProbable: "~ PROBABLE",
    verdictDoubtful: "⚠ DUDOSO",
    verdictUpscale: "✗ UPSCALE",
    verdictError: "⊘ ERROR",
    analysisComplete: "Análisis completo",
    filesOne: "archivo",
    filesMany: "archivos",
    summaryGenuine: "✅ GENUINO",
    summaryProbable: "~ PROBABLE",
    summaryDoubtful: "⚠️ DUDOSO",
    summaryUpscale: "❌ UPSCALE",
    summaryError: "⊘ ERROR",
    reportSaved: "Reporte guardado",
    openReport: "Abrir PDF",
    reportError: "No se pudo generar el reporte PDF",
    csvSaved: "CSV guardado",
    openCsv: "Abrir CSV",
    csvError: "No se pudo generar CSV",
    savePdf: "Guardar reporte (PDF + CSV)",
    historyTab: "Historial",
    historyEmpty: "Sin historial aún",
    historyEmptyDesc: "Ejecuta un análisis con la opción Reporte activada para comenzar a construir tu historial.",
    clearHistory: "Limpiar historial",
    clearHistoryConfirm: "¿Eliminar todo el historial de análisis? Esta acción no se puede deshacer.",
    openHistoryCsv: "Abrir CSV maestro",
    refreshHistory: "Actualizar",
    histColDate: "Fecha",
    histColFolder: "Carpeta",
    histColFile: "Archivo",
    histColVerdict: "Veredicto",
    histColScore: "Puntuación",
    histColSpectral: "Espectral",
    histColDR: "Rango Din.",
    mpSpectralTitle: "Techo espectral",
    mpSpectralWhat: "La frecuencia más alta con energía de audio significativa (por encima de −60 dB del pico). Se mide a partir de la transformada de Fourier de tiempo corto del audio.",
    mpSpectralWhy: "Los archivos lossless de CD (44,1 kHz) deben tener contenido llegando a ~22 kHz, cerca del límite de Nyquist. Los codecs lossy como MP3 y AAC cortan las frecuencias en su techo de codificación, dejando un escalón espectral abrupto. Un techo muy por debajo del límite de Nyquist es una fuerte huella de upscaling desde una fuente lossy.",
    mpDRTitle: "Rango dinámico",
    mpDRWhat: "La relación entre el pico más alto y el promedio de los bloques RMS de 3 segundos más fuertes, expresada en dB.",
    mpDRWhy: "DR alto (> 14 dB) sugiere audio sin comprimir o con masterización ligera. Las producciones comerciales modernas suelen tener DR 6–10 dB debido a la normalización de loudness. DR muy bajo (< 6 dB) puede indicar que la fuente era un stream comprimido, una masterización con brick-wall extremo, o una re-codificación de un archivo lossy.",
    mpClipTitle: "Detección de clipping",
    mpClipWhat: "Cuenta las rachas de 3 o más muestras de audio consecutivas al nivel digital máximo (amplitud ≥ 0,9999 de la escala completa), indicando formas de onda con tope plano.",
    mpClipWhy: "El clipping causa distorsión audible y es señal de que el audio fue normalizado más allá de 0 dBFS o proviene de una fuente ya saturada. También puede ocurrir al recodificar un archivo con ajustes de ganancia incorrectos. Si bien el clipping solo no prueba que un archivo sea lossy, su presencia junto a otros problemas es una señal de alerta.",
    mpBitTitle: "Análisis de profundidad 24 bits",
    mpBitWhat: "Verifica si un FLAC de 24 bits usa realmente la precisión completa de 24 bits, midiendo qué porcentaje de muestras cae exactamente en la cuadrícula entera de 16 bits.",
    mpBitWhy: "Cuando una fuente de 16 bits (CD) se sube a 24 bits, los valores de muestra siguen en la cuadrícula de 16 bits — los 8 bits extra contienen ceros o ruido dither mínimo. Si más del 97% de las muestras se alinean a la cuadrícula de 16 bits, el archivo es casi seguramente un falso 24 bits. Las grabaciones genuinas de 24 bits tienen precisión sub-16-bit significativa.",
    mpCurrentValue: "Valor actual:",
    mpClose: "Cerrar",
    mpWhatLabel: "\u00bfQU\u00c9 MIDE?",
    mpWhyLabel: "\u00bfPOR QU\u00c9 IMPORTA?",
    analyzingNext: "Analizando siguiente archivo…",
    processingLabel: "Procesando",
    langToggle: "EN",
    filterAll: "Todos",
    filterGenuine: "Genuinos",
    filterFake: "Falsos",
    filterInconclusive: "Dudosos",
    filterNoMatch: "Ningún resultado coincide con este filtro.",
  },
};

// ─── Backend issue string translator ──────────────────────────────────────────
// The Python engine (motor_flac.py) emits all "problemas" strings in Spanish.
// This map translates them to English when lang === "en".
// Patterns with dynamic values use regex capture groups.

interface IssuePattern {
  /** Matches the raw Spanish string from Python */
  test: RegExp;
  en: (m: RegExpMatchArray) => string;
  es: (m: RegExpMatchArray) => string;
}

const ISSUE_PATTERNS: IssuePattern[] = [
  // "techo espectral 20 kHz (bajo para 48 kHz Nyquist)"
  {
    test: /techo espectral (\d+) kHz \(bajo para (\d+) kHz Nyquist\)/,
    en:   (m) => `spectral ceiling ${m[1]} kHz (low for ${m[2]} kHz Nyquist)`,
    es:   (m) => `techo espectral ${m[1]} kHz (bajo para ${m[2]} kHz Nyquist)`,
  },
  // "techo solo 20 kHz — muy probable upscale"
  {
    test: /techo solo (\d+) kHz — muy probable upscale/,
    en:   (m) => `ceiling only ${m[1]} kHz — very likely upscale`,
    es:   (m) => `techo solo ${m[1]} kHz — muy probable upscale`,
  },
  // "corte artificial a 22.0 kHz (firma de codec lossy)"
  {
    test: /corte artificial a ([\d.]+) kHz \(firma de codec lossy\)/,
    en:   (m) => `artificial cut at ${m[1]} kHz (lossy codec signature)`,
    es:   (m) => `corte artificial a ${m[1]} kHz (firma de codec lossy)`,
  },
  // "ratio altas/medias muy bajo (0.00123)"
  {
    test: /ratio altas\/medias muy bajo \(([\d.]+)\)/,
    en:   (m) => `high/mid frequency ratio very low (${m[1]})`,
    es:   (m) => `ratio altas/medias muy bajo (${m[1]})`,
  },
  // "LSBs parcialmente inactivos (87.3% en grid 16-bit)"
  {
    test: /LSBs parcialmente inactivos \(([\d.]+)% en grid 16-bit\)/,
    en:   (m) => `LSBs partially inactive (${m[1]}% on 16-bit grid)`,
    es:   (m) => `LSBs parcialmente inactivos (${m[1]}% en grid 16-bit)`,
  },
  // "muestras alineadas a cuadrícula 16-bit (97.1%) — upscale"
  {
    test: /muestras alineadas a cuadr[íi]cula 16-bit \(([\d.]+)%\) — upscale/,
    en:   (m) => `samples aligned to 16-bit grid (${m[1]}%) — upscale`,
    es:   (m) => `muestras alineadas a cuadrícula 16-bit (${m[1]}%) — upscale`,
  },
  // "sin MD5 registrado — encoder de baja calidad o metadatos alterados"
  {
    test: /sin MD5 registrado — encoder de baja calidad o metadatos alterados/,
    en:   () => `no MD5 checksum — low-quality encoder or altered metadata`,
    es:   () => `sin MD5 registrado — encoder de baja calidad o metadatos alterados`,
  },
];

export function translateIssue(raw: string, lang: Lang): string {
  for (const p of ISSUE_PATTERNS) {
    const m = raw.match(p.test);
    if (m) return lang === "en" ? p.en(m) : p.es(m);
  }
  return raw; // unknown pattern — return as-is
}
