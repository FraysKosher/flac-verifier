"""Design tokens for the FLAC VERIFIER interface (see DESIGN.md, section 5).

This module holds values and nothing else: no widget code, no analysis, no
window. `gui.py` reads the tokens and builds the interface from them, which is
what keeps a single source of truth for colour, type, spacing and radii.

Two deliberate implementation choices:

  · The palettes are plain dicts keyed by the dotted token names of DESIGN.md,
    so a token can be traced from the design brief to the code without a
    translation table in between.
  · `font()` and `mono()` cache `CTkFont` instances, because CustomTkinter
    creates a Tk font object per call and hundreds of them slow the window down.
    The cache is keyed by interpreter, so opening several windows in one process
    (the test suite does) never reuses a font from a destroyed Tk root.

Tk only knows the weights `normal` and `bold`: the numeric weights of the design
scale (500, 600, 700) are mapped onto those two, which is why 500 and 600 look
the same in Tk and 700 looks bold.
"""
from __future__ import annotations

from typing import Any

# ─── Colour ─────────────────────────────────────────────────────────────────

DARK = {
    "bg.canvas": "#0E1113",
    "bg.surface": "#151A1D",
    "bg.raised": "#1B2226",
    "bg.hover": "#222B30",
    "border.subtle": "#2A353C",
    "border.strong": "#3A4852",
    "text.primary": "#E9EFF2",
    "text.secondary": "#A8B6BE",
    "text.tertiary": "#74828B",
    "accent": "#63B7C2",
    "accent.hover": "#79C8D2",
    "accent.pressed": "#4E9DA8",
    "on.accent": "#06282C",
    "focus": "#9BE3EA",
    "ok": "#63D08D",
    "ok.bg": "#102A1B",
    "ok.border": "#1F5C38",
    "warn": "#E8BC63",
    "warn.bg": "#2E2410",
    "warn.border": "#6B501C",
    "bad": "#E28374",
    "bad.bg": "#2E1614",
    "bad.border": "#6B2C25",
    "unknown": "#9AA7B0",
    "unknown.bg": "#1D252A",
    "unknown.border": "#38464F",
    "danger": "#E06C6C",
    "danger.bg": "#2A1414",
}

# The light theme of DESIGN.md section 5.2 only defines the core surfaces, text
# and accent: its verdict tints are designed together with the theme (phase 4).
# A token that is not here raises KeyError on purpose instead of quietly falling
# back to the dark value, so phase 4 cannot ship a half-lit window by accident.
LIGHT = {
    "bg.canvas": "#F4F6F7",
    "bg.surface": "#FFFFFF",
    "bg.raised": "#F9FAFB",
    "bg.hover": "#EEF2F3",
    "border.subtle": "#DCE3E6",
    "border.strong": "#C3CFD4",
    "text.primary": "#132028",
    "text.secondary": "#53646D",
    "text.tertiary": "#7C8B93",
    "accent": "#2F7F89",
    "on.accent": "#FFFFFF",
}

PALETTE = {"dark": DARK, "light": LIGHT}
MODE = "dark"


def color(token: str, mode: str | None = None) -> str:
    """Hex value of a token in the given (or current) appearance mode."""
    return PALETTE[mode or MODE][token]


def mix(color_hex: str, background_hex: str, opacity: float) -> str:
    """`color_hex` painted over `background_hex` at `opacity` (0..1).

    CustomTkinter has no per-widget opacity, so the disabled state of a filled
    button is approximated by blending its fill with the surface behind it.
    """
    foreground = _rgb(color_hex)
    background = _rgb(background_hex)
    blended = tuple(round(opacity * fg + (1.0 - opacity) * bg)
                    for fg, bg in zip(foreground, background))
    return "#" + "".join(f"{channel:02X}" for channel in blended)


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


# ─── Typography ─────────────────────────────────────────────────────────────

FONT_UI_CANDIDATES = ("Segoe UI Variable Text", "Segoe UI")
FONT_MONO_CANDIDATES = ("Cascadia Mono", "Consolas")
FONT_UI = FONT_UI_CANDIDATES[0]
FONT_MONO = FONT_MONO_CANDIDATES[0]

_FONTS: dict[tuple[Any, ...], Any] = {}


def resolve_fonts(window: Any = None) -> None:
    """Picks the first installed family of each list.

    Windows 11 ships Segoe UI Variable and Cascadia Mono; Windows 10 ships
    neither, so the window falls back to Segoe UI and Consolas. Called once,
    after the Tk root exists and before any widget is built."""
    global FONT_UI, FONT_MONO
    try:
        import tkinter.font
        families = set(tkinter.font.families(window))
    except Exception:                      # no Tk: keep the preferred names
        return
    FONT_UI = next((name for name in FONT_UI_CANDIDATES if name in families),
                   FONT_UI_CANDIDATES[-1])
    FONT_MONO = next((name for name in FONT_MONO_CANDIDATES if name in families),
                     FONT_MONO_CANDIDATES[-1])
    _FONTS.clear()


def _tk_weight(weight: int | str) -> str:
    if isinstance(weight, str):
        return "bold" if weight.lower() in ("bold", "semibold") else "normal"
    return "bold" if weight >= 600 else "normal"


def _font(family: str, size: int, weight: int | str) -> Any:
    import customtkinter as ctk
    import tkinter

    root = tkinter._default_root
    interpreter = root.tk.interpaddr() if root is not None else None
    key = (family, size, _tk_weight(weight), interpreter)
    cached = _FONTS.get(key)
    if cached is None:
        cached = ctk.CTkFont(family=family, size=size, weight=_tk_weight(weight))
        _FONTS[key] = cached
    return cached


def font(size: int, weight: int | str = "normal") -> Any:
    """Interface font (Segoe UI Variable Text, Segoe UI fallback), cached."""
    return _font(FONT_UI, size, weight)


def mono(size: int, weight: int | str = "normal") -> Any:
    """Tabular font for data and the log (Cascadia Mono, Consolas fallback)."""
    return _font(FONT_MONO, size, weight)


# ─── Spacing and radii ──────────────────────────────────────────────────────

# The design scale is 4, 8, 12, 16, 20, 24, 32, 40. The names are the ones
# DESIGN.md uses in words ("gap between sections 24"), not a new scale.
SPACE = {
    "2xs": 4,
    "xs": 8,
    "sm": 12,
    "md": 16,
    "lg": 20,
    "xl": 24,
    "2xl": 32,
    "3xl": 40,
}

RADIUS = {
    "xs": 4,      # checkbox box of DESIGN.md 7.4 (18x18, radius 4)
    "sm": 6,
    "md": 8,
    "lg": 12,
    "xl": 16,
    "pill": 999,
}
