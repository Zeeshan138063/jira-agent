"""Draws .claude-plugin/icon.png (a ticket with a check mark). Not a runtime dependency:

uv run --with pillow python scripts/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
SCALE = 4
OUT = Path(__file__).resolve().parents[1] / ".claude-plugin" / "icon.png"

BACKGROUND = (18, 94, 107)
TICKET = (255, 255, 255)
ACCENT = (18, 94, 107)
LINE = (190, 215, 220)


def main() -> None:
    s = SIZE * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=BACKGROUND)

    left, right, top, bottom = int(s * 0.17), int(s * 0.83), int(s * 0.28), int(s * 0.72)
    d.rounded_rectangle((left, top, right, bottom), radius=int(s * 0.05), fill=TICKET)
    notch = int(s * 0.055)
    mid = (top + bottom) // 2
    for x in (left, right):
        d.ellipse((x - notch, mid - notch, x + notch, mid + notch), fill=BACKGROUND)

    perf_x = int(s * 0.62)
    for y in range(top + int(s * 0.03), bottom - int(s * 0.02), int(s * 0.05)):
        d.line((perf_x, y, perf_x, y + int(s * 0.025)), fill=LINE, width=int(s * 0.008))

    check = [(0.27, 0.51), (0.35, 0.59), (0.50, 0.41)]
    d.line([(x * s, y * s) for x, y in check], fill=ACCENT, width=int(s * 0.04), joint="curve")
    for x, y in (check[0], check[2]):
        r = int(s * 0.02)
        d.ellipse((x * s - r, y * s - r, x * s + r, y * s + r), fill=ACCENT)

    for y0, x1 in ((0.40, 0.57), (0.48, 0.54)):
        d.rounded_rectangle(
            (int(0.66 * s), int(y0 * s), int(x1 * s + 0.2 * s), int((y0 + 0.025) * s)),
            radius=int(s * 0.012),
            fill=LINE,
        )

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.resize((SIZE, SIZE), Image.LANCZOS).save(OUT, optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
