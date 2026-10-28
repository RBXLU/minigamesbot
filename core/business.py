"""Логика режима Telegram Business, которая не знает про Telegram.

Бот подключается к личному аккаунту владельца и видит его переписки: сообщает
об удалённых и изменённых сообщениях собеседника, глушит собеседника по
команде .mute и проводит игры прямо в чате. Здесь — разбор команд, правила
игр, память о сообщениях и учёт времени подключения; обработчики — в bot.py.
"""
import random
import re
import time
from collections import OrderedDict

# ------------------------------------ .mute ------------------------------------

MUTE_UNITS = {"s": 1, "с": 1, "m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400}
MUTE_MAX = 365 * 86400
_MUTE_RE = re.compile(r"^(\d{1,6})\s*([a-zа-я])$", re.IGNORECASE)


def parse_mute(text):
    """'.mute 10m' → 600 секунд; '.mute' без срока → 0 (до .unmute).

    None — это не команда .mute; ValueError — команда, но срок записан непонятно.
    """
    parts = (text or "").strip().split(maxsplit=1)
    if not parts or parts[0].lower() not in (".mute", ".мут"):
        return None
    if len(parts) == 1:
        return 0
    match = _MUTE_RE.match(parts[1].strip())
    if not match or match.group(2).lower() not in MUTE_UNITS:
        raise ValueError("Срок пишется так: 30s, 10m, 2h или 3d")
    seconds = int(match.group(1)) * MUTE_UNITS[match.group(2).lower()]
    if not 0 < seconds <= MUTE_MAX:
        raise ValueError("Срок — от секунды до года")
    return seconds


def human_duration(seconds):
    """3725 → '1 ч 2 мин'; для коротких сроков — секунды."""
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    if secs and not days and not hours:
        parts.append(f"{secs} с")
    return " ".join(parts[:2]) or "0 с"


def mute_active(until, now=None):
    """until == 0 — бессрочно; иначе отметка времени окончания."""
    if until is None:
        return False
    return until == 0 or (now or time.time()) < until


# ------------------------------ память о сообщениях ------------------------------

# Что положить в память о сообщении: тип, текст или подпись, file_id вложения
MEDIA_KINDS = ("photo", "video", "animation", "voice", "video_note", "audio", "document", "sticker")
MEDIA_LABELS = {
    "photo": "фото", "video": "видео", "animation": "гифка", "voice": "голосовое",
    "video_note": "кружок", "audio": "аудио", "document": "файл", "sticker": "стикер",
}


def snapshot(message):
    """Короткая запись о сообщении: кто прислал, текст и вложение."""
    kind, file_id = "text", None
    for media in MEDIA_KINDS:
        value = getattr(message, media, None)
        if value:
            kind = media
            item = value[-1] if isinstance(value, list) else value   # у фото — самый крупный размер
            file_id = getattr(item, "file_id", None)
            break
    user = message.from_user
    return {
        "from": user.id if user else None,
        "name": (user.first_name or user.username or "Собеседник") if user else "Собеседник",
        "kind": kind,
        "text": message.text or message.caption or "",
        "file_id": file_id,
        "at": time.time(),
    }


def describe(record):
    """Как показать сообщение в уведомлении: «текст» или «[фото] подпись»."""
    text = record.get("text") or ""
    if record.get("kind", "text") == "text":
        return text or "(пусто)"
    label = MEDIA_LABELS.get(record["kind"], record["kind"])
    return f"[{label}] {text}".strip()


class MessageCache:
    """Помнит последние сообщения бизнес-чатов: без этого об удалении известны
    только номера сообщений. Свежие — в памяти, а копия — в store (база), чтобы
    перезапуск бота не стирал память. Всё старше ttl выбрасывается."""

    PURGE_EVERY = 500

    def __init__(self, limit=20000, ttl=3 * 86400, store=None):
        self._items = OrderedDict()
        self._limit = limit
        self._ttl = ttl
        self._store = store
        self._puts = 0

    @staticmethod
    def key(connection_id, chat_id, message_id):
        return f"{connection_id}:{chat_id}:{message_id}"

    def _fresh(self, record):
        return bool(record) and time.time() - record.get("at", 0) <= self._ttl

    def put(self, connection_id, chat_id, message_id, record):
        key = self.key(connection_id, chat_id, message_id)
        self._items[key] = record
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        if self._store is not None:
            self._store.put(key, record)
            self._puts += 1
            if self._puts % self.PURGE_EVERY == 0:
                self._store.purge(time.time() - self._ttl)

    def get(self, connection_id, chat_id, message_id):
        key = self.key(connection_id, chat_id, message_id)
        record = self._items.get(key)
        if record is None and self._store is not None:
            record = self._store.get(key)
        if not self._fresh(record):
            self._items.pop(key, None)
            return None
        return record

    def pop(self, connection_id, chat_id, message_id):
        record = self.get(connection_id, chat_id, message_id)
        key = self.key(connection_id, chat_id, message_id)
        self._items.pop(key, None)
        if self._store is not None:
            self._store.delete(key)
        return record

    def __len__(self):
        return len(self._items)


# ------------------------------------- игры -------------------------------------

RPS_MOVES = {"rock": "🪨 Камень", "scissors": "✂️ Ножницы", "paper": "📄 Бумага"}
_RPS_BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


def rps_winner(move_a, move_b):
    """0 — ничья, 1 — победил первый, 2 — второй."""
    if move_a == move_b:
        return 0
    return 1 if _RPS_BEATS[move_a] == move_b else 2


TTT_LINES = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6))


