"""Вход в админку Mini App: одноразовый код + сессия.

Подпись Telegram (initData) уже говорит, кто открыл приложение, — но телефон
могут оставить разблокированным или увести сам аккаунт. Поэтому админке
мало того, что это «тот самый» пользователь: нужен ещё код, который приходит
туда, где его увидит только владелец, — в консоль хостинга или на почту.

Код: 6 цифр, живёт 5 минут, 5 попыток, новый — не чаще раза в минуту.
Храним только хэш кода. После входа выдаётся токен сессии на 12 часов.
Всё в памяти: перезапуск бота просто попросит войти заново.
"""
import hashlib
import hmac
import secrets
import threading
import time

CODE_TTL = 5 * 60
CODE_ATTEMPTS = 5
CODE_COOLDOWN = 60
SESSION_TTL = 12 * 3600


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class AdminGate:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._codes = {}        # uid -> {"hash", "until", "attempts", "sent"}
        self._sessions = {}     # hash(token) -> {"uid", "until"}
        self._lock = threading.Lock()

    def request_code(self, uid):
        """Новый код; None — прошлый запрошен меньше минуты назад."""
        now = self._clock()
        with self._lock:
            old = self._codes.get(uid)
            if old and now - old["sent"] < CODE_COOLDOWN:
                return None
            code = f"{secrets.randbelow(10 ** 6):06d}"
            self._codes[uid] = {"hash": _digest(code), "until": now + CODE_TTL,
                                "attempts": CODE_ATTEMPTS, "sent": now}
            return code

    def verify(self, uid, code):
        """Токен сессии или None. Пять ошибок — код сгорает, нужен новый."""
        now = self._clock()
        code = "".join(ch for ch in str(code or "") if ch.isdigit())
        with self._lock:
            entry = self._codes.get(uid)
            if not entry or now > entry["until"] or entry["attempts"] <= 0:
                self._codes.pop(uid, None)
                return None
            if not hmac.compare_digest(entry["hash"], _digest(code)):
                entry["attempts"] -= 1
                return None
            self._codes.pop(uid, None)
            token = secrets.token_urlsafe(32)
            self._sessions = {k: v for k, v in self._sessions.items() if v["until"] > now}
            self._sessions[_digest(token)] = {"uid": uid, "until": now + SESSION_TTL}
            return token

    def check(self, uid, token):
        if not token:
            return False
        with self._lock:
            session = self._sessions.get(_digest(str(token)))
            return bool(session) and session["uid"] == uid and session["until"] > self._clock()

    def logout(self, token):
        with self._lock:
            self._sessions.pop(_digest(str(token or "")), None)

    def attempts_left(self, uid):
        entry = self._codes.get(uid)
        return entry["attempts"] if entry else 0
