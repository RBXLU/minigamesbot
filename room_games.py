import random
import time
from threading import Thread
from telebot import types

# Игры только для комнат (без inline). Ключи должны быть уникальными и стабильными.
ROOM_VOTE_GAMES = [
    ("room_rps", "Камень-ножницы-бумага"),
    ("room_duel", "Быстрая дуэль"),
    ("room_bship", "Морской бой"),
    ("room_quiz", "Викторина"),
    ("room_combo", "Комбо-битва"),
    ("room_mafia", "Мафия"),
]

# Состояние комнатных игр в памяти: код комнаты -> состояние
_room_rps_state = {}
_room_duel_state = {}
_room_bship_state = {}
_room_quiz_state = {}
_room_combo_state = {}
_room_mafia_state = {}
_ALL_ROOM_STATES = (
    _room_rps_state, _room_duel_state, _room_bship_state,
    _room_quiz_state, _room_combo_state, _room_mafia_state,
)
_room_state_persist_callback = None


def _jsonable(value):
    if isinstance(value, set):
        return list(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def configure_room_game_persistence(callback):
    global _room_state_persist_callback
    _room_state_persist_callback = callback


def export_room_runtime_state(code=None):
    maps = dict(zip(
        ("room_rps", "room_duel", "room_bship", "room_quiz", "room_combo", "room_mafia"),
        _ALL_ROOM_STATES,
    ))
    if code is not None:
        return {
            game_key: _jsonable(states.get(code))
            for game_key, states in maps.items()
            if code in states
        }
    return {game_key: _jsonable(states) for game_key, states in maps.items()}


def cleanup_room_runtime_state(code):
    for states in _ALL_ROOM_STATES:
        states.pop(code, None)
    _persist_room_state(code)


def remove_room_game_player(code, user_id):
    for states in _ALL_ROOM_STATES:
        st = states.get(code)
        if not isinstance(st, dict):
            continue
        for field in ("players", "ready"):
            value = st.get(field)
            if isinstance(value, set):
                value.discard(user_id)
            elif isinstance(value, list) and user_id in value:
                value.remove(user_id)
        for field in ("names", "moves", "scores", "votes", "hp", "shots", "ships"):
            value = st.get(field)
            if isinstance(value, dict):
                value.pop(user_id, None)
    _persist_room_state(code)


def _persist_room_state(code):
    if not _room_state_persist_callback:
        return
    try:
        _room_state_persist_callback(code, export_room_runtime_state(code))
    except Exception:
        pass


ROOM_GAME_START_TEXTS = {
    "room_rps": "Игра в чате. Нажмите «Присоединиться», затем выберите ход.",
    "room_duel": "Игра в чате. Дуэль 3 раунда: КНБ. Нажмите «Присоединиться» и выбирайте ход каждый раунд.",
    "room_bship": "Игра в чате. Нужны 2 игрока. Ходы вводятся сообщением: A1–E5.",
    "room_quiz": "Игра в чате. Нажмите «Присоединиться». Вопросы появятся ниже.",
    "room_combo": "Игра в чате. Нажмите «Присоединиться», затем выбирайте приемы.",
    "room_mafia": "Игра в чате. Нажмите «Присоединиться». После старта — голосование.",
}


def is_room_game(game_key):
    return any(k == game_key for k, _ in ROOM_VOTE_GAMES)


def room_game_start_text(game_key):
    return ROOM_GAME_START_TEXTS.get(game_key, "Игра в чате.")


def room_game_launch(bot, chat_id, code, room=None):
    game_key = room.get("game") if isinstance(room, dict) else None
    launcher = {
        "room_rps": _room_rps_launch,
        "room_duel": _room_duel_launch,
        "room_bship": _room_bship_launch,
        "room_quiz": _room_quiz_launch,
        "room_combo": _room_combo_launch,
        "room_mafia": _room_mafia_launch,
    }.get(game_key)
    if not launcher:
        return False
    launcher(bot, chat_id, code)
    return True


def _display_name(user):
    if not user:
        return "Игрок"
    if getattr(user, "username", None):
        return f"@{user.username}"
    return user.first_name or f"user_{user.id}"


def _join_kb(callback_prefix, code):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🤝 Присоединиться", callback_data=f"{callback_prefix}_join_{code}"))
    return kb


def _room_rps_launch(bot, chat_id, code):
    _room_rps_state[code] = {"players": [], "names": {}, "moves": {}}
    bot.send_message(chat_id, "🎮 Камень-ножницы-бумага\nНужны 2 игрока.", reply_markup=_join_kb("roomrps", code))
    _persist_room_state(code)


def _room_duel_launch(bot, chat_id, code):
    _room_duel_state[code] = {
        "players": [],
        "names": {},
        "moves": {},
        "scores": {},
        "round": 1,
        "status": "waiting",
    }
    bot.send_message(
        chat_id,
        "⚔️ Дуэль (3 раунда КНБ)\nНужны 2 игрока. Нажмите «Присоединиться».",
        reply_markup=_join_kb("roomduel", code),
    )
    _persist_room_state(code)


def _duel_move_kb(code):
    kb = types.InlineKeyboardMarkup()
    kb.row(*[
        types.InlineKeyboardButton(icon, callback_data=f"roomduel_move_{code}_{move}")
        for move, icon in (("rock", "🪨"), ("paper", "📄"), ("scissors", "✂️"))
    ])
    return kb


def _duel_score_line(st):
    p1, p2 = st["players"][0], st["players"][1]
    s1 = st["scores"].get(p1, 0)
    s2 = st["scores"].get(p2, 0)
    n1 = st["names"].get(p1, "P1")
    n2 = st["names"].get(p2, "P2")
    return f"{n1} {s1} : {s2} {n2}"


def _room_bship_launch(bot, chat_id, code):
    _room_bship_state[code] = {
        "chat_id": chat_id,
        "players": [],
        "names": {},
        "turn": None,
        "ships": {},
        "shots": {},
    }
    bot.send_message(
        chat_id,
        "🚢 Морской бой\nНужны 2 игрока. Вводите ходы как A1–E5.",
        reply_markup=_join_kb("roombship", code),
    )
    _persist_room_state(code)


def _room_quiz_launch(bot, chat_id, code):
    _room_quiz_state[code] = {"chat_id": chat_id, "players": set(), "names": {}, "scores": {}, "qidx": 0}
    bot.send_message(
        chat_id,
        "🧠 Викторина\nНажмите «Присоединиться», затем «Старт».",
        reply_markup=_join_kb("roomquiz", code),
    )
    _persist_room_state(code)


def _room_combo_launch(bot, chat_id, code):
    _room_combo_state[code] = {"chat_id": chat_id, "players": [], "names": {}, "moves": {}, "hp": {}}
    bot.send_message(chat_id, "🥊 Комбо-битва\nНужны 2 игрока.", reply_markup=_join_kb("roomcombo", code))
    _persist_room_state(code)


def _room_mafia_launch(bot, chat_id, code):
    _room_mafia_state[code] = {"chat_id": chat_id, "players": set(), "names": {}, "votes": {}, "mafia": None}
    bot.send_message(chat_id, "🕵️ Мафия\nНужны минимум 3 игрока.", reply_markup=_join_kb("roommafia", code))
    _persist_room_state(code)


def register_room_game_handlers(bot):
    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomrps_join_"))
    def room_rps_join(call):
        code = call.data.split("_", 2)[2]
        st = _room_rps_state.setdefault(code, {"players": [], "names": {}, "moves": {}})
        uid = call.from_user.id
        st["names"][uid] = _display_name(call.from_user)
        if uid in st["players"]:
            bot.answer_callback_query(call.id, "Вы уже в игре.")
            return
        if len(st["players"]) >= 2:
            bot.answer_callback_query(call.id, "В игре уже 2 игрока.")
            return
        st["players"].append(uid)
        bot.answer_callback_query(call.id, "Вы в игре!")
        _persist_room_state(code)
        if len(st["players"]) == 2:
            kb = types.InlineKeyboardMarkup()
            kb.row(
                types.InlineKeyboardButton("🪨 Камень", callback_data=f"roomrps_move_{code}_rock"),
                types.InlineKeyboardButton("📄 Бумага", callback_data=f"roomrps_move_{code}_paper"),
                types.InlineKeyboardButton("✂️ Ножницы", callback_data=f"roomrps_move_{code}_scissors"),
            )
            bot.send_message(call.message.chat.id, "Ходы: выберите вариант.", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomrps_move_"))
    def room_rps_move(call):
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Ошибка данных.")
            return
        code = parts[2]
        move = parts[3]
        st = _room_rps_state.get(code)
        if not st:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        uid = call.from_user.id
        if uid not in st["players"]:
            bot.answer_callback_query(call.id, "Вы не участвуете в этой партии.")
            return
        if uid in st["moves"]:
            bot.answer_callback_query(call.id, "Вы уже сделали ход.")
            return
        if move not in ("rock", "paper", "scissors"):
            bot.answer_callback_query(call.id, "Неверный ход.")
            return
        st["moves"][uid] = move
        bot.answer_callback_query(call.id, "Ход принят.")
        _persist_room_state(code)

        if len(st["moves"]) < 2:
            return

        p1, p2 = st["players"][0], st["players"][1]
        m1, m2 = st["moves"].get(p1), st["moves"].get(p2)
        if not m1 or not m2:
            return

        res = _rps_result(m1, m2)
        if res == 0:
            text = "🤝 Ничья!"
        elif res == 1:
            text = f"🎉 Победил {st['names'].get(p1, p1)}"
        else:
            text = f"🎉 Победил {st['names'].get(p2, p2)}"
        bot.send_message(call.message.chat.id, text)
        _room_rps_state.pop(code, None)
        _persist_room_state(code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomduel_join_"))
    def room_duel_join(call):
        code = call.data.split("_", 2)[2]
        st = _room_duel_state.setdefault(code, {
            "players": [], "names": {}, "moves": {}, "scores": {}, "round": 1, "status": "waiting"
        })
        uid = call.from_user.id
        if uid in st["players"]:
            bot.answer_callback_query(call.id, "Вы уже в игре.")
            return
        if len(st["players"]) >= 2:
            bot.answer_callback_query(call.id, "В дуэли уже 2 игрока.")
            return
        st["players"].append(uid)
        st["names"][uid] = _display_name(call.from_user)
        st["scores"][uid] = 0
        bot.answer_callback_query(call.id, "Вы в дуэли!")
        _persist_room_state(code)
        if len(st["players"]) == 2:
            st["status"] = "playing"
            rnd = st["round"]
            bot.send_message(
                call.message.chat.id,
                f"⚔️ Раунд {rnd}/3\nВыберите ход!\n{_duel_score_line(st)}",
                reply_markup=_duel_move_kb(code),
            )
            _persist_room_state(code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomduel_move_"))
    def room_duel_move(call):
        parts = call.data.split("_", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Ошибка данных.")
            return
        code = parts[2]
        move = parts[3]
        st = _room_duel_state.get(code)
        if not st or st.get("status") != "playing":
            bot.answer_callback_query(call.id, "Дуэль не найдена или не активна.")
            return
        uid = call.from_user.id
        if uid not in st["players"]:
            bot.answer_callback_query(call.id, "Вы не участник этой дуэли.")
            return
        if uid in st.get("moves", {}):
            bot.answer_callback_query(call.id, "Вы уже сделали ход в этом раунде.")
            return
        if move not in ("rock", "paper", "scissors"):
            bot.answer_callback_query(call.id, "Неверный ход.")
            return
        st.setdefault("moves", {})[uid] = move
        bot.answer_callback_query(call.id, "Ход принят!")
        _persist_room_state(code)

        if len(st["moves"]) < 2:
            return

        p1, p2 = st["players"][0], st["players"][1]
        m1, m2 = st["moves"].get(p1), st["moves"].get(p2)
        move_names = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        round_text = (
            f"Раунд {st['round']}: "
            f"{st['names'].get(p1)} {move_names.get(m1,'?')} vs "
            f"{st['names'].get(p2)} {move_names.get(m2,'?')}"
        )
        res = _rps_result(m1, m2)
        if res == 1:
            st["scores"][p1] = st["scores"].get(p1, 0) + 1
            round_text += f"\n→ Очко: {st['names'].get(p1)}"
        elif res == 2:
            st["scores"][p2] = st["scores"].get(p2, 0) + 1
            round_text += f"\n→ Очко: {st['names'].get(p2)}"
        else:
            round_text += "\n→ Ничья в раунде"

        st["moves"] = {}
        st["round"] = st.get("round", 1) + 1
        _persist_room_state(code)

        if st["round"] > 3:
            s1 = st["scores"].get(p1, 0)
            s2 = st["scores"].get(p2, 0)
            if s1 > s2:
                winner_name = st["names"].get(p1, str(p1))
            elif s2 > s1:
                winner_name = st["names"].get(p2, str(p2))
            else:
                winner_name = None
            score_line = _duel_score_line(st)
            if winner_name:
                final_text = f"{round_text}\n\n{score_line}\n🏆 Победитель дуэли: {winner_name}!"
            else:
                final_text = f"{round_text}\n\n{score_line}\n🤝 Дуэль завершилась вничью!"
            bot.send_message(call.message.chat.id, f"⚔️ Дуэль завершена\n{final_text}")
            st["status"] = "ended"
            _room_duel_state.pop(code, None)
            _persist_room_state(code)
        else:
            rnd = st["round"]
            bot.send_message(
                call.message.chat.id,
                f"{round_text}\n\n⚔️ Раунд {rnd}/3\nВыберите ход!\n{_duel_score_line(st)}",
                reply_markup=_duel_move_kb(code),
            )

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roombship_join_"))
    def room_bship_join(call):
        code = call.data.split("_", 2)[2]
        st = _room_bship_state.setdefault(
            code,
            {"chat_id": call.message.chat.id, "players": [], "names": {}, "turn": None, "ships": {}, "shots": {}},
        )
        uid = call.from_user.id
        if uid in st["players"]:
            bot.answer_callback_query(call.id, "Вы уже в игре.")
            return
        if len(st["players"]) >= 2:
            bot.answer_callback_query(call.id, "В игре уже 2 игрока.")
            return
        st["players"].append(uid)
        st["names"][uid] = _display_name(call.from_user)
        bot.answer_callback_query(call.id, "Вы в игре!")
        _persist_room_state(code)
        if len(st["players"]) == 2:
            _bship_init_round(bot, st)
            _persist_room_state(code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomquiz_join_"))
    def room_quiz_join(call):
        code = call.data.split("_", 2)[2]
        st = _room_quiz_state.setdefault(
            code,
            {"chat_id": call.message.chat.id, "players": set(), "names": {}, "scores": {}, "qidx": 0, "answered": {}},
        )
        uid = call.from_user.id
        st["players"].add(uid)
        st["names"][uid] = _display_name(call.from_user)
        st["scores"].setdefault(uid, 0)
        bot.answer_callback_query(call.id, "Вы в викторине!")
        _persist_room_state(code)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("▶️ Старт", callback_data=f"roomquiz_start_{code}"))
        bot.send_message(call.message.chat.id, "Когда все подключились — жмите «Старт».", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomquiz_start_"))
    def room_quiz_start(call):
        code = call.data.split("_", 2)[2]
        st = _room_quiz_state.get(code)
        if not st:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        if len(st["players"]) < 1:
            bot.answer_callback_query(call.id, "Нужен хотя бы 1 игрок.")
            return
        bot.answer_callback_query(call.id, "Стартуем!")
        _quiz_next_question(bot, code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomquiz_ans_"))
    def room_quiz_answer(call):
        parts = call.data.split("_")
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "Ошибка.")
            return
        code = parts[2]
        qidx = int(parts[3])
        ans = int(parts[4])
        st = _room_quiz_state.get(code)
        if not st or st.get("qidx") != qidx:
            bot.answer_callback_query(call.id, "Этот вопрос уже завершен.")
            return
        uid = call.from_user.id
        if uid not in st["players"]:
            bot.answer_callback_query(call.id, "Вы не в игре.")
            return
        answered = st.setdefault("answered", {}).setdefault(qidx, set())
        if uid in answered:
            bot.answer_callback_query(call.id, "Вы уже ответили.")
            return
        answered.add(uid)
        if _QUIZ_QUESTIONS[qidx]["answer"] == ans:
            st["scores"][uid] = st["scores"].get(uid, 0) + 1
            bot.answer_callback_query(call.id, "Верно!")
        else:
            bot.answer_callback_query(call.id, "Неверно.")
        _persist_room_state(code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomcombo_join_"))
    def room_combo_join(call):
        code = call.data.split("_", 2)[2]
        st = _room_combo_state.setdefault(
            code, {"chat_id": call.message.chat.id, "players": [], "names": {}, "moves": {}, "hp": {}}
        )
        uid = call.from_user.id
        if uid in st["players"]:
            bot.answer_callback_query(call.id, "Вы уже в игре.")
            return
        if len(st["players"]) >= 2:
            bot.answer_callback_query(call.id, "В игре уже 2 игрока.")
            return
        st["players"].append(uid)
        st["names"][uid] = _display_name(call.from_user)
        bot.answer_callback_query(call.id, "Вы в игре!")
        _persist_room_state(code)
        if len(st["players"]) == 2:
            st["hp"] = {st["players"][0]: 3, st["players"][1]: 3}
            _combo_prompt_moves(bot, call.message.chat.id, code)
            _persist_room_state(code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roomcombo_move_"))
    def room_combo_move(call):
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Ошибка.")
            return
        code = parts[2]
        move = parts[3]
        st = _room_combo_state.get(code)
        if not st:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        uid = call.from_user.id
        if uid not in st["players"]:
            bot.answer_callback_query(call.id, "Вы не участвуете.")
            return
        if move not in ("punch", "kick", "block"):
            bot.answer_callback_query(call.id, "Неверный прием.")
            return
        st["moves"][uid] = move
        bot.answer_callback_query(call.id, "Принято.")
        _persist_room_state(code)
        if len(st["moves"]) < 2:
            return
        _combo_resolve_round(bot, call.message.chat.id, code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roommafia_join_"))
    def room_mafia_join(call):
        code = call.data.split("_", 2)[2]
        st = _room_mafia_state.setdefault(
            code, {"chat_id": call.message.chat.id, "players": set(), "names": {}, "votes": {}, "mafia": None}
        )
        uid = call.from_user.id
        st["players"].add(uid)
        st["names"][uid] = _display_name(call.from_user)
        bot.answer_callback_query(call.id, "Вы в мафии!")
        _persist_room_state(code)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("▶️ Старт", callback_data=f"roommafia_start_{code}"))
        bot.send_message(call.message.chat.id, "Когда все подключились — жмите «Старт».", reply_markup=kb)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roommafia_start_"))
    def room_mafia_start(call):
        code = call.data.split("_", 2)[2]
        st = _room_mafia_state.get(code)
        if not st or len(st["players"]) < 3:
            bot.answer_callback_query(call.id, "Нужно минимум 3 игрока.")
            return
        mafia = random.choice(list(st["players"]))
        st["mafia"] = mafia
        bot.answer_callback_query(call.id, "Старт!")
        bot.send_message(call.message.chat.id, "Мафия назначена. Голосуем!")
        _persist_room_state(code)
        _mafia_vote_prompt(bot, call.message.chat.id, code)

    @bot.callback_query_handler(func=lambda c: c.data.startswith("roommafia_vote_"))
    def room_mafia_vote(call):
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Ошибка.")
            return
        code = parts[2]
        target = int(parts[3])
        st = _room_mafia_state.get(code)
        if not st:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        uid = call.from_user.id
        if uid not in st["players"]:
            bot.answer_callback_query(call.id, "Вы не участник.")
            return
        st["votes"][uid] = target
        bot.answer_callback_query(call.id, "Голос учтен.")
        _persist_room_state(code)
        if len(st["votes"]) == len(st["players"]):
            _mafia_finish(bot, call.message.chat.id, code)

    @bot.message_handler(func=lambda m: bool(m.chat) and m.chat.type in ("group", "supergroup") and bool(_find_bship_code_by_chat(m.chat.id)))
    def room_bship_message_router(message):
        text = (message.text or "").strip().upper()
        if len(text) != 2:
            return
        code = _find_bship_code_by_chat(message.chat.id)
        if not code:
            return
        st = _room_bship_state.get(code)
        if not st:
            return
        _bship_handle_shot(bot, message, st, text)


_QUIZ_QUESTIONS = [
    {"q": "Столица Франции?", "opts": ["Лион", "Париж", "Марсель", "Нант"], "answer": 1},
    {"q": "2 + 2 = ?", "opts": ["3", "4", "5", "6"], "answer": 1},
    {"q": "Самая большая планета?", "opts": ["Марс", "Земля", "Юпитер", "Венера"], "answer": 2},
    {"q": "Сколько дней в неделе?", "opts": ["5", "6", "7", "8"], "answer": 2},
    {"q": "Что из этого — язык программирования?", "opts": ["Python", "Violet", "Mercury", "Omega"], "answer": 0},
]


def _quiz_next_question(bot, code):
    st = _room_quiz_state.get(code)
    if not st:
        return
    qidx = st.get("qidx", 0)
    if qidx >= len(_QUIZ_QUESTIONS):
        _quiz_finish(bot, st["chat_id"], code)
        return
    q = _QUIZ_QUESTIONS[qidx]
    kb = types.InlineKeyboardMarkup()
    for i, opt in enumerate(q["opts"]):
        kb.add(types.InlineKeyboardButton(opt, callback_data=f"roomquiz_ans_{code}_{qidx}_{i}"))
    bot.send_message(st["chat_id"], f"❓ {q['q']}", reply_markup=kb)

    def finalize():
        time.sleep(20)
        st2 = _room_quiz_state.get(code)
        if not st2 or st2.get("qidx") != qidx:
            return
        st2["qidx"] = qidx + 1
        _persist_room_state(code)
        _quiz_next_question(bot, code)

    Thread(target=finalize, daemon=True).start()


def _quiz_finish(bot, chat_id, code):
    st = _room_quiz_state.get(code)
    if not st:
        return
    scores = st.get("scores", {})
    if not scores:
        bot.send_message(chat_id, "Викторина завершена. Нет ответов.")
    else:
        top = max(scores.values())
        winners = [uid for uid, sc in scores.items() if sc == top]
        names = ", ".join([st["names"].get(uid, str(uid)) for uid in winners])
        bot.send_message(chat_id, f"🏆 Победители: {names} (очки: {top})")
    _room_quiz_state.pop(code, None)
    _persist_room_state(code)


def _combo_prompt_moves(bot, chat_id, code):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("👊 Удар", callback_data=f"roomcombo_move_{code}_punch"),
        types.InlineKeyboardButton("🦵 Пинок", callback_data=f"roomcombo_move_{code}_kick"),
        types.InlineKeyboardButton("🛡 Блок", callback_data=f"roomcombo_move_{code}_block"),
    )
    bot.send_message(chat_id, "Выберите прием:", reply_markup=kb)


def _combo_resolve_round(bot, chat_id, code):
    st = _room_combo_state.get(code)
    if not st:
        return
    p1, p2 = st["players"][0], st["players"][1]
    m1, m2 = st["moves"].get(p1), st["moves"].get(p2)
    if not m1 or not m2:
        return
    res = _rps_result(_combo_to_rps(m1), _combo_to_rps(m2))
    if res == 1:
        st["hp"][p2] -= 1
        bot.send_message(chat_id, f"Раунд за {st['names'].get(p1, p1)}")
    elif res == 2:
        st["hp"][p1] -= 1
        bot.send_message(chat_id, f"Раунд за {st['names'].get(p2, p2)}")
    else:
        bot.send_message(chat_id, "Раунд вничью")
    st["moves"] = {}
    if st["hp"][p1] <= 0 or st["hp"][p2] <= 0:
        winner = p1 if st["hp"][p1] > st["hp"][p2] else p2
        bot.send_message(chat_id, f"🏆 Победил {st['names'].get(winner, winner)}")
        _room_combo_state.pop(code, None)
        _persist_room_state(code)
        return
    bot.send_message(chat_id, f"HP: {st['names'].get(p1, p1)}={st['hp'][p1]} | {st['names'].get(p2, p2)}={st['hp'][p2]}")
    _persist_room_state(code)
    _combo_prompt_moves(bot, chat_id, code)


def _combo_to_rps(move):
    return {"punch": "rock", "kick": "scissors", "block": "paper"}[move]


def _mafia_vote_prompt(bot, chat_id, code):
    st = _room_mafia_state.get(code)
    if not st:
        return
    kb = types.InlineKeyboardMarkup()
    for uid in st["players"]:
        name = st["names"].get(uid, str(uid))
        kb.add(types.InlineKeyboardButton(name, callback_data=f"roommafia_vote_{code}_{uid}"))
    bot.send_message(chat_id, "Голосование: выберите игрока.", reply_markup=kb)

    def finalize():
        time.sleep(25)
        if _room_mafia_state.get(code):
            _mafia_finish(bot, chat_id, code)

    Thread(target=finalize, daemon=True).start()


def _mafia_finish(bot, chat_id, code):
    st = _room_mafia_state.get(code)
    if not st:
        return
    votes = st.get("votes", {})
    if not votes:
        bot.send_message(chat_id, "Голосов нет. Победила мафия.")
        _room_mafia_state.pop(code, None)
        _persist_room_state(code)
        return
    tally = {}
    for target in votes.values():
        tally[target] = tally.get(target, 0) + 1
    target = max(tally, key=tally.get)
    if target == st.get("mafia"):
        bot.send_message(chat_id, "🎉 Мафия поймана! Победа граждан.")
    else:
        mafia_name = st["names"].get(st.get("mafia"), "мафия")
        bot.send_message(chat_id, f"💀 Мафия победила. Мафия была: {mafia_name}")
    _room_mafia_state.pop(code, None)
    _persist_room_state(code)


def _bship_init_round(bot, st):
    players = st["players"]
    st["ships"][players[0]] = _bship_place_ships()
    st["ships"][players[1]] = _bship_place_ships()
    st["shots"][players[0]] = set()
    st["shots"][players[1]] = set()
    st["turn"] = players[0]
    bot.send_message(st["chat_id"], f"Игра началась! Первый ход: {st['names'].get(players[0], players[0])}.")


def _bship_place_ships():
    cells = set()
    while len(cells) < 3:
        r = random.choice("ABCDE")
        c = random.randint(1, 5)
        cells.add(f"{r}{c}")
    return cells


def _find_bship_code_by_chat(chat_id):
    for code, st in _room_bship_state.items():
        if st.get("chat_id") == chat_id:
            return code
    return None


def _bship_handle_shot(bot, message, st, text):
    uid = message.from_user.id
    if uid != st.get("turn"):
        return
    if text[0] not in "ABCDE" or text[1] not in "12345":
        return
    if text in st["shots"].get(uid, set()):
        bot.send_message(st["chat_id"], "Эта клетка уже была.")
        return
    st["shots"][uid].add(text)
    opponent = st["players"][1] if st["players"][0] == uid else st["players"][0]
    if text in st["ships"][opponent]:
        st["ships"][opponent].remove(text)
        bot.send_message(st["chat_id"], f"💥 Попадание! {st['names'].get(uid, uid)}")
        if not st["ships"][opponent]:
            bot.send_message(st["chat_id"], f"🏆 Победил {st['names'].get(uid, uid)}")
            code = _find_bship_code_by_chat(st["chat_id"])
            _room_bship_state.pop(code, None)
            _persist_room_state(code)
            return
    else:
        bot.send_message(st["chat_id"], "Мимо.")
    st["turn"] = opponent
    bot.send_message(st["chat_id"], f"Ход: {st['names'].get(opponent, opponent)}")
    _persist_room_state(_find_bship_code_by_chat(st["chat_id"]))


def _rps_result(m1, m2):
    """0 — ничья, 1 — победил первый, 2 — победил второй."""
    if m1 == m2:
        return 0
    wins = {("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")}
    return 1 if (m1, m2) in wins else 2
