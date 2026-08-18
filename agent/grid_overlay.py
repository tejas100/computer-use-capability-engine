"""
Grid overlay: draws faint coordinate gridlines + axis labels onto a
screenshot before it's sent to the LLM.

Why: vision-language models are noticeably worse at precise pixel
*localization* than at object *identification* -- they can usually
say "the input field is near the top" correctly, but translating
that into an exact (x, y) is where error creeps in, especially on
sparse pages with few visual anchors to count against. A labeled
grid gives the model something concrete to reference ("the field
sits between the 300 and 400 vertical lines") instead of estimating
in a vacuum. This is a standard mitigation for coordinate-grounding
tasks, not a project-specific hack.

The grid is overlaid only on the *copy* sent to the LLM -- it is
never drawn on the frames actually used for evidence screenshots or
locator capture, so it can't interfere with anything else.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

GRID_SPACING = 50       # a gridline every 50px, for fine-grained targeting
LABEL_SPACING = 100      # a number label only every 100px, to avoid clutter
MAJOR_COLOR = (255, 0, 0, 110)
MINOR_COLOR = (255, 0, 0, 55)
LABEL_COLOR = (255, 0, 0, 220)


def add_grid_overlay(png_bytes: bytes) -> bytes:
    base = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    width, height = base.size

    for x in range(0, width, GRID_SPACING):
        is_major = x % LABEL_SPACING == 0
        draw.line([(x, 0), (x, height)], fill=MAJOR_COLOR if is_major else MINOR_COLOR, width=1)
        if is_major:
            draw.text((x + 2, 2), str(x), fill=LABEL_COLOR)

    for y in range(0, height, GRID_SPACING):
        is_major = y % LABEL_SPACING == 0
        draw.line([(0, y), (width, y)], fill=MAJOR_COLOR if is_major else MINOR_COLOR, width=1)
        if is_major:
            draw.text((2, y + 2), str(y), fill=LABEL_COLOR)

    combined = Image.alpha_composite(base, overlay).convert("RGB")
    out = io.BytesIO()
    combined.save(out, format="PNG")
    return out.getvalue()