#!/usr/bin/env python3
"""Regenerate assets/social-preview.png (1280x640) from the icon SVG.

GitHub has no API for the social preview; the output is uploaded once by hand
in the repo settings. Re-run this whenever the icon or tagline changes.
"""
import gi
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import tempfile

HERE = Path(__file__).resolve().parent
W, H = 1280, 640
img = Image.new("RGB", (W, H))
top, bot = (0x3a, 0x42, 0x4b), (0x24, 0x2a, 0x30)
px = img.load()
for y in range(H):
    t = y / (H - 1)
    c = tuple(int(a + (b - a) * t) for a, b in zip(top, bot))
    for x in range(W):
        px[x, y] = c

pb = GdkPixbuf.Pixbuf.new_from_file_at_size(
    str(HERE / "network-computing-hub.svg"), 440, 440)
tmp = tempfile.mkstemp(suffix=".png")[1]
pb.savev(tmp, "png", [], [])
icon = Image.open(tmp).convert("RGBA")
Path(tmp).unlink()
img.paste(icon, (80, (H - 440) // 2), icon)

d = ImageDraw.Draw(img)
UB = "/usr/share/fonts/truetype/ubuntu/Ubuntu-B.ttf"
UR = "/usr/share/fonts/truetype/ubuntu/Ubuntu-R.ttf"

def fit(text, path, size, maxw):
    while size > 20:
        f = ImageFont.truetype(path, size)
        if d.textbbox((0, 0), text, font=f)[2] <= maxw:
            return f
        size -= 2
    return ImageFont.truetype(path, size)

X, MAXW = 580, 640
d.text((X, 178), "Network Computing Hub",
       font=fit("Network Computing Hub", UB, 68, MAXW), fill="#ffffff")
fs = fit("launched with the clients you already use.", UR, 34, MAXW)
d.text((X, 268), "One place for your remote connections,", font=fs, fill="#c9d1d9")
d.text((X, 312), "launched with the clients you already use.", font=fs, fill="#c9d1d9")

fchip = ImageFont.truetype(UB, 28)
cx = X
for label, colour in (("RDP", "#4e9a25"), ("VNC", "#2b6cb0"),
                      ("Radmin", "#d97a1f"), ("+ yours", "#5a6470")):
    w = d.textbbox((0, 0), label, font=fchip)[2]
    d.rounded_rectangle((cx, 380, cx + w + 36, 430), radius=12, fill=colour)
    d.text((cx + 18, 391), label, font=fchip, fill="#ffffff")
    cx += w + 36 + 16

d.text((X, 478), "github.com/thebdr/network-computing-hub",
       font=ImageFont.truetype(UR, 26), fill="#8b949e")

out = HERE / "social-preview.png"
img.save(out)
print("saved", out, img.size)
