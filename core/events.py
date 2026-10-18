"""Сезонные события: окна дат и правила начисления.

Модуль ничего не знает про Telegram и базу — только считает, поэтому его
можно проверить без бота. Даты читаются из окружения при каждом вызове:
.env загружается позже импорта, а менять окно ивента без правки кода удобно.
"""
import os
import random
from datetime import date

HALLOWEEN = "halloween"
ANNIVERSARY = "anniversary"
SEASON = "season"

_DEFAULT_WINDOWS = {
    # Последний день включительно.
    HALLOWEEN: ("2026-10-25", "2026-11-02"),
    ANNIVERSARY: ("2026-11-13", "2026-11-19"),
    SEASON: ("2026-11-13", "2027-01-31"),
}

# Общая цель ивента: порог тыкв и награда каждому, кто внёс хоть одну.
HALLOWEEN_GOALS = (
    (5000, {"coins": 50}),
    (15000, {"coins": 150}),
    (30000, {"item": "avatar_full_moon"}),
)


def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def event_window(name):
    """(первый день, последний день) события."""
    start_default, end_default = _DEFAULT_WINDOWS[name]
    prefix = name.upper()
    start = os.getenv(f"{prefix}_START", "").strip() or start_default
    end = os.getenv(f"{prefix}_END", "").strip() or end_default
    return date.fromisoformat(start), date.fromisoformat(end)


def _forced():
    return {x.strip() for x in os.getenv("EVENT_FORCE", "").split(",") if x.strip()}


def is_event_active(name, today=None):
    """EVENT_FORCE=halloween включает событие вне его дат — для проверки."""
    if name in _forced():
        return True
    start, end = event_window(name)
    return start <= (today or date.today()) <= end


def is_event_finished(name, today=None):
    if name in _forced():
        return False
    return (today or date.today()) > event_window(name)[1]


def days_left(name, today=None):
    """Сколько дней осталось, считая сегодняшний."""
    return max(0, (event_window(name)[1] - (today or date.today())).days + 1)


def pumpkins_per_play():
    return _env_int("PUMPKINS_PER_PLAY", 1)


def pumpkins_per_win():
    return _env_int("PUMPKINS_PER_WIN", 2)


def pumpkins_daily_game_cap():
    """Потолок тыкв за партии в день: монетка и слоты дают партию на каждое нажатие."""
    return _env_int("PUMPKINS_DAILY_GAME_CAP", 60)


def hunt_chance():
    return _env_float("PUMPKIN_HUNT_CHANCE", 0.04)


def hunt_cooldown():
    return _env_int("PUMPKIN_HUNT_COOLDOWN", 20 * 60)


def hunt_reward(rng=random):
    return rng.randint(_env_int("PUMPKIN_HUNT_MIN", 3), _env_int("PUMPKIN_HUNT_MAX", 7))


def halloween_goal():
    return HALLOWEEN_GOALS[-1][0]


def new_bucket():
    return {
        "pumpkins": 0,       # на руках, тратятся в лавке
        "earned": 0,         # всего добыто — по нему рейтинг
        "day": "",
        "day_games": 0,      # добыто за партии сегодня (для потолка)
        "caught": 0,         # поймано на охоте
        "claimed_goals": [],
    }


def credit(bucket, amount, source, today=None):
    """Начисляет тыквы в ведро игрока, соблюдая дневной потолок для партий.

    Возвращает, сколько реально начислено.
    """
    amount = max(0, int(amount))
    if not amount:
        return 0
    for key, value in new_bucket().items():
        bucket.setdefault(key, value)
    day = (today or date.today()).isoformat()
    if bucket.get("day") != day:
        bucket["day"] = day
        bucket["day_games"] = 0
    if source == "game":
        room = max(0, pumpkins_daily_game_cap() - int(bucket.get("day_games", 0)))
        amount = min(amount, room)
        bucket["day_games"] = int(bucket.get("day_games", 0)) + amount
    if source == "hunt" and amount:
        bucket["caught"] = int(bucket.get("caught", 0)) + 1
    bucket["pumpkins"] = int(bucket.get("pumpkins", 0)) + amount
    bucket["earned"] = int(bucket.get("earned", 0)) + amount
    return amount


def spend(bucket, price):
    """Списывает тыквы; False, если не хватает."""
    price = int(price)
    if int(bucket.get("pumpkins", 0)) < price:
        return False
    bucket["pumpkins"] = int(bucket.get("pumpkins", 0)) - price
    return True


def claimable_goals(total, bucket):
    """Пороги общей цели, которые достигнуты и ещё не забраны этим игроком."""
    if int(bucket.get("earned", 0)) <= 0:
        return []
    claimed = set(bucket.get("claimed_goals", []))
    return [need for need, _ in HALLOWEEN_GOALS if total >= need and need not in claimed]


def goal_reward(need):
    return dict(next(reward for threshold, reward in HALLOWEEN_GOALS if threshold == need))


def progress_bar(value, target, width=10):
    target = max(int(target or 1), 1)
    value = max(int(value or 0), 0)
    filled = min(width, int(width * min(value, target) / target))
    return "▓" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Сезонный пропуск: уровни за XP, награда на каждом уровне.
# ---------------------------------------------------------------------------
SEASON_ID = "s1"
SEASON_MAX_LEVEL = 20
SEASON_TITLE = "💎 Ветеран 1-го сезона"
SEASON_REWARDS = {
    5: {"coins": 100, "item": "avatar_cake"},
    10: {"coins": 150, "item": "theme_anniversary"},
    15: {"coins": 200, "item": "victory_party"},
    20: {"coins": 500, "item": "avatar_crown", "title": SEASON_TITLE},
}


def season_xp_per_level():
    return max(1, _env_int("SEASON_XP_PER_LEVEL", 150))


def season_xp_per_play():
    return _env_int("SEASON_XP_PER_PLAY", 5)


def season_xp_per_win():
    return _env_int("SEASON_XP_PER_WIN", 10)


def season_daily_game_xp_cap():
    return _env_int("SEASON_DAILY_GAME_XP_CAP", 250)


def season_reward(level):
    return dict(SEASON_REWARDS.get(level, {"coins": 50}))


def new_season_bucket():
    return {"xp": 0, "day": "", "day_xp": 0, "claimed": []}


def season_level(xp):
    return min(SEASON_MAX_LEVEL, max(0, int(xp)) // season_xp_per_level())


def season_progress(xp):
    """(уровень, XP внутри текущего уровня, XP на уровень); на максимуме — полный бар."""
    per = season_xp_per_level()
    level = season_level(xp)
    if level >= SEASON_MAX_LEVEL:
        return level, per, per
    return level, max(0, int(xp)) - level * per, per


def add_season_xp(bucket, amount, source, today=None):
    amount = max(0, int(amount))
    for key, value in new_season_bucket().items():
        bucket.setdefault(key, value)
    day = (today or date.today()).isoformat()
    if bucket.get("day") != day:
        bucket["day"] = day
        bucket["day_xp"] = 0
    if source == "game":
        amount = min(amount, max(0, season_daily_game_xp_cap() - int(bucket.get("day_xp", 0))))
        bucket["day_xp"] = int(bucket.get("day_xp", 0)) + amount
    bucket["xp"] = int(bucket.get("xp", 0)) + amount
    return amount


def claimable_levels(bucket):
    claimed = set(bucket.get("claimed", []))
    return [lvl for lvl in range(1, season_level(bucket.get("xp", 0)) + 1) if lvl not in claimed]


# Подарок на день рождения бота.
BIRTHDAY_GIFT = {"coins": 300, "item": "frame_anniversary"}
