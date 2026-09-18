"""Generates the FLAC VERIFIER logo assets from the splash design.

The design is NOT invented here: `splashscreen.html` defines the identity (background
#0f0f0d, five white bars of 8 px with radius 3 and the teal check #4f98a3) and this
script reproduces it in units so that it can be rasterised at the sizes the
application needs. All the proportions come from that file.

    python Logo/generar_logo.py        # rewrites the assets of this folder

It only needs Pillow (it already comes with matplotlib/reportlab).
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

AQUI = os.path.dirname(os.path.abspath(__file__))

# ─── The design, in the units of the splash ──────────────────────────────────
FONDO   = "#0f0f0d"      # splash background
BARRA   = "#ffffff"      # .bar { background: #ffffff }
TEAL    = "#4f98a3"      # .subtitle and the check
ALTURAS = (28, 44, 60, 36, 52)   # .bar:nth-child(n) { height: ... }
ANCHO_BARRA = 8          # .bar { width: 8px }
HUECO       = 5          # .bars { gap: 5px }
RADIO_BARRA = 3          # .bar { border-radius: 3px 3px 0 0 }
ALTO_TOTAL  = 60         # the tallest bar
# .check { right: -4px; bottom: -4px } with a 24x18 SVG and a stroke of 3
CHECK_PUNTOS = ((2, 10), (8, 16), (22, 2))
CHECK_CAJA   = (24, 18)
CHECK_SOBRESALE = 4
TRAZO_CHECK  = 3

ANCHO_UNIDADES = len(ALTURAS) * ANCHO_BARRA + (len(ALTURAS) - 1) * HUECO   # 60
ALTO_UNIDADES  = ALTO_TOTAL + CHECK_SOBRESALE                               # 64
ANCHO_CON_CHECK = ANCHO_UNIDADES + CHECK_SOBRESALE                          # 64


def dibujar_marca(imagen: Image.Image, x: float, y: float, alto: float,
                  barras: bool = True, check: bool = True) -> None:
    """Draws the bars and the check inside a square of side (alto) at (x, y)."""
    escala = alto / ALTO_UNIDADES
    lapiz = ImageDraw.Draw(imagen)

    def px(u: float) -> float:                       # units -> pixels
        return u * escala

    base = y + px(ALTO_TOTAL)                        # the bars rest on the bottom
    if barras:
        for indice, altura in enumerate(ALTURAS):
            izquierda = x + px(indice * (ANCHO_BARRA + HUECO))
            derecha = izquierda + px(ANCHO_BARRA)
            arriba = base - px(altura)
            lapiz.rounded_rectangle(
                [izquierda, arriba, derecha, base],
                radius=max(1, px(RADIO_BARRA)), fill=BARRA, corners=(True, True, False, False))

    if check:
        # the check lives in its own box and sticks out to the right and downwards
        caja_x = x + px(ANCHO_UNIDADES + CHECK_SOBRESALE - CHECK_CAJA[0])
        caja_y = base + px(CHECK_SOBRESALE - CHECK_CAJA[1])
        puntos = [(caja_x + px(px_x), caja_y + px(px_y)) for px_x, px_y in CHECK_PUNTOS]
        grosor = max(1, round(px(TRAZO_CHECK)))
        lapiz.line(puntos, fill=TEAL, width=grosor, joint="curve")
        radio = grosor / 2                          # rounded ends
        for punto in (puntos[0], puntos[-1]):
            lapiz.ellipse([punto[0] - radio, punto[1] - radio,
                           punto[0] + radio, punto[1] + radio], fill=TEAL)


def marca(lado: int, fondo: str | None, margen: float = 0.16) -> Image.Image:
    """Square mark. With `fondo` the rounded tile of the icon is drawn."""
    imagen = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    if fondo:
        lapiz = ImageDraw.Draw(imagen)
        lapiz.rounded_rectangle([0, 0, lado - 1, lado - 1],
                                radius=int(lado * 0.22), fill=fondo)
    disponible = lado * (1 - 2 * margen)
    dibujar_marca(imagen, (lado - disponible) / 2, (lado - disponible) / 2, disponible)
    return imagen


def svg() -> str:
    """Vector version with the same geometry, for GitHub and the web."""
    radio = RADIO_BARRA
    partes = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{ANCHO_CON_CHECK}" '
        f'height="{ALTO_UNIDADES}" viewBox="0 0 {ANCHO_CON_CHECK} {ALTO_UNIDADES}" '
        f'fill="none" role="img" aria-label="FLAC Verifier">',
        f'  <title>FLAC Verifier</title>',
        f'  <rect width="{ANCHO_CON_CHECK}" height="{ALTO_UNIDADES}" rx="14" fill="{FONDO}"/>',
    ]
    for indice, altura in enumerate(ALTURAS):
        x = 2 + indice * (ANCHO_BARRA + HUECO)
        partes.append(f'  <rect x="{x}" y="{60 - altura}" width="{ANCHO_BARRA}" '
                      f'height="{altura}" rx="{radio}" fill="{BARRA}"/>')
    puntos = " ".join(f"{40 + px_x},{46 + px_y}" for px_x, px_y in CHECK_PUNTOS)
    partes.append(f'  <polyline points="{puntos}" stroke="{TEAL}" '
                  f'stroke-width="{TRAZO_CHECK}" stroke-linecap="round" '
                  f'stroke-linejoin="round"/>')
    partes.append('</svg>')
    return "\n".join(partes) + "\n"


def main() -> None:
    generados = []

    cuadrado = marca(1024, FONDO)
    ruta = os.path.join(AQUI, "logo_flac_verifier.png")
    cuadrado.save(ruta)
    generados.append(ruta)

    transparente = marca(1024, None)
    ruta = os.path.join(AQUI, "logo_flac_verifier_transparente.png")
    transparente.save(ruta)
    generados.append(ruta)

    # Small version for iconphoto (Tk reads PNG, but it is not advisable to hand it 1024 px)
    ruta = os.path.join(AQUI, "logo_flac_verifier_128.png")
    marca(128, FONDO).save(ruta)
    generados.append(ruta)

    # Multi-resolution icon for the window and for the future .exe
    ruta = os.path.join(AQUI, "icono_app.ico")
    marca(256, FONDO).save(ruta, format="ICO",
                           sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                  (64, 64), (128, 128), (256, 256)])
    generados.append(ruta)

    # Vector version, for the web and for the GitHub README
    ruta = os.path.join(AQUI, "logo_flac_verifier.svg")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(svg())
    generados.append(ruta)

    print("Logo assets generated from the splash design:")
    for ruta in generados:
        print(f"  {os.path.basename(ruta):38s} {os.path.getsize(ruta) / 1024:8.1f} KB")


if __name__ == "__main__":
    main()
