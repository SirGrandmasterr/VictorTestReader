"""Render packaging/icon.svg into icon.ico (Windows) and icon.icns (macOS).

Usage: ``python packaging/make_icon.py [output directory]`` (default: this
directory). Needs Pillow. The SVG is rasterised with ``cairosvg`` when that
package is installed; otherwise the same design (paper tile, slab-serif "T",
moss underline - see the SVG) is drawn directly with Pillow, so the build
workflow needs no native cairo library. Sizes up to ``INVERT_UP_TO`` pixels
are drawn inverted (clay tile, cream "T") so the icon keeps its contrast on
a taskbar; cairosvg is skipped for them.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SVG = HERE / "icon.svg"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
INVERT_UP_TO = 32
PAPER = (246, 241, 232, 255)
PAPER_EDGE = (227, 218, 203, 255)
CLAY = (168, 74, 37, 255)
MOSS = (61, 122, 79, 255)


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


def _bezier(p0, p1, p2, p3, steps=32):
    points = []
    for index in range(steps + 1):
        t = index / float(steps)
        u = 1 - t
        x = u ** 3 * p0[0] + 3 * u ** 2 * t * p1[0] + 3 * u * t ** 2 * p2[0] + t ** 3 * p3[0]
        y = u ** 3 * p0[1] + 3 * u ** 2 * t * p1[1] + 3 * u * t ** 2 * p2[1] + t ** 3 * p3[1]
        points.append((x, y))
    return points


def draw_with_pillow(size, inverted=False):
    """The icon.svg design at ``size`` pixels, drawn at 4x and downsampled for smooth edges."""
    scale = 4
    px = size * scale
    unit = px / 256.0
    tile, edge, ink, mark = (CLAY, CLAY, PAPER, PAPER) if inverted else (PAPER, PAPER_EDGE, CLAY, MOSS)
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([8 * unit, 8 * unit, 248 * unit, 248 * unit], radius=56 * unit, fill=tile,
                           outline=edge, width=max(1, int(6 * unit)))
    for x, y, w, h in ((58, 60, 140, 28), (58, 60, 14, 44), (184, 60, 14, 44), (113, 60, 30, 126), (96, 172, 64, 16)):
        draw.rounded_rectangle([x * unit, y * unit, (x + w) * unit, (y + h) * unit], radius=4 * unit, fill=ink)
    curve = _bezier((66, 214), (100, 202), (156, 202), (190, 214))
    width = int(13 * unit)
    draw.line([(x * unit, y * unit) for x, y in curve], fill=mark, width=width, joint="curve")
    for x, y in (curve[0], curve[-1]):
        draw.ellipse([x * unit - width / 2, y * unit - width / 2, x * unit + width / 2, y * unit + width / 2], fill=mark)
    return image.resize((size, size), Image.LANCZOS)


def render(size):
    if size <= INVERT_UP_TO:
        return draw_with_pillow(size, inverted=True)
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
