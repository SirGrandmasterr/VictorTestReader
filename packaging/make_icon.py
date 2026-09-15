"""Render packaging/icon.svg into icon.ico (Windows) and icon.icns (macOS).

Usage: ``python packaging/make_icon.py [output directory]`` (default: this
directory). Needs Pillow. The SVG is rasterised with ``cairosvg`` when that
package is installed; otherwise the same design (rounded square, "T",
accent dot - see the SVG) is drawn directly with Pillow, so the build
workflow needs no native cairo library.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SVG = HERE / "icon.svg"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
BACKGROUND = (31, 58, 95, 255)
FOREGROUND = (255, 255, 255, 255)
ACCENT = (242, 177, 52, 255)


def render_with_cairosvg(size):
    try:
        import cairosvg  # noqa: F401 - optional
        import io
    except ImportError:
        return None
    try:
        png = cairosvg.svg2png(url=str(SVG), output_width=size, output_height=size)
    except Exception:
        return None
    return Image.open(io.BytesIO(png)).convert("RGBA")


def draw_with_pillow(size):
    """The icon.svg design at ``size`` pixels, drawn at 4x and downsampled for smooth edges."""
    scale = 4
    px = size * scale
    unit = px / 256.0
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([8 * unit, 8 * unit, 248 * unit, 248 * unit], radius=52 * unit, fill=BACKGROUND)
    draw.rounded_rectangle([60 * unit, 62 * unit, 196 * unit, 92 * unit], radius=6 * unit, fill=FOREGROUND)
    draw.rounded_rectangle([113 * unit, 62 * unit, 143 * unit, 194 * unit], radius=6 * unit, fill=FOREGROUND)
    draw.ellipse([168 * unit, 164 * unit, 204 * unit, 200 * unit], fill=ACCENT)
    return image.resize((size, size), Image.LANCZOS)


def render(size):
    return render_with_cairosvg(size) or draw_with_pillow(size)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    out = Path(argv[0]) if argv else HERE
    out.mkdir(parents=True, exist_ok=True)
    images = {size: render(size) for size in SIZES}
    ico = out / "icon.ico"
    images[256].save(ico, format="ICO", sizes=[(s, s) for s in SIZES if s <= 256])
    icns = out / "icon.icns"
    images[1024].save(icns, format="ICNS")
    png = out / "icon.png"
    images[256].save(png, format="PNG")
    print("wrote", ico, icns, png)
    return 0


if __name__ == "__main__":
    sys.exit(main())
