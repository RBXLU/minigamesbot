"""HTTP-сервер Telegram Mini App: отдаёт статику из webapp/ и принимает
результаты игр, подписанные initData самого Telegram."""
import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

STATIC_DIR = Path(__file__).parent / "webapp"
INIT_DATA_MAX_AGE = 24 * 3600


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


def create_app(bot_token, dataset_provider, profile_provider, result_handler, allow_unsigned=False):
    """dataset_provider() -> словари игр, profile_provider(uid) -> профиль,
    result_handler(uid, payload) -> обновлённый профиль."""
    app = Flask(__name__, static_folder=None)

    def _current_user():
        init_data = request.headers.get("X-Telegram-Init-Data") or (request.json or {}).get("initData", "")
        user = verify_init_data(init_data, bot_token)
        if user is None and allow_unsigned:
            return {"id": 0, "first_name": "Гость", "unsigned": True}
        return user

    @app.after_request
    def _headers(response):
        response.headers["X-Frame-Options"] = "ALLOWALL"
        response.headers["Cache-Control"] = "no-store"
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
        if not user:
            return jsonify({"error": "unauthorized"}), 401
        payload = request.json or {}
        try:
            return jsonify(result_handler(user["id"], user, payload))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    return app


def run_webapp(app, host, port, ssl_cert=None, ssl_key=None):
    ssl_context = (ssl_cert, ssl_key) if ssl_cert and ssl_key else None
    # .env читает сам bot.py, повторная загрузка сервером не нужна
    app.run(host=host, port=port, threaded=True, ssl_context=ssl_context, load_dotenv=False)
