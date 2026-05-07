"""Generate Gap & Crap app icon — dark theme with red down arrow + "GC" text.
Creates proper multi-size ICO for Windows taskbar compatibility."""
from PIL import Image, ImageDraw, ImageFont
import os

def create_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background: dark rounded square
    margin = max(1, size // 32)
    radius = max(4, size // 6)
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=radius, fill="#0D1014", outline="#FF4444", width=max(1, size // 64)
    )

    # Red down arrow
    cx = size // 2
    arrow_top = int(size * 0.18)
    arrow_bot = int(size * 0.58)
    arrow_w = int(size * 0.2)
    shaft_w = max(2, size // 20)

    # Shaft
    draw.rectangle([cx - shaft_w, arrow_top, cx + shaft_w, arrow_bot - int(size * 0.1)], fill="#FF4444")
    # Head
    draw.polygon([
        (cx - arrow_w, arrow_bot - int(size * 0.15)),
        (cx + arrow_w, arrow_bot - int(size * 0.15)),
        (cx, arrow_bot + int(size * 0.04)),
    ], fill="#FF4444")

    # "GC" text
    font_size = max(8, int(size * 0.25))
    try:
        font = ImageFont.truetype("segoeuib.ttf", font_size)
    except:
        try:
            font = ImageFont.truetype("arialbd.ttf", font_size)
        except:
            font = ImageFont.load_default()

    text_y = int(size * 0.72)
    draw.text((cx, text_y), "GC", fill="#63D3FF", font=font, anchor="mm")

    return img

icon_dir = os.path.dirname(os.path.abspath(__file__))

# Create the 256px version for preview
img_256 = create_icon(256)
img_256.save(os.path.join(icon_dir, "gc_icon.png"), "PNG")

# Create individual size images for ICO
sizes = [16, 24, 32, 48, 64, 128, 256]
images = [create_icon(s) for s in sizes]

# Save as ICO with all sizes embedded
ico_path = os.path.join(icon_dir, "gc_icon.ico")
images[0].save(
    ico_path, format="ICO",
    append_images=images[1:],
    sizes=[(s, s) for s in sizes]
)

print(f"Saved gc_icon.ico with sizes: {sizes}")
print(f"Saved gc_icon.png (256x256)")
