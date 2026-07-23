import os
from PIL import Image, ImageDraw, ImageFont

from generator.pdf_engine import TEMPLATES

# Ensure the directory exists
out_dir = os.path.join('generator', 'static', 'img', 'templates')
os.makedirs(out_dir, exist_ok=True)

for tpl in TEMPLATES:
    img = Image.new('RGB', (300, 400), color=tpl.get('primary_color', '#1a1a2e'))
    d = ImageDraw.Draw(img)
    
    name = tpl.get('name', tpl['slug'])
    # Try to load a font, otherwise use default
    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except IOError:
        font = ImageFont.load_default()
        
    text_bbox = d.textbbox((0, 0), name, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    
    d.text(((300 - text_w) / 2, (400 - text_h) / 2), name, fill="white", font=font)
    
    filename = f"{tpl['slug']}.jpg"
    filepath = os.path.join(out_dir, filename)
    img.save(filepath)
    print(f"Generated {filepath}")
