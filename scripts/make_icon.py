"""Draws the app icon (a page with text lines and a scan beam) → AppIcon.icns."""
import os
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFilter

out_dir = sys.argv[1]
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# squircle background with a vertical gradient
bg = Image.new("RGBA", (S, S))
px = bg.load()
for y in range(S):
    t = y / S
    c = (int(40 + 30 * t), int(86 + 50 * t), int(235 - 25 * t), 255)
    for x in range(S):
        px[x, y] = c
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=210, fill=255)
img.paste(bg, (0, 0), mask)

# page
shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shadow).rounded_rectangle((250, 215, 770, 825), radius=36, fill=(0, 0, 40, 110))
img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(24)))
d = ImageDraw.Draw(img)
d.rounded_rectangle((240, 190, 760, 800), radius=36, fill=(255, 255, 255, 255))
d.polygon([(640, 190), (760, 190), (760, 310)], fill=(222, 230, 250, 255))
d.rounded_rectangle((300, 260, 520, 300), radius=18, fill=(40, 70, 190, 255))      # heading
for i, w in enumerate((400, 380, 400, 330, 390, 250)):
    y = 350 + i * 52
    d.rounded_rectangle((300, y, 300 + w, y + 22), radius=11, fill=(150, 165, 200, 255))
d.rounded_rectangle((300, 680, 470, 750), radius=12, fill=(190, 210, 250, 255))     # figure
d.polygon([(318, 735), (365, 695), (395, 722), (420, 702), (452, 735)], fill=(90, 130, 230, 255))

# scan beam
beam = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(beam).rounded_rectangle((200, 500, 820, 530), radius=15, fill=(120, 255, 200, 235))
img = Image.alpha_composite(img, beam.filter(ImageFilter.GaussianBlur(10)))
ImageDraw.Draw(img).rounded_rectangle((210, 508, 810, 522), radius=7, fill=(190, 255, 225, 255))

iconset = os.path.join(out_dir, "AppIcon.iconset")
os.makedirs(iconset, exist_ok=True)
for size in (16, 32, 128, 256, 512):
    img.resize((size, size), Image.LANCZOS).save(os.path.join(iconset, f"icon_{size}x{size}.png"))
    img.resize((size * 2, size * 2), Image.LANCZOS).save(os.path.join(iconset, f"icon_{size}x{size}@2x.png"))
subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(out_dir, "AppIcon.icns")], check=True)
print("icon ok")
