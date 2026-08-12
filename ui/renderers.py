def render_profile_text(uid, user, lang, achievements_count, achievements_total, get_game_title):
    total = int(user.get("games_total", 0) or 0)
    coins = int(user.get("coins", 0) or 0)
    gstats = user.get("game_stats", {}) if isinstance(user.get("game_stats", {}), dict) else {}
    history = user.get("match_history", []) if isinstance(user.get("match_history", []), list) else []
    display_name = user.get("display_name") or f"user_{uid}"
    avatar = user.get("avatar_emoji", "🙂")
    frame_style = user.get("frame_style", "base")
    theme_style = user.get("theme_style", "classic")
    victory_emoji = user.get("victory_emoji", "🎉")

    fav_game = "—"
    fav_count = 0
    wins_total = 0
    losses_total = 0
    draws_total = 0
    for game_key, row in gstats.items():
        played = int((row or {}).get("played", 0) or 0)
        wins_total += int((row or {}).get("wins", 0) or 0)
        losses_total += int((row or {}).get("losses", 0) or 0)
        draws_total += int((row or {}).get("draws", 0) or 0)
        if played > fav_count:
            fav_count = played
            fav_game = get_game_title(uid, game_key)

    rated_games = wins_total + losses_total
    winrate = (wins_total * 100.0 / rated_games) if rated_games > 0 else 0.0

    if lang == "uk":
        lines = [
            f"{avatar} Профіль: {display_name}",
            f"tg://emoji?id=5467583879948803288 Всього зіграно: {total}",
            f"🪙 Монети: {coins}",
            f"tg://emoji?id=5409008750893734809 Досягнення: {achievements_count}/{achievements_total}",
            f"🏅 Улюблена гра: {fav_game}" + (f" ({fav_count})" if fav_count else ""),
            f"📈 Winrate: {winrate:.1f}% (W:{wins_total} L:{losses_total} D:{draws_total})",
            f"🎨 Оформлення: рамка={frame_style}, тема={theme_style}, перемога={victory_emoji}",
        ]
        stats_title = "tg://emoji?id=5431577498364158238 Статистика по іграх:"
        stats_empty = "tg://emoji?id=5431577498364158238 Статистика по іграх: поки порожньо"
        history_title = "🕓 Останні матчі:"
        default_game = "Гра"
    elif lang == "en":
        lines = [
            f"{avatar} Profile: {display_name}",
            f"tg://emoji?id=5467583879948803288 Total games: {total}",
            f"🪙 Coins: {coins}",
            f"tg://emoji?id=5409008750893734809 Achievements: {achievements_count}/{achievements_total}",
            f"🏅 Favorite game: {fav_game}" + (f" ({fav_count})" if fav_count else ""),
            f"📈 Winrate: {winrate:.1f}% (W:{wins_total} L:{losses_total} D:{draws_total})",
            f"🎨 Style: frame={frame_style}, theme={theme_style}, victory={victory_emoji}",
        ]
        stats_title = "tg://emoji?id=5431577498364158238 Game stats:"
        stats_empty = "tg://emoji?id=5431577498364158238 Game stats: empty for now"
        history_title = "🕓 Recent matches:"
        default_game = "Game"
    else:
        lines = [
            f"{avatar} Профиль: {display_name}",
            f"tg://emoji?id=5467583879948803288 Всего сыграно: {total}",
            f"🪙 Монеты: {coins}",
            f"tg://emoji?id=5409008750893734809 Достижения: {achievements_count}/{achievements_total}",
            f"🏅 Любимая игра: {fav_game}" + (f" ({fav_count})" if fav_count else ""),
            f"📈 Winrate: {winrate:.1f}% (W:{wins_total} L:{losses_total} D:{draws_total})",
            f"🎨 Оформление: рамка={frame_style}, тема={theme_style}, победа={victory_emoji}",
        ]
        stats_title = "tg://emoji?id=5431577498364158238 Статистика по играм:"
        stats_empty = "tg://emoji?id=5431577498364158238 Статистика по играм: пока пусто"
        history_title = "🕓 Последние матчи:"
        default_game = "Игра"

    if gstats:
        lines.append("")
        lines.append(stats_title)
        rows = sorted(gstats.items(), key=lambda kv: int((kv[1] or {}).get("played", 0) or 0), reverse=True)
        for game_key, row in rows:
            played = int((row or {}).get("played", 0) or 0)
            if played <= 0:
                continue
            lines.append(f"• {get_game_title(uid, game_key)}: {played}")
    else:
        lines.append("")
        lines.append(stats_empty)

    if history:
        lines.append("")
        lines.append(history_title)
        for item in history[-10:][::-1]:
            game_key = str(item.get("game", ""))
            title = get_game_title(uid, game_key) if game_key else default_game
            lines.append(f"• {title} — {str(item.get('at', ''))}")

    return "\n".join(lines)


