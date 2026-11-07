"""Draws the WiFiDrop icon (PNG for Linux, ICO for Windows). Run: python assets/make_icon.py"""
import os

from PIL import Image, ImageDraw

S = 1024
here = os.path.dirname(os.path.abspath(__file__))
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# rounded square with a vertical blue gradient
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
top, bottom = (91, 140, 255), (40, 90, 230)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([40, 40, S - 40, S - 40], radius=230, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
white = (255, 255, 255, 255)
cx, cy = S // 2, 720
# wifi arcs fanning out from a drop at the bottom
for r in (420, 290, 160):
    d.arc([cx - r, cy - r, cx + r, cy + r], start=225, end=315, fill=white, width=72)
# the "drop": a teardrop point at the wifi origin
d.ellipse([cx - 62, cy - 62, cx + 62, cy + 62], fill=white)
d.polygon([(cx - 56, cy - 26), (cx + 56, cy - 26), (cx, cy - 120)], fill=white)

img.save(os.path.join(here, "icon.png"))
img.save(os.path.join(here, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icons written")
