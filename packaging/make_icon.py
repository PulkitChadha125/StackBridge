"""Draws the app icon: the "S" of the ThinkStak logo (white, with its yellow swoosh) on a dark rounded tile.

Writes packaging/stakbridge.ico (EXE, window and taskbar icon), stakbridge/static/icon.png
(StakBridge page icon) and superadmin_panel/static/favicon.png (panel page icon).
Needs Pillow, only when you run it: python packaging/make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
ROOT = HERE.parent
LOGO = HERE / "thinkstak-logo.png"   # the full ThinkStak wordmark, white letters on transparent
S_COLUMNS = (612, 737)                # where the "S" sits in that image (left, right)
TILE = (17, 18, 22, 255)              # near-black


def letter_s() -> Image.Image:
    logo = Image.open(LOGO).convert("RGBA")
    s = logo.crop((S_COLUMNS[0], 0, S_COLUMNS[1], logo.height))
    return s.crop(s.getbbox())


def icon(size: int = 1024) -> Image.Image:
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=round(size * 0.22), fill=TILE)
    s = letter_s()
    height = round(size * 0.66)
    s = s.resize((round(s.width * height / s.height), height), Image.LANCZOS)
    tile.alpha_composite(s, ((size - s.width) // 2, (size - s.height) // 2))
    return tile


if __name__ == "__main__":
    big = icon()
    big.save(HERE / "stakbridge.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    page_icon = big.resize((256, 256), Image.LANCZOS)
    page_icon.save(ROOT / "stakbridge" / "static" / "icon.png", optimize=True)
    page_icon.save(ROOT / "superadmin_panel" / "static" / "favicon.png", optimize=True)
    print(f"wrote {HERE / 'stakbridge.ico'} and the page icons")
