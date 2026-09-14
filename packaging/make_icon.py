"""Generate the cyRouter icon (.ico + .png) from the same design as the web
favicon: 2x2 rounded squares on a dark rounded-square background.

Outputs:
  packaging/cyrouter.ico  — multi-resolution (16/32/48/64/128/256) for exe
  packaging/cyrouter.png  — single 256x256 for tray/preview

Usage:  python packaging/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

# Brand colours (must match web/index.html favicon SVG exactly)
BG = (13, 15, 22, 255)        # #0d0f16
FG = (0, 229, 160, 255)       # #00e5a0
FG_DIM = (0, 229, 160, 128)   # 50% opacity
FG_FAINT = (0, 229, 160, 77)  # 30% opacity
STROKE = (0, 229, 160, 255)

OUT_DIR = Path(__file__).parent


def _draw(size: int) -> Image.Image:
    """Draw the cyRouter logo at the given pixel size."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Outer rounded square (dark background) — viewBox 0..32, rx=8 → rx = size*0.25
    bg_rx = size * 8 / 32
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=bg_rx, fill=BG)

    # Four inner squares matching the favicon SVG (viewBox 0..32):
    #   TL solid, TR 50%, BL 30%, BR outlined-only
    _fill_cell(d, size, 4, 4, FG)
    _fill_cell(d, size, 18, 4, FG_DIM)
    _fill_cell(d, size, 4, 18, FG_FAINT)
    _stroke_cell(d, size, 18, 18, STROKE)

    return img


def _fill_cell(d, size, ix, iy, fill):
    x0, y0 = size * ix / 32, size * iy / 32
    w = size * 10 / 32
    rx = size * 2.5 / 32
    d.rounded_rectangle((x0, y0, x0 + w, y0 + w), radius=rx, fill=fill)


def _stroke_cell(d, size, ix, iy, stroke):
    x0, y0 = size * ix / 32, size * iy / 32
    w = size * 10 / 32
    rx = size * 2.5 / 32
    sw = max(1, size * 1.5 / 32)
    d.rounded_rectangle(
        (x0, y0, x0 + w, y0 + w), radius=rx, outline=stroke, width=int(sw)
    )


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    sizes = [16, 32, 48, 64, 128, 256]
    images = [_draw(s) for s in sizes]

    # .ico with multiple resolutions embedded (Windows picks the best fit)
    ico_path = OUT_DIR / "cyrouter.ico"
    images[-1].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[:-1],
    )
    print(f"Wrote {ico_path}  ({len(sizes)} sizes)")

    # Single 256x256 PNG for tray / preview
    png_path = OUT_DIR / "cyrouter.png"
    images[-1].save(png_path, format="PNG")
    print(f"Wrote {png_path}")


if __name__ == "__main__":
    main()
