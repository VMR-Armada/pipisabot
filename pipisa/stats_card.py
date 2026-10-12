from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from pipisa.texts import format_cm, format_signed_cm, format_wait

BG = (14, 16, 28)
CARD = (28, 31, 48)
PINK = (255, 92, 138)
MINT = (72, 214, 180)
WHITE = (244, 244, 248)
MUTED = (168, 172, 196)
PILL = (40, 44, 66)
ZERO = (120, 124, 150)


@dataclass
class CardData:
    name: str
    chat_title: str | None
    size: int
    rank: int
    total: int
    plays: int
    best_gain: int
    worst_loss: int
    next_free_at: int | None
    now: int
    deltas: list[int]


def render_stats(data: CardData) -> bytes:
    image = Image.new("RGB", (1000, 700), BG)
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    decor = ImageDraw.Draw(overlay)
    decor.ellipse((760, -140, 1160, 260), fill=(255, 92, 138, 46))
    decor.ellipse((-120, 420, 260, 800), fill=(72, 214, 180, 36))
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    title_font = _font(22, bold=True)
    name_font = _font(40, bold=True)
    size_font = _font(84, bold=True)
    text_font = _font(24)
    label_font = _font(16)
    value_font = _font(28, bold=True)

    draw.text((48, 36), "ЛИНЕЙКА", font=title_font, fill=PINK)
    name = _truncate(draw, data.name, name_font, 900)
    draw.text((48, 78), name, font=name_font, fill=WHITE)

    size_color = MINT if data.size > 0 else PINK if data.size < 0 else WHITE
    draw.text((48, 140), format_cm(data.size), font=size_font, fill=size_color)

    place = f"{data.rank} место из {data.total}"
    if data.chat_title:
        place += "  ·  " + data.chat_title
    draw.text((48, 250), _truncate(draw, place, text_font, 900), font=text_font, fill=MUTED)

    if data.next_free_at is None or data.now >= data.next_free_at:
        free_text = "Можно сделать замер"
    else:
        free_text = "Следующий замер через " + format_wait(data.next_free_at - data.now)
    draw.text((48, 286), free_text, font=text_font, fill=WHITE)

    pills = [
        ("Замеров", str(data.plays), WHITE),
        ("Лучший рост", format_signed_cm(data.best_gain) if data.best_gain else "—", MINT),
        ("Худший", format_signed_cm(data.worst_loss) if data.worst_loss else "—", PINK),
    ]
    gap = 16
    pill_w = (1000 - 96 - gap * 2) // 3
    pill_h = 92
    top = 344
    for index, (label, value, color) in enumerate(pills):
        left = 48 + index * (pill_w + gap)
        draw.rounded_rectangle((left, top, left + pill_w, top + pill_h), radius=18, fill=PILL)
        draw.text((left + 16, top + 14), label, font=label_font, fill=MUTED)
        draw.text((left + 16, top + 42), value, font=value_font, fill=color)

    draw.text((48, 468), "Последние замеры", font=text_font, fill=MUTED)
    _draw_bars(draw, data.deltas, text_font)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _draw_bars(draw: ImageDraw.ImageDraw, deltas: list[int], font: ImageFont.ImageFont) -> None:
    left, right = 48, 952
    base = 600
    height = 64
    if not deltas:
        draw.text((left, 520), "История появится после первого замера", font=font, fill=MUTED)
        return
    draw.line((left, base, right, base), fill=(70, 74, 98), width=2)
    gap = 10
    count = len(deltas)
    bar_w = min(40, int((right - left - gap * (count - 1)) / count))
    span = count * bar_w + (count - 1) * gap
    x = left + (right - left - span) / 2
    scale = height / 10
    for delta in deltas:
        color = MINT if delta > 0 else PINK if delta < 0 else ZERO
        bar_h = max(4, abs(delta) * scale) if delta else 4
        if delta >= 0:
            box = (x, base - bar_h, x + bar_w, base)
        else:
            box = (x, base, x + bar_w, base + bar_h)
        radius = min(4, bar_w / 2, bar_h / 2)
        draw.rounded_rectangle(box, radius=radius, fill=color)
        x += bar_w + gap


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _font_path(bold)
    if path is not None:
        return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def _font_path(bold: bool) -> Path | None:
    regular = (
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
    )
    heavy = (
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    )
    for path in heavy if bold else regular:
        if path.exists():
            return path
    return None


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    trimmed = text
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return trimmed + "…"
