/* Deterministic editorial album sleeve generator + real WebP cover loader. */

const PALETTES = [
  { bg: "#1c2228", fg: "#d8dee4", accent: "#6d8a96" },
  { bg: "#191c24", fg: "#c8d0dc", accent: "#7b6c8d" },
  { bg: "#26231e", fg: "#ebe3d5", accent: "#b88a4a" },
  { bg: "#261f1b", fg: "#e9dcd0", accent: "#c76c4e" },
  { bg: "#1b2429", fg: "#dce6eb", accent: "#4e9aa6" },
  { bg: "#211d1a", fg: "#ede6dc", accent: "#bfa06c" },
  { bg: "#241919", fg: "#ebe2e2", accent: "#a84c45" },
  { bg: "#1c1a22", fg: "#e2dfea", accent: "#8d6ca8" },
];

const MOTIFS = ["moon", "horizon", "frame", "split", "ripples", "arc", "bars", "grid"];

function hashString(text) {
  let h = 2166136261 >>> 0;
  const s = String(text || "");
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h;
}

export function fallbackSleeveSVG(seedText) {
  const h = hashString(seedText);
  const pal = PALETTES[h % PALETTES.length];
  const motif = MOTIFS[((h >>> 11) ^ (h >>> 3)) % MOTIFS.length];
  const { bg, fg, accent } = pal;

  let shape = "";
  if (motif === "moon") {
    shape = `<circle cx="72" cy="48" r="22" fill="${fg}" opacity="0.88"/><circle cx="78" cy="44" r="20" fill="${bg}"/><line x1="16" y1="92" x2="104" y2="92" stroke="${accent}" stroke-width="1.5" opacity="0.7"/>`;
  } else if (motif === "horizon") {
    shape = `<rect x="0" y="62" width="120" height="58" fill="${accent}" opacity="0.22"/><line x1="14" y1="62" x2="106" y2="62" stroke="${fg}" stroke-width="1" opacity="0.5"/><circle cx="60" cy="44" r="14" stroke="${fg}" stroke-width="1.2" fill="none" opacity="0.55"/>`;
  } else if (motif === "frame") {
    shape = `<rect x="20" y="20" width="80" height="80" stroke="${fg}" stroke-width="1.2" fill="none" opacity="0.55"/><rect x="34" y="34" width="52" height="52" fill="${accent}" opacity="0.28"/><line x1="20" y1="100" x2="100" y2="20" stroke="${accent}" stroke-width="1.2" opacity="0.8"/>`;
  } else if (motif === "split") {
    shape = `<rect x="0" y="0" width="56" height="120" fill="${accent}" opacity="0.26"/><circle cx="56" cy="60" r="24" fill="${fg}" opacity="0.85"/><circle cx="56" cy="60" r="9" fill="${bg}"/>`;
  } else if (motif === "ripples") {
    shape = `<circle cx="60" cy="60" r="40" stroke="${accent}" stroke-width="1.2" fill="none" opacity="0.45"/><circle cx="60" cy="60" r="27" stroke="${fg}" stroke-width="1.2" fill="none" opacity="0.55"/><circle cx="60" cy="60" r="14" fill="${accent}" opacity="0.5"/>`;
  } else if (motif === "arc") {
    shape = `<path d="M22 92 A38 38 0 0 1 98 92" stroke="${accent}" stroke-width="2" fill="none"/><path d="M36 92 A24 24 0 0 1 84 92" stroke="${fg}" stroke-width="1.2" fill="none" opacity="0.55"/><circle cx="60" cy="92" r="4" fill="${fg}"/>`;
  } else if (motif === "bars") {
    shape = `<rect x="24" y="34" width="10" height="54" fill="${fg}" opacity="0.75"/><rect x="42" y="24" width="10" height="64" fill="${accent}" opacity="0.85"/><rect x="60" y="42" width="10" height="46" fill="${fg}" opacity="0.45"/><rect x="78" y="30" width="10" height="58" fill="${fg}" opacity="0.75"/>`;
  } else {
    shape = `<path d="M24 36h72M24 60h72M24 84h72M48 20v80M72 20v80" stroke="${fg}" stroke-width="1" opacity="0.22"/><rect x="48" y="36" width="24" height="24" fill="${accent}" opacity="0.55"/>`;
  }
  return `<svg viewBox="0 0 120 120" width="100%" height="100%" preserveAspectRatio="xMidYMid slice" style="display:block"><rect width="120" height="120" fill="${bg}"/>${shape}</svg>`;
}

export function renderCoverHTML(coverHash, seedText) {
  if (coverHash && /^[0-9a-f]{8,64}$/i.test(coverHash)) {
    const encodedSeed = encodeURIComponent(String(seedText || ""));
    return `<img class="cover-img" src="/covers/${coverHash}.webp" loading="lazy" alt="" data-fallback-seed="${encodedSeed}">`;
  }
  return fallbackSleeveSVG(seedText);
}

export function attachCoverFallbacks(container) {
  if (!container) return;
  container.querySelectorAll("img.cover-img[data-fallback-seed]").forEach((img) => {
    if (img.dataset.boundErr) return;
    img.dataset.boundErr = "1";
    img.addEventListener("error", () => {
      const seed = decodeURIComponent(img.dataset.fallbackSeed || "");
      const parent = img.parentElement;
      if (parent) {
        const wrapper = document.createElement("div");
        wrapper.style.width = "100%";
        wrapper.style.height = "100%";
        wrapper.innerHTML = fallbackSleeveSVG(seed);
        img.replaceWith(wrapper.firstElementChild);
      }
    });
  });
}
