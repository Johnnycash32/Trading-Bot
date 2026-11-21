import os
from PIL import Image, ImageDraw

def draw_dual_ellipse(size, bg_color, fg_color, filename, rounded_bg=False):
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    if bg_color is not None:
        if rounded_bg:
            r = int(s * 0.22)
            draw.rounded_rectangle([0, 0, s, s], radius=r, fill=bg_color)
        else:
            draw.rectangle([0, 0, s, s], fill=bg_color)
            
    pad = int(s * 0.16)
    stroke = max(4, int(s * 0.055))
    
    def make_ellipse(angle):
        layer = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        rx = (s - 2 * pad) // 2
        ry = int(rx * 0.56)
        cx, cy = s // 2, s // 2
        bbox = [cx - rx, cy - ry, cx + rx, cy + ry]
        d.ellipse(bbox, outline=fg_color, width=stroke)
        return layer.rotate(angle, resample=Image.BICUBIC)
        
    e1 = make_ellipse(45)
    e2 = make_ellipse(-45)
    img = Image.alpha_composite(img, e1)
    img = Image.alpha_composite(img, e2)
    
    draw = ImageDraw.Draw(img)
    cx, cy = s // 2, s // 2
    dr = int(s * 0.115)
    diamond = [(cx, cy - dr), (cx + dr, cy), (cx, cy + dr), (cx - dr, cy)]
    draw.polygon(diamond, fill=fg_color)
    
    final = img.resize((size, size), resample=Image.LANCZOS)
    final.save(filename, "PNG")

os.makedirs("ui/assets", exist_ok=True)
draw_dual_ellipse(512, "#FFFFFF", "#000000", "ui/assets/icon_black_on_white.png")
draw_dual_ellipse(512, "#0D0E12", "#FFFFFF", "ui/assets/icon_white_on_black.png")
draw_dual_ellipse(512, "#FFFFFF", "#000000", "ui/assets/app_icon_light.png", rounded_bg=True)
draw_dual_ellipse(512, "#0D0E12", "#FFFFFF", "ui/assets/app_icon_dark.png", rounded_bg=True)
draw_dual_ellipse(512, None, "#FFFFFF", "ui/assets/icon_transparent_white.png")
draw_dual_ellipse(512, None, "#000000", "ui/assets/icon_transparent_black.png")

print("Icons generated successfully.")
