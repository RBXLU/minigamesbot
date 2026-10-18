"""Карточка «Мой год в MiniGames» — картинка, которую игрок пересылает друзьям.

Pillow импортируется внутри функции: картинка нужна раз в год на игрока,
держать библиотеку в памяти бота с самого старта незачем.
"""
import io
import random
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
WIDTH, HEIGHT = 1080, 1350

TOP = (24, 16, 64)
BOTTOM = (92, 40, 140)
ACCENT = (255, 196, 92)
TILE = (255, 255, 255, 34)
TEXT = (255, 255, 255)
MUTED = (214, 204, 240)
CONFETTI = ((255, 196, 92), (255, 120, 160), (120, 220, 255), (160, 255, 170), (255, 255, 255))


def _font(size, bold=False):
    from PIL import ImageFont

    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONT_DIR / name), size)


def _fit(draw, text, size, max_width, bold=False):
    """Уменьшает шрифт, пока строка не влезет в max_width."""
    while size > 18:
        font = _font(size, bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return _font(size, bold)


def render_year_card(stats, seed=0):
    """Возвращает PNG в байтах.

    stats: name, games, wins, winrate, favorite, favorite_count, best_streak,
    achievements, achievements_total, rank, players, footer.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (WIDTH, HEIGHT), TOP)
    draw = ImageDraw.Draw(image)
    for y in range(HEIGHT):
        k = y / (HEIGHT - 1)
        draw.line(
            [(0, y), (WIDTH, y)],
            fill=tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * k) for i in range(3)),
        )

    # Конфетти своё у каждого игрока, но одинаковое при повторной генерации,
    # и только вне текста и плиток — иначе точки съедают буквы.
    keep_out = (
        (50, 60, 820, 250),
        (50, 255, WIDTH - 50, 335),
        (50, 360, WIDTH - 50, 1150),
        (50, HEIGHT - 125, WIDTH - 50, HEIGHT - 55),
    )
    rng = random.Random(seed)
    placed = 0
    while placed < 70:
        x, y = rng.randint(0, WIDTH), rng.randint(0, HEIGHT)
        r = rng.randint(3, 9)
        if any(x0 - r <= x <= x1 + r and y0 - r <= y <= y1 + r for x0, y0, x1, y1 in keep_out):
            continue
        draw.ellipse([x - r, y - r, x + r, y + r], fill=rng.choice(CONFETTI))
        placed += 1

    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    tiles = ImageDraw.Draw(overlay)

    margin = 70
    draw.text((margin, 80), "МОЙ ГОД В", font=_font(44, True), fill=MUTED)
    draw.text((margin, 132), "MiniGames", font=_font(110, True), fill=ACCENT)
    name = str(stats.get("name") or "Игрок")
    draw.text((margin, 270), name, font=_fit(draw, name, 56, WIDTH - 2 * margin, True), fill=TEXT)

    cells = [
        (f"{stats.get('games', 0)}", "партий сыграно"),
        (f"{stats.get('wins', 0)}", f"побед · {stats.get('winrate', 0):.0f}%"),
        (str(stats.get("favorite") or "—"), f"любимая игра · {stats.get('favorite_count', 0)}"),
        (f"{stats.get('best_streak', 0)} дн.", "лучшая серия"),
        (f"{stats.get('achievements', 0)}/{stats.get('achievements_total', 0)}", "достижений"),
        (f"#{stats.get('rank', '—')}", f"из {stats.get('players', 0)} игроков"),
    ]
    gap = 30
    tile_w = (WIDTH - 2 * margin - gap) // 2
    tile_h = 230
    top = 380
    for index, (value, label) in enumerate(cells):
        col, row = index % 2, index // 2
        x = margin + col * (tile_w + gap)
        y = top + row * (tile_h + gap)
        tiles.rounded_rectangle([x, y, x + tile_w, y + tile_h], radius=36, fill=TILE)
    image.paste(overlay, (0, 0), overlay)
    draw = ImageDraw.Draw(image)
    for index, (value, label) in enumerate(cells):
        col, row = index % 2, index // 2
        x = margin + col * (tile_w + gap) + 36
        y = top + row * (tile_h + gap)
        draw.text((x, y + 40), value, font=_fit(draw, value, 76, tile_w - 72, True), fill=TEXT)
        draw.text((x, y + 150), label, font=_fit(draw, label, 32, tile_w - 72), fill=MUTED)

    footer = str(stats.get("footer") or "")
    draw.text((margin, HEIGHT - 110), footer, font=_fit(draw, footer, 34, WIDTH - 2 * margin), fill=MUTED)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()
