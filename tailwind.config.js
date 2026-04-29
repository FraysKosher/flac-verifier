/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // All driven by CSS variables so dark/light toggle works at runtime
        bg:           "rgb(var(--col-bg)           / <alpha-value>)",
        surface:      "rgb(var(--col-surface)       / <alpha-value>)",
        surface2:     "rgb(var(--col-surface2)      / <alpha-value>)",
        border:       "rgb(var(--col-border)        / <alpha-value>)",
        border2:      "rgb(var(--col-border2)       / <alpha-value>)",
        accent:       "rgb(var(--col-accent)        / <alpha-value>)",
        "accent-dim": "rgb(var(--col-accent-dim)    / <alpha-value>)",
        genuine:      "rgb(var(--col-genuine)       / <alpha-value>)",
        probable:     "rgb(var(--col-probable)      / <alpha-value>)",
        doubtful:     "rgb(var(--col-doubtful)      / <alpha-value>)",
        upscale:      "rgb(var(--col-upscale)       / <alpha-value>)",
        muted:        "rgb(var(--col-muted)         / <alpha-value>)",
        subtle:       "rgb(var(--col-subtle)        / <alpha-value>)",
      },
      fontFamily: {
        sans: ['"Segoe UI Variable"', '"Segoe UI"', "system-ui", "sans-serif"],
        mono: ["Consolas", "Monaco", "monospace"],
      },
      animation: {
        "slide-in":   "slideIn 0.35s cubic-bezier(0.22, 1, 0.36, 1) forwards",
        "fade-in":    "fadeIn 0.3s ease forwards",
        "pulse-slow": "pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "bar-fill":   "barFill 0.4s ease forwards",
        "spin-slow":  "spin 1.5s linear infinite",
      },
      keyframes: {
        slideIn: {
          "0%":   { opacity: "0", transform: "translateY(20px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        fadeIn: {
          "0%":   { opacity: "0" },
          "100%": { opacity: "1" },
        },
        barFill: {
          "0%":   { width: "0%" },
          "100%": { width: "var(--bar-width, 0%)" },
        },
      },
      transitionTimingFunction: { spring: "cubic-bezier(0.22, 1, 0.36, 1)" },
    },
  },
  plugins: [],
};
