"""Игры на удачу для Mini App, которые разыгрывает сервер.

Клиент только показывает карты и барабаны: исход выбирает сервер, ставка
списывается при начале раздачи, выигрыш выплачивает сервер. Модуль ничего не
знает про Telegram и базу — только правила, поэтому проверяется без бота.
"""
import random

BET_MIN = 10
BET_MAX = 500

SLOT_SYMBOLS = ["🍒", "🍋", "🍉", "⭐", "💎", "7️⃣"]
SLOT_JACKPOT = "7️⃣"

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]

HAND_NAMES = {
    8: "Стрит-флеш", 7: "Каре", 6: "Фулл-хаус", 5: "Флеш", 4: "Стрит",
    3: "Сет", 2: "Две пары", 1: "Пара", 0: "Старшая карта",
}
POKER_STAGES = ["preflop", "flop", "turn", "river", "showdown"]
POKER_VISIBLE = {"preflop": 0, "flop": 3, "turn": 4, "river": 5, "showdown": 5}


def clamp_bet(bet, balance):
    """Ставка в допустимых пределах; None, если на балансе меньше минимума."""
    try:
        bet = int(bet)
    except (TypeError, ValueError):
        bet = BET_MIN
    bet = max(BET_MIN, min(BET_MAX, bet))
    if balance < BET_MIN:
        return None
    return min(bet, balance)


def payout(bet, result):
    """Сколько вернуть игроку, если ставка уже списана."""
    return {"wins": 2 * bet, "draws": bet}.get(result, 0)


# ------------------------------- монетка, слоты -------------------------------

def coin_flip(side, rng=random):
    got = rng.choice(["heads", "tails"])
    return got, ("wins" if got == side else "losses")


def slot_spin(rng=random):
    """(барабаны, множитель выплаты к ставке)."""
    reels = [rng.choice(SLOT_SYMBOLS) for _ in range(3)]
    unique = len(set(reels))
    if unique == 1:
        multiplier = 20 if reels[0] == SLOT_JACKPOT else 10
    elif unique == 2:
        multiplier = 1
    else:
        multiplier = 0
    return reels, multiplier


# ------------------------------------ колода ------------------------------------

def new_deck(rng=random):
    deck = [[rank, suit] for suit in SUITS for rank in RANKS]
    rng.shuffle(deck)
    return deck


# ---------------------------------- блэкджек ----------------------------------

def bj_value(hand):
    total, aces = 0, 0
    for rank, _ in hand:
        if rank == "A":
            total, aces = total + 11, aces + 1
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total, aces = total - 10, aces - 1
    return total


def bj_new(rng=random):
    deck = new_deck(rng)
    state = {"deck": deck, "player": [deck.pop(), deck.pop()], "dealer": [deck.pop(), deck.pop()],
             "status": "playing", "result": None}
    if bj_value(state["player"]) == 21:
        _bj_finish(state, "draws" if bj_value(state["dealer"]) == 21 else "wins")
    return state


def _bj_finish(state, result):
    state["status"] = "ended"
    state["result"] = result


def bj_hit(state):
    if state["status"] != "playing":
        return state
    state["player"].append(state["deck"].pop())
    if bj_value(state["player"]) > 21:
        _bj_finish(state, "losses")
    elif bj_value(state["player"]) == 21:
        bj_stand(state)
    return state


def bj_stand(state):
    if state["status"] != "playing":
        return state
    while bj_value(state["dealer"]) < 17 and state["deck"]:
        state["dealer"].append(state["deck"].pop())
    p, d = bj_value(state["player"]), bj_value(state["dealer"])
    _bj_finish(state, "wins" if d > 21 or p > d else "losses" if p < d else "draws")
    return state


def bj_public(state):
    """То, что видит игрок: закрытая карта дилера не уходит на клиент до конца раздачи."""
    ended = state["status"] != "playing"
    dealer = state["dealer"] if ended else state["dealer"][:1]
    return {
        "player": state["player"],
        "dealer": dealer,
        "dealer_hidden": 0 if ended else len(state["dealer"]) - 1,
        "player_value": bj_value(state["player"]),
        "dealer_value": bj_value(state["dealer"]) if ended else bj_value(dealer),
        "status": state["status"],
        "result": state["result"],
    }


# ------------------------------------ покер ------------------------------------

def _hand_rank(cards):
    values = sorted((RANKS.index(rank) for rank, _ in cards), reverse=True)
    flush = len({suit for _, suit in cards}) == 1
    unique = sorted(set(values), reverse=True)
    straight = len(unique) == 5 and unique[0] - unique[4] == 4
    ordered = values
    if unique == [12, 3, 2, 1, 0]:          # A-2-3-4-5
        straight, ordered = True, [3, 2, 1, 0, -1]
    counts = sorted(((values.count(v), v) for v in unique), reverse=True)
    shape = [c for c, _ in counts]
    by_count = [v for _, v in counts]
    if straight and flush:
        return (8, ordered)
    if shape[0] == 4:
        return (7, by_count)
    if shape[:2] == [3, 2]:
        return (6, by_count)
    if flush:
        return (5, ordered)
    if straight:
        return (4, ordered)
    if shape[0] == 3:
        return (3, by_count)
    if shape[:2] == [2, 2]:
        return (2, by_count)
    if shape[0] == 2:
        return (1, by_count)
    return (0, ordered)


def best_hand(cards):
    from itertools import combinations

    return max(_hand_rank(list(combo)) for combo in combinations(cards, 5))


def poker_new(rng=random):
    deck = new_deck(rng)
    return {
        "player": [deck.pop(), deck.pop()],
        "bot": [deck.pop(), deck.pop()],
        "community": [deck.pop() for _ in range(5)],
        "stage": "preflop",
        "status": "playing",
        "result": None,
    }


def poker_next(state):
    """Чек: открыть следующие общие карты, на ривере — вскрытие."""
    if state["status"] != "playing":
        return state
    state["stage"] = POKER_STAGES[POKER_STAGES.index(state["stage"]) + 1]
    if state["stage"] == "showdown":
        mine = best_hand(state["player"] + state["community"])
        theirs = best_hand(state["bot"] + state["community"])
        state["result"] = "wins" if mine > theirs else "losses" if theirs > mine else "draws"
        state["status"] = "ended"
    return state


def poker_fold(state):
    if state["status"] == "playing":
        state["status"] = "ended"
        state["result"] = "losses"
        state["folded"] = True
    return state


def poker_public(state):
    ended = state["status"] != "playing"
    shown = state["community"][:POKER_VISIBLE[state["stage"]]]
    data = {
        "player": state["player"],
        "community": shown,
        "bot": state["bot"] if ended and not state.get("folded") else [],
        "stage": state["stage"],
        "status": state["status"],
        "result": state["result"],
    }
    if ended and not state.get("folded"):
        data["player_hand"] = HAND_NAMES[best_hand(state["player"] + state["community"])[0]]
        data["bot_hand"] = HAND_NAMES[best_hand(state["bot"] + state["community"])[0]]
    return data
