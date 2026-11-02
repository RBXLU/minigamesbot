"""HTTP-сервер Telegram Mini App: отдаёт статику из webapp/ и принимает
результаты игр, подписанные initData самого Telegram."""
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

STATIC_DIR = Path(__file__).parent / "webapp"
INIT_DATA_MAX_AGE = 24 * 3600
ACTION_WINDOW = 10        # секунд
ACTION_BURST = 40         # действий за окно: игре хватает с запасом, спаму — нет


def verify_init_data(init_data, bot_token, max_age=INIT_DATA_MAX_AGE):
    """Проверяет подпись Telegram WebApp initData и возвращает данные пользователя."""
    if not init_data or not bot_token:
        return None
    try:
        fields = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None

    received_hash = fields.pop("hash", "")
    if not received_hash:
        return None

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        if max_age and time.time() - int(fields.get("auth_date", 0)) > max_age:
            return None
    except (TypeError, ValueError):
        return None

    try:
        user = json.loads(fields.get("user") or "{}")
    except Exception:
        return None
    return user if user.get("id") else None


def create_app(bot_token, dataset_provider, profile_provider, result_handler, allow_unsigned=False,
               actions=None):
    """dataset_provider() -> словари игр, profile_provider(uid) -> профиль,
    result_handler(uid, payload) -> обновлённый профиль,
    actions: {имя: handler(uid, user, payload)} для /api/action/<имя>."""
    actions = actions or {}
    app = Flask(__name__, static_folder=None)
    # Запросы игры — пара сотен байт; тело в сотни мегабайт положило бы бота вместе с сервером
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    hits = {}
    hits_lock = threading.Lock()

    def _too_fast(uid):
        """Не больше ACTION_BURST действий за ACTION_WINDOW секунд на человека."""
        now = time.monotonic()
        with hits_lock:
            recent = [t for t in hits.get(uid, ()) if now - t < ACTION_WINDOW]
            if len(recent) >= ACTION_BURST:
                hits[uid] = recent
                return True
            recent.append(now)
            hits[uid] = recent
            if len(hits) > 10000:
                for key in [k for k, v in hits.items() if not v or now - v[-1] > ACTION_WINDOW]:
                    hits.pop(key, None)
        return False

    def _current_user():
        init_data = request.headers.get("X-Telegram-Init-Data") or (request.get_json(silent=True) or {}).get("initData", "")
        user = verify_init_data(init_data, bot_token)
        if user is None and allow_unsigned:
            return {"id": 0, "first_name": "Гость", "unsigned": True}
        return user

    @app.after_request
    def _headers(response):
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/<path:filename>")
    def static_files(filename):
        return send_from_directory(STATIC_DIR, filename)

    @app.route("/health")
    def health():
        return "ok"

    @app.route("/api/config")
    def api_config():
        return jsonify(dataset_provider())

    @app.route("/api/profile", methods=["POST"])
    def api_profile():
        user = _current_user()
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        return jsonify(profile_provider(user["id"], user))

    @app.route("/api/report", methods=["POST"])
    def api_report():
        user = _current_user()
        if not user or user.get("unsigned"):
            return jsonify({"error": "unauthorized"}), 401
        if _too_fast(user["id"]):
            return jsonify({"error": "too fast"}), 429
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(result_handler(user["id"], user, payload))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            # Текст исключения наружу не отдаём: в нём могут быть пути, SQL и прочие детали
            app.logger.exception("api_report")
            return jsonify({"error": "server error"}), 500

    @app.route("/api/action/<name>", methods=["POST"])
    def api_action(name):
        handler = actions.get(name)
        if handler is None:
            return jsonify({"error": "unknown action"}), 404
        user = _current_user()
        if not user or user.get("unsigned"):
            # Действия меняют баланс — гостю без подписи Telegram они недоступны.
            return jsonify({"error": "unauthorized"}), 401
        if _too_fast(user["id"]):
            return jsonify({"error": "too fast"}), 429
        try:
            return jsonify(handler(user["id"], user, request.get_json(silent=True) or {}))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    return app


def run_webapp(app, host, port, ssl_cert=None, ssl_key=None):
    ssl_context = (ssl_cert, ssl_key) if ssl_cert and ssl_key else None
    # .env читает сам bot.py, повторная загрузка сервером не нужна
    app.run(host=host, port=port, threaded=True, ssl_context=ssl_context, load_dotenv=False)