def ttt_winner(board):
    """'X' или 'O' — победитель, 'draw' — ничья, None — игра идёт."""
    for a, b, c in TTT_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return "draw" if all(board) else None


def ttt_line(board):
    """Клетки выигрышной линии — чтобы подсветить их."""
    for line in TTT_LINES:
        a, b, c = line
        if board[a] and board[a] == board[b] == board[c]:
            return line
    return ()


def new_game(kind, owner, friend, rng=random):
    """Партия в чате владельца с собеседником; ходит первым владелец."""
    players = [owner, friend]
    if kind == "ttt":
        rng.shuffle(players)                  # кто играет крестиками — жребий
        return {"kind": "ttt", "players": players, "board": [""] * 9, "turn": 0, "result": None}
    return {"kind": kind, "players": players, "moves": {}, "result": None}


# ----------------------------------- телеметрия -----------------------------------

# У каждого, кто подключал бота, есть запись: без неё не посчитать, сколько
# всего людей подключили. Время подключения в ней появляется только с согласия:
# consent None — ещё не ответил, False — отказался, True — согласился.

def telemetry_on_connect(user, now=None):
    if user.get("consent"):
        user["since"] = now or time.time()
    return user


def telemetry_on_disconnect(user, now=None):
    since = user.pop("since", None)
    if user.get("consent") and since:
        user["seconds"] = int(user.get("seconds", 0)) + int((now or time.time()) - since)
    return user


def telemetry_set_consent(user, consent, connected, now=None):
    """Согласие можно дать и забрать в любой момент. Отказ стирает накопленное время."""
    if consent:
        user["consent"] = True
        if connected and not user.get("since"):
            user["since"] = now or time.time()
    else:
        user["consent"] = False
        user.pop("since", None)
        user.pop("seconds", None)
    return user


def telemetry_seconds(user, now=None):
    """Сколько бот подключён к аккаунту (только у согласившихся)."""
    if not user.get("consent"):
        return 0
    total = int(user.get("seconds", 0))
    if user.get("since"):
        total += int((now or time.time()) - user["since"])
    return total


def telemetry_summary(users, now=None):
    """Сводка для админа: всего подключали, подключено сейчас, согласились, время."""
    users = [u for u in (users or {}).values() if isinstance(u, dict)]
    consented = [u for u in users if u.get("consent")]
    seconds = [telemetry_seconds(u, now) for u in consented]
    return {
        "total": len(users),
        "connected": sum(1 for u in consented if u.get("since")),
        "consented": len(consented),
        "declined": sum(1 for u in users if u.get("consent") is False),
        "seconds_total": sum(seconds),
        "seconds_avg": int(sum(seconds) / len(seconds)) if seconds else 0,
    }


# ------------------------------- анимации сообщений -------------------------------

# Владелец начинает сообщение со спецсимвола — бот заменяет его анимированной копией.
# Кадров не больше 10: чаще правки Telegram начинает отклонять.
ANIMATIONS = {
    "~": ("words", "по словам"),
    "^": ("letters", "по буквам"),
    "%": ("decode", "расшифровка"),
}
ANIMATION_MAX_FRAMES = 10
ANIMATION_MAX_LEN = {"words": 1000, "letters": 300, "decode": 300}
_NOISE = "█▓▒░#@$&*?"


def parse_animation(text):
    """'~Привет всем' → ('words', 'Привет всем'); не анимация → None.

    Сразу за символом должен идти текст: «~ ок» или одиночное «~» — обычное сообщение.
    """
    text = text or ""
    if len(text) < 3 or text[0] not in ANIMATIONS or text[1].isspace():
        return None
    body = text[1:].strip()
    if len(body) < 2:
        return None
    kind = ANIMATIONS[text[0]][0]
    return kind, body[:ANIMATION_MAX_LEN[kind]]


def animation_frames(kind, body, rng=random):
    """Кадры анимации; последний — сам текст."""
    if kind == "words":
        words = body.split(" ")
        steps = min(ANIMATION_MAX_FRAMES, len(words))
        frames = [" ".join(words[:-(-len(words) * i // steps)]) for i in range(1, steps)]
    elif kind == "letters":
        steps = max(1, min(ANIMATION_MAX_FRAMES, len(body)))
        size = -(-len(body) // steps)
        frames = [body[:size * i] + "▌" for i in range(1, steps)]
    else:
        # Буквы проявляются в случайном порядке, остальное — «шум»
        hidden = [i for i, ch in enumerate(body) if not ch.isspace()]
        rng.shuffle(hidden)
        steps = ANIMATION_MAX_FRAMES
        frames = []
        for i in range(steps - 1):
            still = set(hidden[-(-len(hidden) * i // steps):]) if i else set(hidden)
            frames.append("".join(rng.choice(_NOISE) if j in still else ch for j, ch in enumerate(body)))
    frames = [f for f in frames if f.strip()]
    # Соседние одинаковые кадры Telegram не примет («message is not modified»)
    out = []
    for frame in frames + [body]:
        if not out or out[-1] != frame:
            out.append(frame)
    return out
