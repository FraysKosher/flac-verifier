import { useState, useEffect } from "react";
import { check, type Update } from "@tauri-apps/plugin-updater";

type Status = "idle" | "checking" | "up-to-date" | "available" | "downloading" | "done" | "error";

interface UpdateCheckerProps {
  /** Run a silent check on mount and only surface UI if an update exists */
  silentOnStartup?: boolean;
}

export function UpdateChecker({ silentOnStartup = false }: UpdateCheckerProps) {
  const [status, setStatus]     = useState<Status>("idle");
  const [update, setUpdate]     = useState<Update | null>(null);
  const [progress, setProgress] = useState(0);
  const [errMsg, setErrMsg]     = useState("");

  // Silent startup check
  useEffect(() => {
    if (!silentOnStartup) return;
    (async () => {
      try {
        const u = await check();
        if (u) { setUpdate(u); setStatus("available"); }
      } catch {
        // Silently ignore — no network, no release manifest, etc.
      }
    })();
  }, [silentOnStartup]);

  const handleCheck = async () => {
    setStatus("checking");
    setUpdate(null);
    setErrMsg("");
    try {
      const u = await check();
      if (u) { setUpdate(u); setStatus("available"); }
      else    { setStatus("up-to-date"); setTimeout(() => setStatus("idle"), 4000); }
    } catch (e) {
      setErrMsg(String(e));
      setStatus("error");
      setTimeout(() => setStatus("idle"), 5000);
    }
  };

  const handleInstall = async () => {
    if (!update) return;
    setStatus("downloading");
    setProgress(0);
    let received = 0;
    try {
      await update.downloadAndInstall((event) => {
        if (event.event === "Started")       { received = 0; setProgress(0); }
        else if (event.event === "Progress") { received += event.data.chunkLength ?? 0; setProgress(Math.min(99, Math.round(received / 1024))); }
        else if (event.event === "Finished") { setProgress(100); }
      });
      setStatus("done");
    } catch (e) {
      setErrMsg(String(e));
      setStatus("error");
      setTimeout(() => setStatus("idle"), 5000);
    }
  };

  // ── Render helpers ──────────────────────────────────────────────────────────

  if (status === "idle") {
    return (
      <button
        onClick={handleCheck}
        className="flex items-center gap-1.5 rounded-md border border-border2 bg-surface2 px-2.5 py-1 text-[11px] text-muted hover:border-accent/40 hover:text-white transition-all"
        title="Check for updates"
      >
        <span className="text-[13px]">↑</span> Update
      </button>
    );
  }

  if (status === "checking") {
    return (
      <span className="flex items-center gap-1.5 text-[11px] text-muted animate-pulse">
        <span className="h-3 w-3 rounded-full border-2 border-accent border-t-transparent animate-spin inline-block" />
        Checking…
      </span>
    );
  }

  if (status === "up-to-date") {
    return (
      <span className="flex items-center gap-1 text-[11px] text-genuine font-medium">
        ✓ Up to date
      </span>
    );
  }

  if (status === "available" && update) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-accent/30 bg-accent/8 px-3 py-1 text-[11px]">
        <span className="text-accent font-semibold">v{update.version} available</span>
        <button
          onClick={handleInstall}
          className="rounded bg-accent px-2 py-0.5 text-[10px] font-bold text-bg hover:bg-accent-dim transition-colors"
        >
          Install
        </button>
        <button onClick={() => setStatus("idle")} className="text-muted hover:text-white ml-1">×</button>
      </div>
    );
  }

  if (status === "downloading") {
    return (
      <div className="flex items-center gap-2 text-[11px] text-accent">
        <span className="h-3 w-3 rounded-full border-2 border-accent border-t-transparent animate-spin inline-block" />
        <span>Downloading… {progress}%</span>
      </div>
    );
  }

  if (status === "done") {
    return (
      <span className="flex items-center gap-1 text-[11px] text-genuine font-medium">
        ✓ Installed — restart to apply
      </span>
    );
  }

  if (status === "error") {
    return (
      <span className="flex items-center gap-1 text-[11px] text-upscale" title={errMsg}>
        ⚠ Update failed
        <button onClick={() => setStatus("idle")} className="ml-1 opacity-60 hover:opacity-100">×</button>
      </span>
    );
  }

  return null;
}
