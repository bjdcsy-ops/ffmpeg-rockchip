"""Render fixed white text watermarks; global opacity belongs to FFmpeg."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

WIDTH, HEIGHT = 1920, 1080
MAX_TEXT_PIXELS = 16 * 1024 * 1024


def _positions(start, end, phase, extent, period):
    first = phase + ((start - phase - extent) // period + 1) * period
    return range(first, end, period)


def _matrix_mask(Image, tile, box, x, y, period_x, period_y):
    left, top, right, bottom = box
    mask = Image.new("L", (right - left, bottom - top), 0)
    columns = _positions(left, right, x, tile.width, period_x)
    rows = _positions(top, bottom, y, tile.height, period_y)
    bands = {}
    for row in rows:
        source_top = max(0, top - row)
        source_bottom = min(tile.height, bottom - row)
        key = (source_top, source_bottom)
        if key not in bands:
            # Reuse horizontal bands instead of enumerating millions of tiny tiles.
            band = Image.new("L", (mask.width, source_bottom - source_top), 0)
            fragment = tile.crop((0, source_top, tile.width, source_bottom))
            for column in columns:
                band.paste(fragment, (column - left, 0))
            bands[key] = band
        mask.paste(bands[key], (0, row + source_top - top))
    return mask, len(columns) * len(rows)


def render_watermark(
    output: Path,
    *,
    mode: str,
    text: str,
    font_path: str,
    font_size: int = 44,
    angle: float = 30,
    gap_x: int = 56,
    gap_y: int = 148,
    x: int = 32,
    y: int = 32,
) -> dict:
    """Save a 1920x1080 RGBA PNG; x/y are ink bounds or matrix phase.

    Positive diagonal angles rotate the whole matrix counterclockwise around
    the frame center. Matrix phases extend in both directions before cropping.
    The destination directory must already exist.
    """
    if mode not in ("single", "matrix", "diagonal"):
        raise ValueError("mode must be single, matrix or diagonal")
    if (not isinstance(text, str) or not text.strip() or len(text) > 256
            or text.splitlines() != [text] or "\x00" in text):
        raise ValueError("text must be one nonempty line of at most 256 characters")
    for name, value, low, high in (
        ("font_size", font_size, 8, 256),
        ("gap_x", gap_x, 0, WIDTH), ("gap_y", gap_y, 0, HEIGHT),
        ("x", x, 0, WIDTH - 1), ("y", y, 0, HEIGHT - 1),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("{} must be an integer in {}..{}".format(name, low, high))
    if (isinstance(angle, bool) or not isinstance(angle, (int, float))
            or not -90 <= angle <= 90 or not math.isfinite(angle)):
        raise ValueError("angle must be finite and in -90..90 degrees")
    try:
        from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version
    except ImportError as error:
        raise RuntimeError("PNG rendering requires Pillow >= 9.1; install 'Pillow>=9.1,<13'") from error
    if not hasattr(Image, "Resampling") or not hasattr(Image, "Transform"):
        raise RuntimeError("PNG rendering requires Pillow >= 9.1")
    try:
        font = ImageFont.truetype(font_path, font_size)
    except (OSError, TypeError, ValueError) as error:
        raise ValueError("Cannot load watermark font: {}".format(font_path)) from error
    left, top, right, bottom = font.getbbox(text)
    if right <= left or bottom <= top:
        raise ValueError("text has no visible glyphs")
    if (right - left) * (bottom - top) > MAX_TEXT_PIXELS:
        raise ValueError("rendered text exceeds the 16 Mi-pixel mask limit")
    tile = Image.new("L", (right - left, bottom - top), 0)
    ImageDraw.Draw(tile).text((-left, -top), text, font=font, fill=255)
    ink = tile.getbbox()
    if ink is None:
        raise ValueError("text has no visible glyphs")
    tile = tile.crop(ink)
    period_x, period_y = tile.width + gap_x, tile.height + gap_y
    effective_angle = float(angle) if mode == "diagonal" else 0.0
    cosine = round(math.cos(math.radians(effective_angle)), 15)
    sine = round(math.sin(math.radians(effective_angle)), 15)

    if mode == "single":
        if x + tile.width > WIDTH or y + tile.height > HEIGHT:
            raise ValueError("single watermark must fit completely inside 1920x1080")
        mask = Image.new("L", (WIDTH, HEIGHT), 0)
        mask.paste(tile, (x, y))
        box, tiles = (0, 0, WIDTH, HEIGHT), 1
    else:
        box = (0, 0, WIDTH, HEIGHT)
        if effective_angle:
            span_x = abs(cosine) * WIDTH + abs(sine) * HEIGHT
            span_y = abs(sine) * WIDTH + abs(cosine) * HEIGHT
            # Two source pixels cover the bicubic sampling neighborhood.
            box = (math.floor((WIDTH - span_x) / 2) - 2,
                   math.floor((HEIGHT - span_y) / 2) - 2,
                   math.ceil((WIDTH + span_x) / 2) + 2,
                   math.ceil((HEIGHT + span_y) / 2) + 2)
        mask, tiles = _matrix_mask(Image, tile, box, x, y, period_x, period_y)
        if effective_angle:
            # Pillow affine coefficients map destination pixels back to the source.
            matrix = (cosine, -sine, WIDTH / 2 - cosine * WIDTH / 2 + sine * HEIGHT / 2 - box[0],
                      sine, cosine, HEIGHT / 2 - sine * WIDTH / 2 - cosine * HEIGHT / 2 - box[1])
            mask = mask.transform((WIDTH, HEIGHT), Image.Transform.AFFINE, matrix,
                                  resample=Image.Resampling.BICUBIC, fillcolor=0)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (255, 255, 255, 0))
    image.putalpha(mask)
    image.save(output, format="PNG")
    return {
        "output": str(output), "width": WIDTH, "height": HEIGHT,
        "pillow_version": pillow_version,
        "sha256": hashlib.sha256(Path(output).read_bytes()).hexdigest(),
        "mode": mode, "text": text, "font_path": str(font_path),
        "font_size": font_size, "angle": effective_angle,
        "gap_x": gap_x, "gap_y": gap_y, "x": x, "y": y,
        "text_width": tile.width, "text_height": tile.height,
        "period_x": period_x, "period_y": period_y, "tiles": tiles,
        "work_width": box[2] - box[0], "work_height": box[3] - box[1],
    }
