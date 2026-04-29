// ── JSON Protocol Types ──────────────────────────────────────────────

export interface InitEvent {
  tipo: "inicio";
  total: number;
}

export interface FileMeta {
  valido: boolean;
  sample_rate: number;
  bits_per_sample: number;
  canales: number;
  duracion: number;
  md5: number | null;
}

export interface ClipResult {
  runs_clip: number;
  hay_clipping: boolean;
}

export interface BdiResult {
  aplica: boolean;
  fraccion_16bit_grid?: number;
  conclusion?: string;
  genuino_24?: boolean | null;
  error?: string;
}

export interface EspResult {
  error?: string | null;
  techo_hz?: number;
  corte_artificial?: boolean;
  frecuencia_corte?: number | null;
  ratio?: number;
  var_alta_db?: number;
  separacion_stereo?: number | null;
  nyquist?: number;
  espectrograma?: string | null;
}

export interface ResultEvent {
  tipo: "resultado";
  indice: number;
  total: number;
  archivo: string;
  ruta?: string;
  error?: string;
  meta?: FileMeta;
  score?: number;
  veredicto?: string;
  problemas?: string[];
  clip?: ClipResult | null;
  dr?: number | null;
  bdi?: BdiResult;
  esp?: EspResult;
}

export interface FinEvent {
  tipo: "fin";
}

export interface ErrorEvent {
  tipo: "error";
  mensaje: string;
}

export interface StderrEvent {
  tipo: "stderr";
  mensaje: string;
}

export interface TerminatedEvent {
  tipo: "terminated";
  code: number;
}

export interface PdfEvent {
  tipo: "pdf";
  ruta: string;
}

export interface PdfErrorEvent {
  tipo: "pdf_error";
  mensaje: string;
  detalle?: string;
}

export interface CsvEvent {
  tipo: "csv";
  ruta: string;
}

export interface CsvErrorEvent {
  tipo: "csv_error";
  mensaje: string;
  detalle?: string;
}

export type ProgressEvent =
  | InitEvent
  | ResultEvent
  | FinEvent
  | ErrorEvent
  | StderrEvent
  | TerminatedEvent
  | PdfEvent
  | PdfErrorEvent
  | CsvEvent
  | CsvErrorEvent;

export interface HistoryRow {
  fecha: string;
  carpeta: string;
  archivo: string;
  veredicto: string;
  score: string;
  hz: string;
  bits: string;
  canales: string;
  duracion: string;
  md5_ok: string;
  techo_espectral_khz: string;
  rango_dinamico_db: string;
  clipping: string;
  analisis_24bit: string;
  problemas: string;
}


// ── App State Types ───────────────────────────────────────────────────

export type AnalysisMode = "segundos" | "centro" | "completo";

export type Verdict =
  | "LOSSLESS GENUINO"
  | "PROBABLEMENTE LOSSLESS"
  | "DUDOSO"
  | "PROBABLE UPSCALE"
  | string;

export interface AnalysisResult {
  id: number;
  archivo: string;
  ruta?: string;
  error?: string;
  meta?: FileMeta;
  score?: number;
  veredicto?: Verdict;
  problemas?: string[];
  clip?: ClipResult | null;
  dr?: number | null;
  bdi?: BdiResult;
  esp?: EspResult;
}

export interface AnalysisSummary {
  genuine: number;
  probable: number;
  doubtful: number;
  upscale: number;
  errors: number;
}
