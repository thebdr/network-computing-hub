"""Render the app icon into a Windows .ico for the .exe.

Runs under MSYS2's Python, the one the build uses, since it already has
GdkPixbuf with the librsvg loader. Same rule as `rcm install-icon`: the
simplified SVG for 32px and below, where the detailed one turns to mush.

The .ico holds PNG-compressed images, which Windows has read since Vista.

    python make_icon.py <out.ico>
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
DETAILED = ROOT / "assets" / "network-computing-hub.svg"
SMALL = ROOT / "assets" / "network-computing-hub-small.svg"
SMALL_UPTO = 32                       # rcm's ICON_SMALL_UPTO
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def png_at(size: int) -> bytes:
    src = SMALL if size <= SMALL_UPTO else DETAILED
    pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(src), size, size)
    if (pixbuf.get_width(), pixbuf.get_height()) != (size, size):
        raise SystemExit(f"{src.name} rendered at {pixbuf.get_width()}x"
                         f"{pixbuf.get_height()}, wanted {size}x{size}")
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    if not ok:
        raise SystemExit(f"could not encode {size}px PNG")
    return bytes(data)


def ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        side = 0 if size >= 256 else size     # 0 means 256 in an ICONDIRENTRY
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32,
                               len(png), offset + len(blobs))
        blobs += png
    return header + entries + blobs


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().splitlines()[-1].strip(), file=sys.stderr)
        return 2
    out = Path(argv[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(ico([(size, png_at(size)) for size in SIZES]))
    print(f"{out} ({len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
