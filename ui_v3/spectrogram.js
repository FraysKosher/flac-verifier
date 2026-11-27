/* Interactive Canvas 2D spectrogram renderer (Magma LUT, Full/Upper zoom,
   live kHz · dBFS · mm:ss crosshair, Engine 17.3 forensic markers, and full
   Spek-style Lightbox axes + dBFS color bar). */

const MAGMA_STOPS = [
  [0.00, [8, 8, 14]],
  [0.22, [34, 17, 68]],
  [0.45, [108, 30, 108]],
  [0.68, [196, 62, 86]],
  [0.86, [244, 144, 72]],
  [1.00, [252, 238, 176]],
];

const MAGMA_LUT = new Uint8Array(256 * 3);
for (let i = 0; i < 256; i++) {
  const v = i / 255;
  let rgb = [252, 238, 176];
  for (let s = 1; s < MAGMA_STOPS.length; s++) {
    if (v <= MAGMA_STOPS[s][0]) {
      const t = (v - MAGMA_STOPS[s - 1][0]) / (MAGMA_STOPS[s][0] - MAGMA_STOPS[s - 1][0]);
      const a = MAGMA_STOPS[s - 1][1];
      const b = MAGMA_STOPS[s][1];
      rgb = [
        Math.round(a[0] + (b[0] - a[0]) * t),
        Math.round(a[1] + (b[1] - a[1]) * t),
        Math.round(a[2] + (b[2] - a[2]) * t),
      ];
      break;
    }
  }
  MAGMA_LUT[i * 3] = rgb[0];
  MAGMA_LUT[i * 3 + 1] = rgb[1];
  MAGMA_LUT[i * 3 + 2] = rgb[2];
}

