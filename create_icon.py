"""Generate a dark-themed icon for the Ask Edgar Dilution Monitor V2."""
from PIL import Image, ImageDraw, ImageFont
import os

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Background: rounded dark rectangle matching app's BG (#0D1014)
bg_color = (13, 16, 20, 255)
border_color = (99, 211, 255, 255)  # ACCENT cyan
draw.rounded_rectangle([(8, 8), (248, 248)], radius=40, fill=bg_color, outline=border_color, width=6)

# Shield shape in the center
shield_color = (35, 42, 51, 255)
shield_pts = [
    (128, 25),   # top center
    (215, 58),   # top right
    (205, 175),  # mid right
    (128, 235),  # bottom center (point)
    (51, 175),   # mid left
    (41, 58),    # top left
]
draw.polygon(shield_pts, fill=shield_color, outline=border_color, width=3)

# Inner shield
inner_pts = [
    (128, 45),
    (195, 72),
    (187, 165),
    (128, 215),
    (69, 165),
    (61, 72),
]
draw.polygon(inner_pts, fill=(21, 26, 32, 255))  # BG_CARD

# Upward arrow / gain chart inside shield
# Rising bars (like a bar chart going up)
bar_colors = [
    (169, 50, 50, 255),    # red (short)
    (185, 106, 22, 255),   # orange (medium)
    (47, 155, 87, 255),    # green (tall)
    (99, 211, 255, 255),   # cyan (tallest)
]
bar_width = 20
bar_gap = 6
bar_heights = [30, 50, 70, 90]
total_width = len(bar_colors) * bar_width + (len(bar_colors) - 1) * bar_gap
start_x = 128 - total_width // 2
base_y = 165

for i, (color, h) in enumerate(zip(bar_colors, bar_heights)):
    x = start_x + i * (bar_width + bar_gap)
    draw.rounded_rectangle([(x, base_y - h), (x + bar_width, base_y)], radius=3, fill=color)

# Upward arrow above the tallest bar
arrow_cx = start_x + 3 * (bar_width + bar_gap) + bar_width // 2
arrow_top = base_y - 105
arrow_pts = [
    (arrow_cx, arrow_top),
    (arrow_cx + 14, arrow_top + 18),
    (arrow_cx - 14, arrow_top + 18),
]
draw.polygon(arrow_pts, fill=(99, 211, 255, 255))

# "V2" text at bottom of shield
try:
    font = ImageFont.truetype("segoeuib.ttf", 36)
except Exception:
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 36)
    except Exception:
        font = ImageFont.load_default()

draw.text((128, 195), "V2", fill=border_color, font=font, anchor="mm")

# Save as ICO with multiple sizes
icon_path = os.path.join(os.path.dirname(__file__), "app_icon.ico")
sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
icons = [img.resize(s, Image.LANCZOS) for s in sizes]
icons[-1].save(icon_path, format="ICO", sizes=sizes, append_images=icons[:-1])
print(f"Icon saved: {icon_path}")

# Also save PNG for preview
png_path = os.path.join(os.path.dirname(__file__), "app_icon.png")
img.save(png_path)
print(f"PNG preview saved: {png_path}")
