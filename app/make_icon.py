"""Generate the 180 x 180 PNG icon using only the standard library."""

from pathlib import Path
import struct
import zlib


SIZE = 180
OUT = Path(__file__).resolve().parent / "web" / "icon-180.png"


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def pixel(x: int, y: int) -> tuple[int, int, int]:
    # Navy background, two overlapping sheets, and a connecting mint arrow.
    color = (36, 54, 74)
    if 33 <= x < 113 and 36 <= y < 132:
        color = (122, 199, 225)
    if 52 <= x < 135 and 55 <= y < 151:
        color = (245, 249, 250)
    if 62 <= x < 117 and 76 <= y < 85:
        color = (36, 54, 74)
    if 62 <= x < 103 and 96 <= y < 104:
        color = (36, 54, 74)
    if 101 <= x < 144 and 112 <= y < 121:
        color = (21, 117, 92)
    if 132 <= x < 149 and abs(y - 116) <= (x - 131) // 2:
        color = (21, 117, 92)
    return color


def main() -> None:
    raw = b"".join(b"\0" + b"".join(bytes(pixel(x, y)) for x in range(SIZE)) for y in range(SIZE))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    OUT.write_bytes(png)
    print(OUT)


if __name__ == "__main__":
    main()