function decodeBase64Bytes(b64) {
  const binary = atob(b64);
  const len = binary.length;
  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

function formatClock(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

export function renderInteractiveSpectrogram(canvas, readoutEl, rangeEl, specData, options = {}) {
  if (!canvas || !specData || !specData.ok || !specData.grid_b64) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const W = canvas.width;
  const H = canvas.height;
  const gw = Number(specData.width) || 256;
  const gh = Number(specData.height) || 160;
  const nyq = Number(specData.nyquist_hz) || 22050;
  const durationS = Number(specData.duration_s) || 0;
  const minDb = Number(specData.min_db ?? -130);
  const maxDb = Number(specData.max_db ?? 0);
  const zoom = options.zoom === "high" ? "high" : "full";
  const showMarkers = Boolean(options.overlay);
  const showAxes = Boolean(options.axes);
  const markers = Array.isArray(specData.markers) ? specData.markers : [];

  const dpr = showAxes ? Math.min(2, window.devicePixelRatio || 1) : 1;
  const padL = showAxes ? Math.round(64 * dpr) : 0;
  const padR = showAxes ? Math.round(82 * dpr) : 0;
  const padT = showAxes ? Math.round(14 * dpr) : 0;
  const padB = showAxes ? Math.round(32 * dpr) : 0;
  const plotW = Math.max(16, W - padL - padR);
  const plotH = Math.max(16, H - padT - padB);

  const minF = zoom === "high" ? Math.round(nyq * 0.45) : 0;
  const maxF = nyq;
  if (rangeEl) {
    rangeEl.textContent = `${(minF / 1000).toFixed(1)} – ${(maxF / 1000).toFixed(1)} kHz`;
  }

  const grid = specData._decodedGrid || decodeBase64Bytes(specData.grid_b64);
  specData._decodedGrid = grid;

  // Render raster strictly for [plotW, plotH] from [gh, gw] uint8 dBFS matrix.
  const img = ctx.createImageData(plotW, plotH);
  const pixels = img.data;
  const minRowF = (minF / nyq) * (gh - 1);
  const maxRowF = (maxF / nyq) * (gh - 1);

  for (let y = 0; y < plotH; y++) {
    const fracFromTop = y / Math.max(1, plotH - 1);
    const rf = maxRowF - fracFromTop * (maxRowF - minRowF);
    const r0 = Math.max(0, Math.min(gh - 1, Math.floor(rf)));
    const r1 = Math.min(gh - 1, r0 + 1);
    const rt = rf - r0;

    for (let x = 0; x < plotW; x++) {
      const cf = (x / Math.max(1, plotW - 1)) * (gw - 1);
      const c0 = Math.max(0, Math.min(gw - 1, Math.floor(cf)));
      const c1 = Math.min(gw - 1, c0 + 1);
      const ct = cf - c0;

      const v00 = grid[r0 * gw + c0];
      const v10 = grid[r0 * gw + c1];
      const v01 = grid[r1 * gw + c0];
      const v11 = grid[r1 * gw + c1];
      const top = v00 + (v10 - v00) * ct;
      const bot = v01 + (v11 - v01) * ct;
      const u8 = Math.max(0, Math.min(255, Math.round(top + (bot - top) * rt)));

      const pIdx = (y * plotW + x) * 4;
      pixels[pIdx] = MAGMA_LUT[u8 * 3];
      pixels[pIdx + 1] = MAGMA_LUT[u8 * 3 + 1];
      pixels[pIdx + 2] = MAGMA_LUT[u8 * 3 + 2];
      pixels[pIdx + 3] = 255;
    }
  }

  // Pre-render vertical Magma dBFS color legend bar for Lightbox mode.
  let legendImg = null;
  const barW = Math.round(14 * dpr);
  const barX = padL + plotW + Math.round(14 * dpr);
  if (showAxes) {
    legendImg = ctx.createImageData(barW, plotH);
    const lp = legendImg.data;
    for (let y = 0; y < plotH; y++) {
      const u8 = Math.max(0, Math.min(255, Math.round((1 - y / Math.max(1, plotH - 1)) * 255)));
      const r = MAGMA_LUT[u8 * 3];
      const g = MAGMA_LUT[u8 * 3 + 1];
      const b = MAGMA_LUT[u8 * 3 + 2];
      for (let x = 0; x < barW; x++) {
        const idx = (y * barW + x) * 4;
        lp[idx] = r;
        lp[idx + 1] = g;
        lp[idx + 2] = b;
        lp[idx + 3] = 255;
      }
    }
  }

  const dbAtPlot = (px, py) => {
    const fracFromTop = Math.max(0, Math.min(1, py / Math.max(1, plotH - 1)));
    const row = Math.max(0, Math.min(gh - 1, Math.round(maxRowF - fracFromTop * (maxRowF - minRowF))));
    const col = Math.max(0, Math.min(gw - 1, Math.round((px / Math.max(1, plotW - 1)) * (gw - 1))));
    const u8 = grid[row * gw + col];
    return minDb + (u8 / 255) * (maxDb - minDb);
  };

  const drawOverlay = (hoverPx = null, hoverPy = null) => {
    if (showAxes) {
      ctx.fillStyle = "#0b0d11";
      ctx.fillRect(0, 0, W, H);
    }
    ctx.putImageData(img, padL, padT);

    // Frequency gridlines & Y axis
    const freqSpan = maxF - minF;
    const step = freqSpan > 60000 ? 16000 : (freqSpan > 30000 ? 10000 : (freqSpan > 16000 ? 4000 : 2000));
    const fontSize = showAxes ? Math.round(11.5 * dpr) : 18;
    ctx.font = `500 ${fontSize}px ui-monospace, Consolas, monospace`;

    if (showAxes) {
      // Plot frame border
      ctx.strokeStyle = "rgba(255,255,255,0.16)";
      ctx.lineWidth = 1;
      ctx.strokeRect(padL - 0.5, padT - 0.5, plotW + 1, plotH + 1);

      // Left kHz ticks (including 0/minF and maxF)
      const ticks = [minF];
      const firstTick = Math.ceil((minF + 1) / step) * step;
      for (let f = firstTick; f < maxF - step * 0.25; f += step) ticks.push(f);
      ticks.push(maxF);

      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (const f of ticks) {
        const y = Math.round(padT + ((maxF - f) / Math.max(1, freqSpan)) * plotH);
        if (f > minF && f < maxF) {
          ctx.strokeStyle = "rgba(255,255,255,0.09)";
          ctx.beginPath();
          ctx.moveTo(padL, y);
          ctx.lineTo(padL + plotW, y);
          ctx.stroke();
        }
        ctx.strokeStyle = "rgba(255,255,255,0.32)";
        ctx.beginPath();
        ctx.moveTo(padL - 5, y);
        ctx.lineTo(padL, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(220,228,236,0.78)";
        const khz = f / 1000;
        const label = Math.abs(khz - Math.round(khz)) < 0.05 ? `${Math.round(khz)} kHz` : `${khz.toFixed(1)} k`;
        ctx.fillText(label, padL - 9, y);
      }

      // Bottom time axis (mm:ss)
      const timeSteps = durationS > 0 ? 6 : 0;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (let i = 0; i <= timeSteps; i++) {
        const frac = i / Math.max(1, timeSteps);
        const x = Math.round(padL + frac * plotW);
        if (i > 0 && i < timeSteps) {
          ctx.strokeStyle = "rgba(255,255,255,0.07)";
          ctx.beginPath();
          ctx.moveTo(x, padT);
          ctx.lineTo(x, padT + plotH);
          ctx.stroke();
        }
        ctx.strokeStyle = "rgba(255,255,255,0.32)";
        ctx.beginPath();
        ctx.moveTo(x, padT + plotH);
        ctx.lineTo(x, padT + plotH + 5);
        ctx.stroke();
        ctx.fillStyle = "rgba(220,228,236,0.75)";
        ctx.fillText(formatClock(frac * durationS), x, padT + plotH + 9);
      }

      // Right dBFS color bar legend
      if (legendImg) {
        ctx.putImageData(legendImg, barX, padT);
        ctx.strokeStyle = "rgba(255,255,255,0.22)";
        ctx.strokeRect(barX - 0.5, padT - 0.5, barW + 1, plotH + 1);
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        const dbTicks = [0, -30, -60, -90, -130];
        for (const dbVal of dbTicks) {
          const frac = (maxDb - dbVal) / Math.max(1, maxDb - minDb);
          const y = Math.round(padT + frac * plotH);
          ctx.fillStyle = "rgba(220,228,236,0.75)";
          ctx.fillText(`${dbVal} dB`, barX + barW + 7, Math.max(padT + 6, Math.min(padT + plotH - 6, y)));
        }
      }
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
    } else {
      for (let f = step; f < maxF; f += step) {
        if (f <= minF) continue;
        const y = Math.round(((maxF - f) / freqSpan) * plotH);
        ctx.strokeStyle = "rgba(255,255,255,0.11)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(plotW, y);
        ctx.stroke();
        ctx.fillStyle = "rgba(255,255,255,0.56)";
        ctx.fillText(`${Math.round(f / 1000)}k`, 10, Math.max(18, y - 5));
      }
    }

    // Forensic & reference markers
    if (showMarkers && markers.length) {
      const usedY = [];
      for (const m of markers.slice(0, 3)) {
        const freq = Number(m.freq_hz);
        if (!Number.isFinite(freq) || freq < minF || freq > maxF) continue;
        const my = Math.round(padT + ((maxF - freq) / Math.max(1, freqSpan)) * plotH);
        const subtle = Boolean(m.subtle || m.kind === "reference");
        ctx.save();
        ctx.strokeStyle = subtle ? "rgba(226, 234, 242, 0.48)" : "#72d8e4";
        ctx.lineWidth = subtle ? 1.4 : 2;
        ctx.setLineDash(subtle ? [5, 5] : [8, 6]);
        ctx.beginPath();
        ctx.moveTo(padL, my);
        ctx.lineTo(padL + plotW, my);
        ctx.stroke();
        ctx.setLineDash([]);

        const padX = showAxes ? Math.round(9 * dpr) : 11;
        const badgeH = showAxes ? Math.round(22 * dpr) : 26;
        const text = String(m.label || "");
        if (text) {
          ctx.font = `500 ${showAxes ? Math.round(11.5 * dpr) : 17}px ui-monospace, Consolas, monospace`;
          const tw = ctx.measureText(text).width;
          const bx = subtle && showAxes
            ? padL + Math.round(10 * dpr)
            : Math.max(padL + 10, padL + plotW - tw - padX * 2 - Math.round(10 * dpr));
          let by = Math.max(padT + 8, Math.min(padT + plotH - badgeH - 8, my - badgeH - 6));
          for (const prevY of usedY) {
            if (Math.abs(by - prevY) < badgeH + 4) {
              by = Math.min(padT + plotH - badgeH - 8, prevY + badgeH + 5);
            }
          }
          usedY.push(by);
          ctx.fillStyle = subtle ? "rgba(14, 18, 24, 0.80)" : "rgba(10, 14, 18, 0.88)";
          ctx.strokeStyle = subtle ? "rgba(210, 220, 232, 0.34)" : "rgba(114, 216, 228, 0.60)";
          ctx.lineWidth = 1.3;
          ctx.beginPath();
          if (typeof ctx.roundRect === "function") {
            ctx.roundRect(bx, by, tw + padX * 2, badgeH, 6);
          } else {
            ctx.rect(bx, by, tw + padX * 2, badgeH);
          }
          ctx.fill();
          ctx.stroke();
          ctx.fillStyle = subtle ? "#cfd8e2" : "#d9f4f7";
          ctx.fillText(text, bx + padX, by + Math.round(badgeH * 0.68));
        }
        ctx.restore();
      }
    }

    if (hoverPx !== null && hoverPy !== null) {
      const cx = padL + hoverPx;
      const cy = padT + hoverPy;
      ctx.strokeStyle = "rgba(255,255,255,0.52)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx, padT);
      ctx.lineTo(cx, padT + plotH);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(padL, cy);
      ctx.lineTo(padL + plotW, cy);
      ctx.stroke();
    }
  };

  drawOverlay();

  canvas.onmousemove = (e) => {
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const sx = ((e.clientX - r.left) / r.width) * W;
    const sy = ((e.clientY - r.top) / r.height) * H;
    const px = Math.round(sx - padL);
    const py = Math.round(sy - padT);
    if (px < 0 || px >= plotW || py < 0 || py >= plotH) {
      if (readoutEl) readoutEl.textContent = "Hover to inspect Hz / dBFS";
      drawOverlay();
      return;
    }
    const f = maxF - (py / Math.max(1, plotH - 1)) * (maxF - minF);
    const sec = (px / Math.max(1, plotW - 1)) * durationS;
    const db = dbAtPlot(px, py).toFixed(1);
    if (readoutEl) {
      readoutEl.textContent = `${(f / 1000).toFixed(2)} kHz · ${db} dBFS · ${formatClock(sec)}`;
    }
    drawOverlay(px, py);
  };

  canvas.onmouseleave = () => {
    if (readoutEl) {
      readoutEl.textContent = "Hover to inspect Hz / dBFS";
    }
    drawOverlay();
  };
}