def render_achievements_text(lang, achievements, unlocked):
    total = len(achievements)
    unlocked_count = len(unlocked)
    if lang == "uk":
        lines = [f"tg://emoji?id=5409008750893734809 Досягнення: {unlocked_count}/{total}"]
        opened_title = "tg://emoji?id=5427009714745517609 Відкрито:"
        locked_title = "🔒 Закрито:"
    elif lang == "en":
        lines = [f"tg://emoji?id=5409008750893734809 Achievements: {unlocked_count}/{total}"]
        opened_title = "tg://emoji?id=5427009714745517609 Unlocked:"
        locked_title = "🔒 Locked:"
    else:
        lines = [f"tg://emoji?id=5409008750893734809 Достижения: {unlocked_count}/{total}"]
        opened_title = "tg://emoji?id=5427009714745517609 Открыты:"
        locked_title = "🔒 Закрыты:"

    if unlocked:
        lines.append("")
        lines.append(opened_title)
        for key, meta in achievements.items():
            if key in unlocked:
                when = unlocked.get(key, "")
                lines.append(f"• {meta['title']} — {meta['desc']}" + (f" ({when})" if when else ""))

    locked = [key for key in achievements.keys() if key not in unlocked]
    if locked:
        lines.append("")
        lines.append(locked_title)
        for key in locked:
            meta = achievements[key]
            lines.append(f"• {meta['title']} — {meta['desc']}")
    return "\n".join(lines)


def render_main_menu_status(uid, user, completed_quests, total_quests, localized_text):
    streak = int(user.get("streak_current", 0) or 0)
    coins = int(user.get("coins", 0) or 0)
    total_games = int(user.get("games_total", 0) or 0)
    return localized_text(
        uid,
        (
            f"📊 Прогресс: {total_games} игр, {coins} монет, серия {streak} дн.\n"
            f"🎯 Квесты: {completed_quests}/{total_quests}"
        ),
        (
            f"📊 Progress: {total_games} games, {coins} coins, streak {streak} days\n"
            f"🎯 Quests: {completed_quests}/{total_quests}"
        ),
        (
            f"📊 Прогрес: {total_games} ігор, {coins} монет, серія {streak} дн.\n"
            f"🎯 Квести: {completed_quests}/{total_quests}"
        ),
    )


def build_last_game_instruction(uid, user, get_game_title, localized_text, inline_bot_username):
    game_key = str(user.get("last_game") or "").strip()
    if not game_key:
        return None

    title = get_game_title(uid, game_key)
    if game_key == "flappy":
        action = localized_text(uid, "Команда: /flappy", "Command: /flappy", "Команда: /flappy")
    elif game_key in {"snake", "g2048", "tetris", "pong", "hangman", "minesweeper", "quizgame", "combogame", "mafia", "wordgame", "rps"}:
        action = localized_text(
            uid,
            f"Введите в любом чате: @{inline_bot_username} {game_key}",
            f"Type in any chat: @{inline_bot_username} {game_key}",
            f"Введіть у будь-якому чаті: @{inline_bot_username} {game_key}",
        )
    else:
        action = localized_text(
            uid,
            f"Откройте раздел игр и выберите {title}.",
            f"Open the games section and choose {title}.",
            f"Відкрийте розділ ігор і виберіть {title}.",
        )

    header = localized_text(
        uid,
        f"▶️ Последняя игра: {title}",
        f"▶️ Last game: {title}",
        f"▶️ Остання гра: {title}",
    )
    if user.get("last_game_at"):
        header += f"\n🕓 {user['last_game_at']}"
    return f"{header}\n{action}"
