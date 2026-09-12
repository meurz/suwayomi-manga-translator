"""Restore text when upstream typesetting leaves an erased edge region blank."""

from PIL import Image, ImageDraw, ImageFont


def ensure_rendered(base: Image.Image, result: Image.Image, regions, font_path: str):
    result = result.copy()
    draw = ImageDraw.Draw(result)
    fallbacks = 0
    for bounds, text in regions:
        x1, y1, x2, y2 = (int(v) for v in bounds)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(result.width, x2), min(result.height, y2)
        if x2 - x1 < 8 or y2 - y1 < 8 or not text.strip():
            continue
        box = (x1, y1, x2, y2)
        if base.crop(box).tobytes() != result.crop(box).tobytes():
            continue
        width, height = x2 - x1 - 4, y2 - y1 - 4
        chosen = None
        for size in range(min(64, height), 5, -1):
            font = ImageFont.truetype(font_path, size)
            lines, current = [], ""
            for char in text:
                if char == "\n":
                    lines.append(current)
                    current = ""
                elif current and draw.textlength(current + char, font=font) > width:
                    lines.append(current)
                    current = char
                else:
                    current += char
            lines.append(current)
            block = "\n".join(lines)
            bbox = draw.multiline_textbbox((0, 0), block, font=font, spacing=2)
            if bbox[2] - bbox[0] <= width and bbox[3] - bbox[1] <= height:
                chosen = (font, block, bbox)
                break
        if chosen:
            font, block, bbox = chosen
            pos = (
                x1 + (x2 - x1 - (bbox[2] - bbox[0])) / 2 - bbox[0],
                y1 + (y2 - y1 - (bbox[3] - bbox[1])) / 2 - bbox[1],
            )
            draw.multiline_text(
                pos,
                block,
                font=font,
                fill="black",
                stroke_width=1,
                stroke_fill="white",
                spacing=2,
                align="center",
            )
            fallbacks += 1
    return result, fallbacks
