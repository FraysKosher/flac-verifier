import { useState, useRef, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type {
  AnalysisResult,
  AnalysisSummary,
  AnalysisMode,
  ProgressEvent,
} from "../types";

export interface UseAnalysisReturn {
  results: AnalysisResult[];
  isRunning: boolean;
  progress: number;
  total: number;
  done: number;
  summary: AnalysisSummary | null;
  errorMessage: string | null;
  pdfPath: string | null;
  pdfError: string | null;
  csvPath: string | null;
  csvError: string | null;
  startAnalysis: (path: string, mode: AnalysisMode, png: boolean, pdf: boolean, seg?: number) => Promise<void>;
  resetAnalysis: () => void;
  dismissPdf: () => void;
  dismissCsv: () => void;
}

export function useAnalysis(): UseAnalysisReturn {
  const [results, setResults]             = useState<AnalysisResult[]>([]);
  const [isRunning, setIsRunning]         = useState(false);
  const [total, setTotal]                 = useState(0);
  const [done, setDone]                   = useState(0);
  const [progress, setProgress]           = useState(0);
  const [summary, setSummary]             = useState<AnalysisSummary | null>(null);
  const [errorMessage, setErrorMessage]   = useState<string | null>(null);
  const [pdfPath, setPdfPath]             = useState<string | null>(null);
  const [pdfError, setPdfError]           = useState<string | null>(null);
  const [csvPath, setCsvPath]             = useState<string | null>(null);
  const [csvError, setCsvError]           = useState<string | null>(null);

  const unlistenRef    = useRef<UnlistenFn | null>(null);
  const resultIdCounter = useRef(0);

  const dismissPdf = useCallback(() => { setPdfPath(null); setPdfError(null); }, []);
  const dismissCsv = useCallback(() => { setCsvPath(null); setCsvError(null); }, []);

  const resetAnalysis = useCallback(() => {
    setResults([]);
    setIsRunning(false);
    setTotal(0);
    setDone(0);
    setProgress(0);
    setSummary(null);
    setErrorMessage(null);
    setPdfPath(null);
    setPdfError(null);
    setCsvPath(null);
    setCsvError(null);
    setPdfError(null);
    resultIdCounter.current = 0;
    if (unlistenRef.current) { unlistenRef.current(); unlistenRef.current = null; }
  }, []);

  const computeSummary = useCallback((res: AnalysisResult[]): AnalysisSummary => {
    const s: AnalysisSummary = { genuine: 0, probable: 0, doubtful: 0, upscale: 0, errors: 0 };
    for (const r of res) {
      if (r.error) { s.errors++; continue; }
      const v = r.veredicto ?? "";
      if (v === "LOSSLESS GENUINO")           s.genuine++;
      else if (v === "PROBABLEMENTE LOSSLESS") s.probable++;
      else if (v === "DUDOSO")                 s.doubtful++;
      else if (v === "PROBABLE UPSCALE")       s.upscale++;
      else                                     s.errors++;
    }
    return s;
  }, []);

  const startAnalysis = useCallback(
    async (path: string, mode: AnalysisMode, png: boolean, pdf: boolean, seg?: number) => {
      resetAnalysis();
      setIsRunning(true);
      setErrorMessage(null);

      const unlisten = await listen<string>("flac://progress", (event) => {
        let parsed: ProgressEvent;
        try { parsed = JSON.parse(event.payload) as ProgressEvent; }
        catch { return; }

        if (parsed.tipo === "inicio") {
          setTotal(parsed.total);
        } else if (parsed.tipo === "resultado") {
          const id = ++resultIdCounter.current;
          const newResult: AnalysisResult = {
            id,
            archivo:   parsed.archivo,
            ruta:      parsed.ruta,
            error:     parsed.error,
            meta:      parsed.meta,
            score:     parsed.score,
            veredicto: parsed.veredicto,
            problemas: parsed.problemas,
            clip:      parsed.clip,
            dr:        parsed.dr,
            bdi:       parsed.bdi,
            esp:       parsed.esp,
          };
          setResults((prev) => {
            const next = [...prev, newResult];
            const newDone = next.length;
            setDone(newDone);
            setProgress(parsed.total > 0 ? Math.round((newDone / parsed.total) * 100) : 0);
            return next;
          });
        } else if (parsed.tipo === "fin" || parsed.tipo === "terminated") {
          setResults((prev) => { setSummary(computeSummary(prev)); return prev; });
          setIsRunning(false);
          setProgress(100);
          // Don't unlisten yet — pdf event may still arrive after fin
        } else if (parsed.tipo === "pdf") {
          setPdfPath(parsed.ruta);
          // Now safe to stop listening
          if (unlistenRef.current) { unlistenRef.current(); unlistenRef.current = null; }
        } else if (parsed.tipo === "pdf_error") {
          setPdfError(parsed.mensaje);
          if (parsed.detalle) console.error("[PDF error traceback]\n", parsed.detalle);
          if (unlistenRef.current) { unlistenRef.current(); unlistenRef.current = null; }
        } else if (parsed.tipo === "csv") {
          setCsvPath(parsed.ruta);
        } else if (parsed.tipo === "csv_error") {
          setCsvError(parsed.mensaje);
          if (parsed.detalle) console.error("[CSV error traceback]\n", parsed.detalle);
        } else if (parsed.tipo === "error") {
          setErrorMessage(parsed.mensaje);
          setIsRunning(false);
        }
      });

      unlistenRef.current = unlisten;

      try {
        await invoke("run_analysis", { path, mode, png, pdf, seg });
      } catch (err) {
        setErrorMessage(String(err));
        setIsRunning(false);
        unlisten();
        unlistenRef.current = null;
      }
    },
    [resetAnalysis, computeSummary]
  );

  return {
    results, isRunning, progress, total, done, summary,
    errorMessage, pdfPath, pdfError, csvPath, csvError,
    startAnalysis, resetAnalysis, dismissPdf, dismissCsv,
  };
}
