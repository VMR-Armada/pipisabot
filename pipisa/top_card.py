from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from pipisa.stats_card import BG, MINT, MUTED, PINK, WHITE, _font, _truncate
from pipisa.texts import format_cm

WIDTH = 1200
HEIGHT = 680
MARGIN = 48
GAP = 16
MAX_FIGURE_WIDTH = 108
TOP_LIMIT = 84
NAME_TOP = HEIGHT - 56
PLOT_ROOM = NAME_TOP - TOP_LIMIT - 20


def render_top(rows: list[tuple[str, int]], *, title: str = "ТОП 10") -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    title_font = _font(28, bold=True)
    size_font = _font(22, bold=True)
    name_font = _font(18)
    draw.text((MARGIN, 32), title, font=title_font, fill=PINK)

    count = len(rows)
    if count == 0:
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    figure_width = min(MAX_FIGURE_WIDTH, int((WIDTH - 2 * MARGIN - GAP * (count - 1)) / count))
    span = count * figure_width + (count - 1) * GAP
    left = (WIDTH - span) / 2
    sizes = [size for _name, size in rows]
    zero, scale = _scale(sizes)
    draw.line((MARGIN, zero, WIDTH - MARGIN, zero), fill=(70, 74, 98), width=2)

    for index, (name, size) in enumerate(rows):
        x = left + index * (figure_width + GAP)
        length = abs(size) * scale
        color = MINT if size > 0 else PINK if size < 0 else MUTED
        if size > 0:
            _draw_up(draw, x, x + figure_width, zero, length, color)
        elif size < 0:
            _draw_down(draw, x, x + figure_width, zero, length, color)
        _draw_size(draw, format_cm(size), size_font, x, x + figure_width, zero, size, length)
        caption = _truncate(draw, name.strip() or "Игрок", name_font, figure_width - 4)
        _draw_centered(
            draw,
            caption,
            name_font,
            x,
            NAME_TOP,
            x + figure_width,
            NAME_TOP + 36,
            WHITE,
        )

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _scale(sizes: list[int]) -> tuple[float, float]:
    upward = max((size for size in sizes if size > 0), default=0)
    downward = max((-size for size in sizes if size < 0), default=0)
    if upward == 0 and downward == 0:
        return TOP_LIMIT + PLOT_ROOM, 0
    if downward == 0:
        return TOP_LIMIT + PLOT_ROOM, PLOT_ROOM / upward
    if upward == 0:
        return float(TOP_LIMIT), PLOT_ROOM / downward
    scale = PLOT_ROOM / (upward + downward)
    return TOP_LIMIT + upward * scale, scale


def _draw_up(
    draw: ImageDraw.ImageDraw,
    left: float,
    right: float,
    zero: float,
    height: float,
    fill: tuple[int, int, int],
) -> None:
    if height <= 1:
        return
    radius = min((right - left) / 2, height / 2)
    top = zero - height
    draw.rectangle((left, top + radius - 1, right, zero), fill=fill)
    draw.pieslice((left, top, right, top + 2 * radius), start=180, end=360, fill=fill)


def _draw_down(
    draw: ImageDraw.ImageDraw,
    left: float,
    right: float,
    zero: float,
    height: float,
    fill: tuple[int, int, int],
) -> None:
    if height <= 1:
        return
    radius = min((right - left) / 2, height / 2)
    bottom = zero + height
    draw.rectangle((left, zero, right, bottom - radius + 1), fill=fill)
    draw.pieslice((left, bottom - 2 * radius, right, bottom), start=0, end=180, fill=fill)


def _draw_size(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    left: float,
    right: float,
    zero: float,
    size: int,
    length: float,
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    text_height = box[3] - box[1]
    if length >= text_height + 16:
        if size >= 0:
            _draw_centered(draw, text, font, left, zero - length, right, zero, WHITE)
        else:
            _draw_centered(draw, text, font, left, zero, right, zero + length, WHITE)
        return
    if size < 0:
        y = zero + length + 8
    else:
        y = zero - length - text_height - 10
    text_width = box[2] - box[0]
    draw.text((left + (right - left - text_width) / 2, y - box[1]), text, font=font, fill=WHITE)


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    left: float,
    top: float,
    right: float,
    bottom: float,
    fill: tuple[int, int, int],
) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    x = left + (right - left - text_width) / 2
    y = top + (bottom - top - text_height) / 2 - box[1]
    draw.text((x, y), text, font=font, fill=fill)
