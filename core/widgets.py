"""Виджеты Bot API 10.4 — маленькие HTML-игры прямо в пузыре сообщения.

Шаблоны лежат в widgets/*.html. Подписи в шаблон подставляются при отправке
уже на языке игрока (на месте __T__). Сколько можно отправить, проверяет
и сам Telegram, но так об ошибке мы узнаём до запроса, а не от сервера.
"""
import json
import zlib
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "widgets"
MAX_HTML = 32768       # байт UTF-8
MAX_PACKED = 4096      # байт после zlib на уровне 9 — так считает сервер


def packed_size(html):
    return len(zlib.compress(html.encode("utf-8"), 9))


def build(name, texts):
    """HTML виджета name с подписями texts; ValueError, если он не пройдёт лимиты Telegram."""
    # «</» внутри строки закрыл бы <script>
    payload = json.dumps(texts, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = (DIR / f"{name}.html").read_text(encoding="utf-8").replace("__T__", payload)
    if "\0" in html or len(html.encode("utf-8")) > MAX_HTML or packed_size(html) > MAX_PACKED:
        raise ValueError(f"widget {name} is over Telegram limits")
    return html
