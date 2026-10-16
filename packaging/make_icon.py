"""Draws packaging/stakbridge.ico (a bridge on a blue tile). Run once: python packaging/make_icon.py"""

import struct
import zlib
from pathlib import Path

BLUE, WHITE = (37, 99, 235), (255, 255, 255)


def inside_tile(x: float, y: float, size: float) -> bool:
    radius, margin = size * 0.2, size * 0.04
    lo, hi = margin, size - margin
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    return lo <= x <= hi and lo <= y <= hi and (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2


def on_bridge(x: float, y: float, size: float) -> bool:
    """An arch above the deck, with hangers down to it and two legs below."""
    u, v = x / size, y / size
    deck = 0.62 <= v <= 0.69 and 0.12 <= u <= 0.88
    arch_distance = ((u - 0.5) ** 2 + ((v - 0.66) * 1.45) ** 2) ** 0.5
    arch = 0.30 <= arch_distance <= 0.36 and v <= 0.66
    hangers = any(abs(u - h) <= 0.018 for h in (0.32, 0.41, 0.5, 0.59, 0.68)) and v <= 0.66 and arch_distance <= 0.31
    legs = any(abs(u - h) <= 0.03 for h in (0.2, 0.8)) and 0.66 <= v <= 0.82
    return deck or arch or hangers or legs


def pixels(size: int) -> bytes:
    rows = []
    samples = 4
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            r = g = b = a = 0
            for sy in range(samples):
                for sx in range(samples):
                    x, y = px + (sx + 0.5) / samples, py + (sy + 0.5) / samples
                    if inside_tile(x, y, size):
                        colour = WHITE if on_bridge(x, y, size) else BLUE
                        r, g, b, a = r + colour[0], g + colour[1], b + colour[2], a + 255
            n = samples * samples
            covered = a // 255
            row += bytes([r // covered, g // covered, b // covered, a // n] if covered else [0, 0, 0, 0])
        rows.append(bytes(row))
    return b"".join(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels(size), 9)) + chunk(b"IEND", b"")


def ico(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    images = [png(size) for size in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, image in zip(sizes, images):
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return out + b"".join(images)


if __name__ == "__main__":
    target = Path(__file__).parent / "stakbridge.ico"
    target.write_bytes(ico())
    (Path(__file__).parents[1] / "stakbridge" / "static" / "icon.png").write_bytes(png(256))  # the page's icon
    print(f"wrote {target}")
