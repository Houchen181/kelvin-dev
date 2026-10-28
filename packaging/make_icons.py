"""Draw the Kelvin mark (same geometry as web/static/kelvin.svg) and write the app icons.

    python packaging/make_icons.py      # writes packaging/icons/kelvin.png, .ico, .icns and the iPhone app's icon
"""
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 2048                      # drawn large, then scaled down for smooth edges
K = SIZE / 100.0                 # svg units -> pixels
FROST, ICE, FLAKE = "#e8f2fc", "#2f6fb0", "#8cc4f2"
OUT = Path(__file__).parent / "icons"
IOS = Path(__file__).parent.parent / "ios" / "Kelvin" / "Assets.xcassets" / "AppIcon.appiconset"
MARK = IOS.parent / "Mark.imageset"   # the mark on its rounded tile, for the app's welcome screen


def p(x, y):
    """Logo coordinates -> pixels (the svg's translate(9.8 4.9) scale(0.82))."""
    return ((9.8 + 0.82 * x) * K, (4.9 + 0.82 * y) * K)


def quad(a, c, b, n=48):
    return [((1 - t) ** 2 * a[0] + 2 * (1 - t) * t * c[0] + t * t * b[0],
             (1 - t) ** 2 * a[1] + 2 * (1 - t) * t * c[1] + t * t * b[1]) for t in (i / n for i in range(n + 1))]


def stroke(d, points, width, color):
    """A round-capped, round-joined stroke, painted as closely spaced dots (wide polylines tear in Pillow)."""
    pts = [p(*q) for q in points]
    r = width * 0.82 * K / 2
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 / (r / 6)))
        for i in range(n + 1):
            x, y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
            d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def draw(square: bool = False) -> Image.Image:
    """The mark on its rounded tile, or on a full square (iOS rounds the corners itself and wants no transparency)."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=0 if square else 22 * K, fill=FROST)
    for seg in [((24, 20), (24, 38)), ((16, 24.5), (32, 33.5)), ((16, 33.5), (32, 24.5))]:
        stroke(d, seg, 2.6, FLAKE)
    stroke(d, quad((18, 66), (16, 86), (40, 86)) + quad((40, 86), (60, 86), (68, 76))[1:], 7, ICE)
    (x0, y0), (x1, y1) = p(16, 43), p(76, 81)
    d.ellipse([x0, y0, x1, y1], fill=ICE)
    d.polygon([p(60, 43), p(62, 30), p(69, 38)], fill=ICE)
    d.polygon([p(82, 43), p(80, 30), p(73, 38)], fill=ICE)
    (x0, y0), (x1, y1) = p(57, 36), p(85, 64)
    d.ellipse([x0, y0, x1, y1], fill=ICE)
    stroke(d, quad((63, 51), (66, 53.6), (69, 51), 16), 1.8, FROST)
    stroke(d, quad((73, 51), (76, 53.6), (79, 51), 16), 1.8, FROST)
    return img


def main() -> None:
    OUT.mkdir(exist_ok=True)
    big = draw().resize((1024, 1024), Image.LANCZOS)
    big.save(OUT / "kelvin.png")
    big.save(OUT / "kelvin.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    big.save(OUT / "kelvin.icns")
    print("wrote", ", ".join(sorted(f.name for f in OUT.iterdir())))
    IOS.mkdir(parents=True, exist_ok=True)
    draw(square=True).resize((1024, 1024), Image.LANCZOS).convert("RGB").save(IOS / "icon-1024.png")
    print("wrote", IOS / "icon-1024.png")
    MARK.mkdir(parents=True, exist_ok=True)
    draw().resize((288, 288), Image.LANCZOS).save(MARK / "mark.png")
    (MARK / "Contents.json").write_text('{\n  "images" : [ { "filename" : "mark.png", "idiom" : "universal" } ],\n'
                                        '  "info" : { "author" : "xcode", "version" : 1 }\n}\n', encoding="utf-8")
    print("wrote", MARK / "mark.png")


if __name__ == "__main__":
    main()
