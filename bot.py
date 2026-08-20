import telebot
from telebot import types
import re
from telebot.apihelper import ApiTelegramException
import random
import time
import logging
import traceback
import sys
from itertools import combinations
from threading import Thread
import html
import json
import threading
from datetime import datetime, date, timedelta
import os
import signal
import tarfile
import uuid
import shutil
from pathlib import Path
from openai import OpenAI
from dotenv import load_dotenv
from bussines_bot import register_business_handlers
from core.db import (
    backup_database,
    checkpoint as checkpoint_database,
    database_is_healthy,
    export_state_to_json,
    initialize_storage,
    load_state,
    log_admin_action,
    save_state as save_state_to_db,
)
from room_games import (
    ROOM_VOTE_GAMES,
    cleanup_room_runtime_state,
    configure_room_game_persistence,
    is_room_game,
    remove_room_game_player,
    room_game_start_text,
    room_game_launch,
    register_room_game_handlers,
)
from ui.renderers import (
    build_last_game_instruction,
    render_achievements_text,
    render_main_menu_status,
    render_profile_text,
)
from webapp import create_app as create_webapp, run_webapp

load_dotenv()

LOGS_DIR = Path("logs")
LOGS_ARCHIVE_DIR = LOGS_DIR / "archive"
LATEST_LOG = LOGS_DIR / "latest.log"
ERRORS_LOG = LOGS_DIR / "errors.log"
KEEP_LOG_ARCHIVES = int(os.getenv("KEEP_LOG_ARCHIVES", 20))
_shutdown_event = threading.Event()


def _archive_previous_logs():
    """Складывает логи прошлого запуска в logs/archive/logs_<дата>.tar.xz."""
    LOGS_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stale = [p for p in LOGS_DIR.glob("*.log") if p.is_file() and p.stat().st_size > 0]
    if not stale:
        return None

    started = datetime.fromtimestamp(min(p.stat().st_mtime for p in stale))
    archive_path = LOGS_ARCHIVE_DIR / f"logs_{started:%Y%m%d_%H%M%S}.tar.xz"
    suffix = 1
    while archive_path.exists():
        archive_path = LOGS_ARCHIVE_DIR / f"logs_{started:%Y%m%d_%H%M%S}_{suffix}.tar.xz"
        suffix += 1

    try:
        with tarfile.open(archive_path, "w:xz", preset=6) as tar:
            for path in stale:
                tar.add(path, arcname=path.name)
    except Exception:
        # Логгер ещё не поднят — сообщаем в stderr и не мешаем запуску
        traceback.print_exc()
        return None

    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass

    _prune_log_archives()
    return archive_path


def _prune_log_archives():
    archives = sorted(LOGS_ARCHIVE_DIR.glob("logs_*.tar.xz"), key=lambda p: p.stat().st_mtime)
    for path in archives[:-KEEP_LOG_ARCHIVES] if KEEP_LOG_ARCHIVES > 0 else []:
        try:
            path.unlink()
        except OSError:
            pass


def _setup_logging():
    LOGS_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("telegram_games_bot")
    if logger.handlers:
        return logger

    archived = _archive_previous_logs()

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"
    )

    info_handler = logging.FileHandler(LATEST_LOG, encoding="utf-8")
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)

    error_handler = logging.FileHandler(ERRORS_LOG, encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(info_handler)
    logger.addHandler(error_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    if archived:
        logger.info("Логи прошлого запуска убраны в %s", archived)
    return logger


LOGGER = _setup_logging()


def _require_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value


def _parse_int_id_set(raw_value):
    return {
        int(chunk.strip())
        for chunk in str(raw_value or "").split(",")
        if chunk.strip().isdigit()
    }


def load_quests():
    try:
        with open("quests.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"daily": [], "weekly": []}

QUESTS = load_quests()

LANGUAGES = {
    "uk": "🇺🇦 Українська",
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English"
}

TRANSLATIONS = {
    "uk": {
        "main_menu": "🏠 Головне меню",
        "choose_option": "Виберіть опцію:",
        "back_to_menu": "↩️ Назад до меню",
        "games": "🎮 Ігри",
        "profile": "👤 Профіль",
        "ai": "🤖 AI-асистент",
        "shop": "🛒 Магазин",
        "achievements": "🏆 Досягнення",
        "leaderboard": "📊 Рейтинг",
        "support": "💬 Підтримка",
        "settings": "⚙️ Налаштування",
        "quests": "Квести",
        "create_room": "🚪 Створити кімнату",
        "choose_language": "🌍 Оберіть мову:",
        "language_changed": "✅ Мову змінено!",
        "welcome": "Ласкаво просимо! Оберіть мову для початку:",
        "games_solo": "👤 Одиночні ігри",
        "games_vs_bot": "🤖 Проти бота",
        "games_multi": "👥 Мультиплеєр",
        "games_room": "🚪 Ігри в кімнаті",
        "choose_game_category": "🎮 Виберіть категорію ігор:",
        "invalid_language": "❌ Некоректна мова",
        "unknown_category": "Невідома категорія",
        "support_menu_title": "💬 Меню підтримки",
        "contact_support": "📧 Звернутися до підтримки",
        "faq": "❓ FAQ",
        "settings_title": "⚙️ Налаштування",
        "language_label": "🌍 Мова",
        "notifications": "🔔 Сповіщення",
    },
    "ru": {
        "main_menu": "🏠 Главное меню",
        "choose_option": "Выберите опцию:",
        "back_to_menu": "↩️ Назад в меню",
        "games": "🎮 Игры",
        "profile": "👤 Профиль",
        "ai": "🤖 AI-ассистент",
        "shop": "🛒 Магазин",
        "achievements": "🏆 Достижения",
        "leaderboard": "📊 Рейтинг",
        "support": "💬 Поддержка",
        "settings": "⚙️ Настройки",
        "quests": "Квесты",
        "create_room": "🚪 Создать комнату",
        "choose_language": "🌍 Выберите язык:",
        "language_changed": "✅ Язык изменён!",
        "welcome": "Добро пожаловать! Выберите язык для начала:",
        "games_solo": "👤 Одиночные игры",
        "games_vs_bot": "🤖 Против бота",
        "games_multi": "👥 Мультиплеер",
        "games_room": "🚪 Игры в комнате",
        "choose_game_category": "🎮 Выберите категорию игр:",
        "invalid_language": "❌ Неверный язык",
        "unknown_category": "Неизвестная категория",
        "support_menu_title": "💬 Меню поддержки",
        "contact_support": "📧 Обратиться в поддержку",
        "faq": "❓ FAQ",
        "settings_title": "⚙️ Настройки",
        "language_label": "🌍 Язык",
        "notifications": "🔔 Уведомления",
    },
    "en": {
        "main_menu": "🏠 Main Menu",
        "choose_option": "Choose an option:",
        "back_to_menu": "↩️ Back to Menu",
        "games": "🎮 Games",
        "profile": "👤 Profile",
        "ai": "🤖 AI Assistant",
        "shop": "🛒 Shop",
        "achievements": "🏆 Achievements",
        "leaderboard": "📊 Leaderboard",
        "support": "💬 Support",
        "settings": "⚙️ Settings",
        "quests": "Quests",
        "create_room": "🚪 Create Room",
        "choose_language": "🌍 Choose language:",
        "language_changed": "✅ Language changed!",
        "welcome": "Welcome! Choose your language to start:",
        "games_solo": "👤 Solo Games",
        "games_vs_bot": "🤖 VS Bot",
        "games_multi": "👥 Multiplayer",
        "games_room": "🚪 Room Games",
        "choose_game_category": "🎮 Choose game category:",
        "invalid_language": "❌ Invalid language",
        "unknown_category": "Unknown category",
        "support_menu_title": "💬 Support Menu",
        "contact_support": "📧 Contact Support",
        "faq": "❓ FAQ",
        "settings_title": "⚙️ Settings",
        "language_label": "🌍 Language",
        "notifications": "🔔 Notifications",
    }
}

GAME_TITLES_LANG = {
    "uk": {
        "rps": "Камінь-ножиці-папір", "ttt": "Хрестики-нулики",
        "millionaire": "Мільйонер", "coin": "Орел чи решка",
        "wordle": "Wordle", "bship": "Морський бій",
        "chess": "Шахи", "guess": "Вгадай число",
        "slot": "Казино", "snake": "Змійка",
        "tetris": "Тетріс", "flappy": "Flappy Bird",
        "g2048": "2048", "pong": "Пінг-понг",
        "hangman": "Шибениця", "minesweeper": "Сапер",
        "quizgame": "Вікторина", "combogame": "Комбо-битва",
        "mafia": "Мафія", "wordgame": "Словесна дуель",
        "reaction": "Бліц-реакція", "blackjack": "Блекджек",
        "room_rps": "Камінь-ножиці-папір (чат)",
        "room_duel": "Швидка дуель (чат)",
        "room_bship": "Морський бій (чат)",
        "room_quiz": "Вікторина (чат)",
        "room_combo": "Комбо-битва (чат)",
        "room_mafia": "Мафія (чат)",
    },
    "ru": {
        "rps": "Камень-ножницы-бумага", "ttt": "Крестики-нолики",
        "millionaire": "Миллионер", "coin": "Орел или решка",
        "wordle": "Wordle", "bship": "Морской бой",
        "chess": "Шахматы", "guess": "Угадай число",
        "slot": "Казино", "snake": "Змейка",
        "tetris": "Тетрис", "flappy": "Flappy Bird",
        "g2048": "2048", "pong": "Пинг-понг",
        "hangman": "Виселица", "minesweeper": "Сапер",
        "quizgame": "Викторина", "combogame": "Комбо-битва",
        "mafia": "Мафия", "wordgame": "Словесная дуэль",
        "reaction": "Блиц-реакция", "blackjack": "Блэкджек",
        "room_rps": "Камень-ножницы-бумага (чат)",
        "room_duel": "Быстрая дуэль (чат)",
        "room_bship": "Морской бой (чат)",
        "room_quiz": "Викторина (чат)",
        "room_combo": "Комбо-битва (чат)",
        "room_mafia": "Мафия (чат)",
    },
    "en": {
        "rps": "Rock-Paper-Scissors", "ttt": "Tic-Tac-Toe",
        "millionaire": "Millionaire", "coin": "Coin Flip",
        "wordle": "Wordle", "bship": "Battleship",
        "chess": "Chess", "guess": "Guess the Number",
        "slot": "Casino", "snake": "Snake",
        "tetris": "Tetris", "flappy": "Flappy Bird",
        "g2048": "2048", "pong": "Ping-Pong",
        "hangman": "Hangman", "minesweeper": "Minesweeper",
        "quizgame": "Quiz Game", "combogame": "Combo Battle",
        "mafia": "Mafia", "wordgame": "Word Duel",
        "reaction": "Reaction Game", "blackjack": "Blackjack",
        "room_rps": "Rock-Paper-Scissors (chat)",
        "room_duel": "Quick Duel (chat)",
        "room_bship": "Battleship (chat)",
        "room_quiz": "Quiz (chat)",
        "room_combo": "Combo Battle (chat)",
        "room_mafia": "Mafia (chat)",
    }
}

GAME_DESCRIPTIONS_LANG = {
    "ru": {
        "snake": "Аркада на реакцию: собирайте еду и не врежьтесь в стену.",
        "tetris": "Классический тетрис с падением фигур и очисткой линий.",
        "flappy": "Пролёт между препятствиями в личном чате.",
        "g2048": "Соединяйте плитки и попробуйте собрать 2048.",
        "slot": "Слоты на удачу и быстрый фан.",
        "wordle": "Угадайте слово за несколько попыток.",
        "hangman": "Открывайте буквы и спасайте персонажа.",
        "minesweeper": "Ищите безопасные клетки и избегайте мин.",
        "guess": "Угадайте загаданное число.",
        "rps": "Камень, ножницы, бумага против игрока или бота.",
        "ttt": "Крестики-нолики с быстрыми партиями.",
        "blackjack": "Карточная игра до 21 очка.",
        "chess": "Шахматы с пошаговыми ходами.",
        "bship": "Морской бой в приватном формате.",
        "wordgame": "Словесная дуэль на скорость.",
        "combogame": "Комбо-битва с выбором действий.",
        "quizgame": "Викторина с вопросами и очками.",
        "room_rps": "Запуск КНБ прямо в комнате чата.",
        "room_duel": "Быстрая дуэль на реакцию для участников комнаты.",
        "room_bship": "Комнатный морской бой.",
        "room_quiz": "Общая викторина для комнаты.",
        "room_combo": "Комнатная комбо-битва.",
        "room_mafia": "Мафия для компании в комнате.",
        "coin": "Подбросьте монетку: орёл или решка.",
        "mafia": "Ночь, день и голосование в компании 4-10 игроков.",
        "millionaire": "Отвечайте на вопросы и проверьте эрудицию.",
        "pong": "Настольный пинг-понг на двоих.",
        "reaction": "Проверьте скорость реакции на сигнал.",
    },
    "uk": {
        "snake": "Аркада на реакцію: збирайте їжу й не вріжтесь у стіну.",
        "tetris": "Класичний тетріс із падінням фігур та очищенням ліній.",
        "flappy": "Політ між перешкодами в особистому чаті.",
        "g2048": "Поєднуйте плитки та спробуйте зібрати 2048.",
        "slot": "Слоти на удачу та швидкий фан.",
        "wordle": "Вгадайте слово за кілька спроб.",
        "hangman": "Відкривайте літери й рятуйте персонажа.",
        "minesweeper": "Шукайте безпечні клітинки та уникайте мін.",
        "guess": "Вгадайте задумане число.",
        "rps": "Камінь, ножиці, папір проти гравця або бота.",
        "ttt": "Хрестики-нулики з швидкими партіями.",
        "blackjack": "Карткова гра до 21 очка.",
        "chess": "Шахи з покроковими ходами.",
        "bship": "Морський бій у приватному форматі.",
        "wordgame": "Словесна дуель на швидкість.",
        "combogame": "Комбо-битва з вибором дій.",
        "quizgame": "Вікторина з питаннями та очками.",
        "room_rps": "Запуск КНП прямо в кімнаті чату.",
        "room_duel": "Швидка дуель на реакцію для учасників кімнати.",
        "room_bship": "Кімнатний морський бій.",
        "room_quiz": "Спільна вікторина для кімнати.",
        "room_combo": "Кімнатна комбо-битва.",
        "room_mafia": "Мафія для компанії в кімнаті.",
        "coin": "Підкиньте монетку: орел чи решка.",
        "mafia": "Ніч, день і голосування у компанії 4-10 гравців.",
        "millionaire": "Відповідайте на питання та перевірте ерудицію.",
        "pong": "Настільний пінг-понг на двох.",
        "reaction": "Перевірте швидкість реакції на сигнал.",
    },
    "en": {
        "snake": "Reaction arcade: collect food and avoid the walls.",
        "tetris": "Classic falling-block puzzle with line clears.",
        "flappy": "Fly through obstacles in private chat.",
        "g2048": "Merge tiles and try to reach 2048.",
        "slot": "Quick slot-machine fun.",
        "wordle": "Guess the hidden word in a few tries.",
        "hangman": "Open letters and avoid losing the round.",
        "minesweeper": "Reveal safe cells and avoid mines.",
        "guess": "Guess the hidden number.",
        "rps": "Rock-paper-scissors versus a player or bot.",
        "ttt": "Fast tic-tac-toe matches.",
        "blackjack": "Card game up to 21 points.",
        "chess": "Turn-based chess matches.",
        "bship": "Battleship in private format.",
        "wordgame": "Fast word duel.",
        "combogame": "Combo battle with move choices.",
        "quizgame": "Quiz with questions and points.",
        "room_rps": "Play RPS right inside a room chat.",
        "room_duel": "Quick reaction duel for room members.",
        "room_bship": "Room battleship mode.",
        "room_quiz": "Shared quiz for the whole room.",
        "room_combo": "Room combo battle.",
        "room_mafia": "Mafia party mode for a room.",
        "coin": "Flip a coin: heads or tails.",
        "mafia": "Night, day and voting for 4-10 players.",
        "millionaire": "Answer questions and test your knowledge.",
        "pong": "Table ping-pong for two players.",
        "reaction": "Test how fast you react to the signal.",
    },
}

WHATS_NEW_ITEMS = {
    "ru": [
        "Сообщения бота теперь с анимированными эмодзи.",
        "Убраны служебные ссылки, которые показывались вместо значков в меню и на кнопках.",
        "Инлайн-режим переведён: игры открываются на языке из настроек.",
        "AI-ассистент переехал на нового провайдера и отвечает стабильнее.",
        "Бот перешёл на вежливое обращение.",
    ],
    "en": [
        "Bot messages now use animated emoji.",
        "Removed the service links that showed up instead of icons in menus and on buttons.",
        "Inline mode is translated: games open in the language from your settings.",
        "The AI assistant moved to a new provider and replies more reliably.",
        "Every game now has a description in all supported languages.",
    ],
    "uk": [
        "Повідомлення бота тепер з анімованими емодзі.",
        "Прибрано службові посилання, що показувалися замість значків у меню та на кнопках.",
        "Inline-режим перекладено: ігри відкриваються мовою з налаштувань.",
        "AI-асистент переїхав до нового провайдера й відповідає стабільніше.",
        "Бот перейшов на ввічливе звертання.",
    ],
}

def get_user_language(user_id):
    user = load_data().get("users", {}).get(str(user_id), {})
    return user.get("language", "ru")

def set_user_language(user_id, lang):
    data = load_data()
    data.setdefault("users", {}).setdefault(str(user_id), {})["language"] = lang
    save_data(data)

def t(user_id, key):
    lang = get_user_language(user_id)
    return TRANSLATIONS.get(lang, TRANSLATIONS["ru"]).get(key, key)


def tr_all(key):
    return [TRANSLATIONS.get(lang, {}).get(key) for lang in LANGUAGES if TRANSLATIONS.get(lang, {}).get(key)]


def text_matches_key(text, key):
    return text in tr_all(key)

def get_game_title(user_id, game_key):
    lang = get_user_language(user_id)
    return GAME_TITLES_LANG.get(lang, GAME_TITLES_LANG["ru"]).get(game_key, game_key)


def localized_text(user_id, ru_text, en_text=None, uk_text=None):
    lang = get_user_language(user_id)
    variants = {
        "ru": ru_text,
        "en": en_text or ru_text,
        "uk": uk_text or ru_text,
    }
    return variants.get(lang, ru_text)


def get_game_description(user_id, game_key):
    lang = get_user_language(user_id)
    return GAME_DESCRIPTIONS_LANG.get(lang, GAME_DESCRIPTIONS_LANG["ru"]).get(
        game_key,
        localized_text(user_id, "Описание скоро появится.", "Description coming soon.", "Опис з'явиться незабаром."),
    )


TOKEN = _require_env("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TOKEN)
try:
    INLINE_BOT_USERNAME = bot.get_me().username or "minigamesisbot"
    bot.delete_webhook()
except ApiTelegramException as e:
    if getattr(e, "error_code", None) == 401:
        raise SystemExit(
            "TELEGRAM_TOKEN отклонён Telegram (401 Unauthorized).\n"
            "Токен отозван или скопирован с ошибкой — возьмите новый у @BotFather "
            "и пропишите его в .env."
        )
    raise
except Exception:
    INLINE_BOT_USERNAME = "minigamesisbot"
    LOGGER.exception("Не удалось получить имя бота при старте")
register_room_game_handlers(bot)

NEWS_EMOJI_IDS = {
    "✅ ": "5206607081334906820",
    "❌ ": "5210952531676504517",
    "⚠️": "5447644880824181073",
    "⚠": "5447644880824181073",
    "❗": "5274099962655816924",
    "❓ ": "5436113877181941026",
    "📊 ": "5231200819986047254",
    "📢": "5424818078833715060",
    "💬 ": "5443038326535759644",
    "💭": "5467538555158943525",
    "💰": "5409048419211682843",
    "💸": "5233326571099534068",
    "⭐": "5438496463044752972",
    "✨": "5325547803936572038",
    "😀": "5372954454653933911",
    "😅": "5373015670822804395",
    "😂": "5370953476635368811",
    "😇": "5370947515220761242",
    "👋": "5337080053119336309",
    "🎉": "5461151367559141950",
    "🎭": "5361741454685256344",
    "🎮 ": "5361741454685256344",
    "🚪 ": "5361741454685256344",
    "📚": "5406756500108501710",
    "📌": "5397782960512444700",
    "📍": "5391032818111363540",
    "📈": "5244837092042750681",
    "📉": "5246762912428603768",
    "🛡": "5251203410396458957",
    "🔊": "5388632425314140043",
    "🔄": "5375338737028841420",
    "🔁": "5375338737028841420",
    "🔒": "5296369303661067030",
    "🔍": "5231012545799666522",
    "🔔 ": "5458603043203327669",
    "⛔": "5260293700088511294",
    "🧹": "5445267414562389170",
    "✍️": "5395444784611480792",
    "✍": "5395444784611480792",
    "👤 ": "5449683594425410231",
    "🏪": "5406683434124859552",
    "🏠": "5416041192905265756",
    "🎰": "5361741454685256344",
    "⏳": "5386367538735104399",
    "⚙️ ": "5341715473882955310",
    "⚙": "5341715473882955310",
    "📣": "5460795800101594035",
    "ℹ️": "5334544901428229844",
    "ℹ": "5334544901428229844",
    "🔥": "5424972470023104089",
    "💥": "5276032951342088188",
    "🙂": "5371073319107827779",
    "😉": "5373101475679443553",
    "😍": "5372886001465170842",
    "😎": "5373141891321699086",
    "🤩": "5373026167722876724",
    "🥳": "5370870691140737817",
    "😏": "5370976574969486150",
    "📝": "5395444784611480792",
    "🪙": "5402186569006210455",
    "🛒 ": "5229064374403998351",
    "🏆 ": "5440539497383087970",
    "🤖 ": "5269531045165816230",
    "🏠 ": "5222444124698853913",
    "📧": "5253742260054409879",
    "🌍 ": "5447410659077661506",
    "↩️ ": "5416117059207572332",
    "↩": "5416117059207572332",
    "💀": "5370971163310693562",
    "✅": "5206607081334906820",
    "❌": "5210952531676504517",
    "🛍": "5229064374403998351",
    "💎": "5438496463044752972",
    "🎯": "5467538555158943525",
    "🏁": "5440539497383087970",
    "🚪": "5416117059207572332",
}


def _load_emoji_pack(path="emoji_pack.json"):
    """Анимированные эмодзи из набора t.me/addemoji/RestrictedEmoji.

    Ручные значения в NEWS_EMOJI_IDS приоритетнее: там часть эмодзи намеренно
    указывает на другой стикер.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            pack = json.load(f)
    except FileNotFoundError:
        return 0
    except Exception:
        LOGGER.exception("Не удалось прочитать %s", path)
        return 0

    added = 0
    for symbol, emoji_id in pack.items():
        if isinstance(symbol, str) and isinstance(emoji_id, str) and symbol not in NEWS_EMOJI_IDS:
            NEWS_EMOJI_IDS[symbol] = emoji_id
            added += 1
    return added


_EMOJI_PACK_ADDED = _load_emoji_pack()
LOGGER.info("Набор анимированных эмодзи: +%s к %s ручным",
            _EMOJI_PACK_ADDED, len(NEWS_EMOJI_IDS) - _EMOJI_PACK_ADDED)

NEWS_EMOJI_ENABLED = os.getenv("NEWS_EMOJI_ENABLED", "1") != "0"
_news_emoji_active = NEWS_EMOJI_ENABLED

# У Telegram есть предел на число entity в сообщении, а игровые поля содержат десятки эмодзи.
NEWS_EMOJI_MAX_PER_MESSAGE = int(os.getenv("NEWS_EMOJI_MAX_PER_MESSAGE", "50"))

# Внутри <tg-emoji> Telegram требует ровно один эмодзи, а не ссылку, поэтому для ключей
# tg://emoji?id=... подбираем эмодзи с тем же emoji-id; если такого нет — ссылку убираем.
_NEWS_EMOJI_LINK_PREFIX = "tg://emoji?id="
_NEWS_EMOJI_FALLBACK_CHARS = {}
for _symbol, _emoji_id in NEWS_EMOJI_IDS.items():
    if not _symbol.startswith(_NEWS_EMOJI_LINK_PREFIX):
        _NEWS_EMOJI_FALLBACK_CHARS.setdefault(_emoji_id, _symbol)

# В текстах эмодзи встречается и голым, и с селектором начертания ("🛡" и "🛡️").
# Без пары для второй формы паттерн отрезал бы базовый символ, оставив VS16 снаружи.
_VARIATION_SELECTOR_16 = "\ufe0f"
for _symbol, _emoji_id in list(NEWS_EMOJI_IDS.items()):
    if len(_symbol) == 1 and _symbol + _VARIATION_SELECTOR_16 not in NEWS_EMOJI_IDS:
        NEWS_EMOJI_IDS[_symbol + _VARIATION_SELECTOR_16] = _emoji_id


def _news_emoji_pattern_part(symbol):
    part = re.escape(symbol)
    if symbol.startswith(_NEWS_EMOJI_LINK_PREFIX):
        # Ссылка идёт как "tg://emoji?id=... Текст"; если замены нет, разделяющий
        # пробел тоже лишний.
        part += " ?"
    return part


# Один проход вместо цепочки str.replace(), которая вкладывала теги друг в друга.
# Границы обязательны: если совпадение — лишь часть составного эмодзи (тон кожи,
# ZWJ-связка, кейкап, VS16), Telegram отвечает ENTITY_TEXT_INVALID.
_EMOJI_CONTINUATION = "\ufe0f\ufe0e\u200d\u20e3\U0001f3fb-\U0001f3ff"
_NEWS_EMOJI_PATTERN = re.compile(
    "(?<!\u200d)(?:"
    + "|".join(_news_emoji_pattern_part(symbol) for symbol in sorted(NEWS_EMOJI_IDS, key=len, reverse=True))
    + f")(?![{_EMOJI_CONTINUATION}])"
)


def _news_emoji_tag(symbol: str) -> str:
    emoji_id = NEWS_EMOJI_IDS.get(symbol)
    if not emoji_id:
        return symbol
    label = symbol
    if symbol.startswith(_NEWS_EMOJI_LINK_PREFIX):
        label = _NEWS_EMOJI_FALLBACK_CHARS.get(emoji_id)
        if not label:
            return ""
    return f'<tg-emoji emoji-id="{emoji_id}">{label}</tg-emoji>'


def _news_emoji_substitute(match):
    token = match.group(0)
    symbol, trailing = token, ""
    if symbol not in NEWS_EMOJI_IDS and symbol.endswith(" "):
        symbol, trailing = symbol[:-1], " "
    tag = _news_emoji_tag(symbol)
    if not tag:
        return ""
    return tag + trailing


def apply_news_emoji(text):
    if not isinstance(text, str) or not _news_emoji_active:
        return text, False

    matches = _NEWS_EMOJI_PATTERN.findall(text)
    if not matches:
        return text, False
    if len(matches) > NEWS_EMOJI_MAX_PER_MESSAGE:
        return text, False

    updated = _NEWS_EMOJI_PATTERN.sub(_news_emoji_substitute, text)
    return updated, updated != text


def _prepare_outgoing_text(text, kwargs):
    parse_mode = kwargs.get("parse_mode")
    if parse_mode not in (None, "HTML"):
        return text, kwargs

    updated_text, changed = apply_news_emoji(text)
    if changed and parse_mode in (None, "HTML"):
        kwargs["parse_mode"] = "HTML"
    return updated_text, kwargs


def _prepare_inline_message_content(content, rollback):
    if not isinstance(content, types.InputTextMessageContent):
        return content

    parse_mode = getattr(content, "parse_mode", None)
    if parse_mode not in (None, "HTML"):
        return content

    updated_text, changed = apply_news_emoji(content.message_text)
    if not changed:
        return content

    rollback.append((content, "message_text", content.message_text))
    rollback.append((content, "parse_mode", parse_mode))
    content.message_text = updated_text
    content.parse_mode = "HTML"
    return content


def _prepare_inline_result(result, rollback):
    if hasattr(result, "caption"):
        updated_value, changed = apply_news_emoji(result.caption)
        if changed:
            rollback.append((result, "caption", result.caption))
            result.caption = updated_value

    if hasattr(result, "input_message_content"):
        result.input_message_content = _prepare_inline_message_content(
            result.input_message_content, rollback
        )
    return result


_original_send_message = bot.send_message
_original_reply_to = bot.reply_to
_original_edit_message_text = bot.edit_message_text
_original_answer_inline_query = bot.answer_inline_query

# Отказ по кастомным эмодзи (400 ENTITY_TEXT_INVALID / CUSTOM_EMOJI_INVALID) раньше
# убивал отправку целиком, и бот молчал на любую команду.
_CUSTOM_EMOJI_REJECTIONS = (
    "entity_text_invalid",
    "custom_emoji_invalid",
    "custom emoji",
)


def _is_custom_emoji_rejection(exc):
    if getattr(exc, "error_code", None) != 400:
        return False
    description = (getattr(exc, "description", "") or str(exc)).lower()
    return any(marker in description for marker in _CUSTOM_EMOJI_REJECTIONS)


# Одно неудачное сообщение уходит обычным текстом, подстановка остаётся. Гасим её
# целиком только при череде отказов подряд — это уже системная проблема.
NEWS_EMOJI_FAILURE_STREAK = int(os.getenv("NEWS_EMOJI_FAILURE_STREAK", "10"))
_news_emoji_failures = 0
_news_emoji_lock = threading.Lock()


def _note_news_emoji_success():
    global _news_emoji_failures
    if _news_emoji_failures:
        with _news_emoji_lock:
            _news_emoji_failures = 0


def _disable_news_emoji(exc, text=None):
    global _news_emoji_failures, _news_emoji_active
    with _news_emoji_lock:
        _news_emoji_failures += 1
        streak = _news_emoji_failures
        exhausted = _news_emoji_active and streak >= NEWS_EMOJI_FAILURE_STREAK
        if exhausted:
            _news_emoji_active = False

    LOGGER.warning(
        "Telegram отклонил кастомные эмодзи (%s), отказ %s подряд; текст: %.120r",
        getattr(exc, "description", "") or exc,
        streak,
        text or "",
    )
    if exhausted:
        LOGGER.error(
            "Кастомные эмодзи отклонены %s раз подряд — подстановка tg-emoji отключена "
            "до перезапуска, сообщения уходят обычным текстом",
            streak,
        )


def send_message_with_news_emoji(chat_id, text, *args, **kwargs):
    prepared, prepared_kwargs = _prepare_outgoing_text(text, dict(kwargs))
    try:
        result = _original_send_message(chat_id, prepared, *args, **prepared_kwargs)
    except ApiTelegramException as e:
        if prepared == text or not _is_custom_emoji_rejection(e):
            raise
        _disable_news_emoji(e, prepared)
        return _original_send_message(chat_id, text, *args, **prepared_kwargs)
    if prepared != text:
        _note_news_emoji_success()
    return result


def reply_to_with_news_emoji(message, text, *args, **kwargs):
    prepared, prepared_kwargs = _prepare_outgoing_text(text, dict(kwargs))

    def _send(body, call_kwargs):
        try:
            return _original_reply_to(message, body, *args, **call_kwargs)
        except ApiTelegramException as e:
            description = getattr(e, "description", "") or str(e)
            if "message to be replied not found" in description.lower():
                call_kwargs.pop("reply_parameters", None)
                return _original_send_message(message.chat.id, body, *args, **call_kwargs)
            raise

    try:
        result = _send(prepared, prepared_kwargs)
    except ApiTelegramException as e:
        if prepared == text or not _is_custom_emoji_rejection(e):
            raise
        _disable_news_emoji(e, prepared)
        return _send(text, prepared_kwargs)
    if prepared != text:
        _note_news_emoji_success()
    return result


def edit_message_text_with_news_emoji(text, chat_id=None, message_id=None, *args, **kwargs):
    prepared, prepared_kwargs = _prepare_outgoing_text(text, dict(kwargs))
    try:
        result = _original_edit_message_text(
            prepared, chat_id=chat_id, message_id=message_id, *args, **prepared_kwargs
        )
    except ApiTelegramException as e:
        if prepared == text or not _is_custom_emoji_rejection(e):
            raise
        _disable_news_emoji(e, prepared)
        return _original_edit_message_text(
            text, chat_id=chat_id, message_id=message_id, *args, **prepared_kwargs
        )
    if prepared != text:
        _note_news_emoji_success()
    return result


def answer_inline_query_with_news_emoji(inline_query_id, results=None, *args, **kwargs):
    rollback = []
    prepared = [_prepare_inline_result(result, rollback) for result in (results or [])]
    try:
        result = _original_answer_inline_query(inline_query_id, prepared, *args, **kwargs)
    except ApiTelegramException as e:
        if not rollback or not _is_custom_emoji_rejection(e):
            raise
        _disable_news_emoji(e)
        for target, attribute, original in reversed(rollback):
            setattr(target, attribute, original)
        return _original_answer_inline_query(inline_query_id, prepared, *args, **kwargs)
    if rollback:
        _note_news_emoji_success()
    return result


bot.send_message = send_message_with_news_emoji
bot.reply_to = reply_to_with_news_emoji
bot.edit_message_text = edit_message_text_with_news_emoji
bot.answer_inline_query = answer_inline_query_with_news_emoji

NVMAPI_KEY = os.getenv("NVMAPI_KEY", "").strip()
NVMAPI_BASE_URL = os.getenv("NVMAPI_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()
NVMAPI_MODEL = os.getenv("NVMAPI_MODEL", "meta/llama-3.1-8b-instruct").strip()
NVMAPI_TIMEOUT = float(os.getenv("NVMAPI_TIMEOUT", "30"))
nvmapi_client = (
    OpenAI(api_key=NVMAPI_KEY, base_url=NVMAPI_BASE_URL, timeout=NVMAPI_TIMEOUT)
    if NVMAPI_KEY
    else None
)

FREE_DAILY_QUOTA = int(os.getenv("FREE_DAILY_QUOTA", 10))

DATA_FILE = "bot_data.json"
DB_FILE = os.getenv("BOT_DB_PATH", "bot_data.sqlite3")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@minigamesbottgk")

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://slu1.heavencloud.in:2673").rstrip("/")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", 2673))
WEBAPP_SSL_CERT = os.getenv("WEBAPP_SSL_CERT", "").strip()
WEBAPP_SSL_KEY = os.getenv("WEBAPP_SSL_KEY", "").strip()
# Открыть Mini App в обычном браузере (без подписи Telegram) — только для отладки
WEBAPP_ALLOW_UNSIGNED = os.getenv("WEBAPP_ALLOW_UNSIGNED", "0") == "1"

# Игры, полностью перенесённые в Mini App: (ключ, название, эмодзи, категория)
WEBAPP_GAMES = [
    ("g2048", "2048", "🔢", "solo"),
    ("tetris", "Тетрис", "🧱", "solo"),
    ("snake", "Змейка", "🐍", "solo"),
    ("flappy", "Flappy Bird", "🐦", "solo"),
    ("wordle", "Wordle", "🟩", "solo"),
    ("hangman", "Виселица", "🔤", "solo"),
    ("minesweeper", "Сапёр", "💣", "solo"),
    ("guess", "Угадай число", "🎯", "solo"),
    ("reaction", "Блиц-реакция", "⚡", "solo"),
    ("slot", "Казино", "🎰", "luck"),
    ("coin", "Орёл и решка", "🪙", "luck"),
    ("blackjack", "Блэкджек", "🃏", "cards"),
    ("poker", "Покер", "♠️", "cards"),
    ("rps", "Камень-ножницы", "✂️", "vs_bot"),
    ("ttt", "Крестики-нолики", "❌", "vs_bot"),
    ("pong", "Пинг-понг", "🏓", "vs_bot"),
    ("combogame", "Комбо-битва", "⚔️", "vs_bot"),
    ("quizgame", "Викторина", "🧠", "quiz"),
    ("millionaire", "Миллионер", "💰", "quiz"),
]
WEBAPP_GAME_KEYS = {key for key, _, _, _ in WEBAPP_GAMES}
WEBAPP_BET_GAMES = {"poker", "blackjack", "slot"}

# Игры, которым нужен чат Telegram: из Mini App ведём обратно в бота
WEBAPP_CHAT_ONLY_GAMES = [
    ("bship", "Морской бой", "🚢", "морской бой"),
    ("chess", "Шахматы", "♟", "шахматы"),
    ("mafia", "Мафия", "🎭", "мафия"),
    ("wordgame", "Словесная дуэль", "📝", "слова"),
    ("iduel", "Дуэль КНБ", "🤜", ""),
]
SUPPORT_ADMIN_IDS_RAW = os.getenv("SUPPORT_ADMIN_IDS", "5782683757")
SUPPORT_ADMIN_IDS = _parse_int_id_set(SUPPORT_ADMIN_IDS_RAW)
BOT_ADMIN_IDS_RAW = os.getenv("BOT_ADMIN_IDS", SUPPORT_ADMIN_IDS_RAW)
BOT_ADMIN_IDS = _parse_int_id_set(BOT_ADMIN_IDS_RAW) | SUPPORT_ADMIN_IDS

AI_MODES = {
    "chat": "Обычный дружелюбный помощник",
    "short": "Отвечай максимально кратко, 1–2 предложения",
    "long": "Отвечай подробно и развернуто",
    "code": "Ты опытный программист, пиши код и объясняй"
}


def _is_bot_admin(user_id):
    return int(user_id) in BOT_ADMIN_IDS


def _is_support_admin(uid):
    return int(uid) in SUPPORT_ADMIN_IDS or _is_bot_admin(uid)


def _send_admin_alert(text):
    if not BOT_ADMIN_IDS:
        return
    for admin_id in BOT_ADMIN_IDS:
        try:
            bot.send_message(admin_id, text[:4000], parse_mode="HTML")
        except Exception:
            LOGGER.exception("Failed to send admin alert to %s", admin_id)


def log_exception(context, exc, user_id=None, chat_id=None, notify_admin=False):
    details = [f"context={context}"]
    if user_id is not None:
        details.append(f"user_id={user_id}")
    if chat_id is not None:
        details.append(f"chat_id={chat_id}")
    detail_text = " | ".join(details)
    LOGGER.error("%s\n%s", detail_text, traceback.format_exc())
    if notify_admin:
        _send_admin_alert(
            "⚠️ <b>Критическая ошибка</b>\n"
            f"<code>{html.escape(detail_text)}</code>\n\n"
            f"<code>{html.escape(str(exc))[:2500]}</code>"
        )


def _threading_excepthook(args):
    log_exception(
        f"thread:{getattr(args.thread, 'name', 'unknown')}",
        args.exc_value,
        notify_admin=True,
    )


def _sys_excepthook(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        return
    LOGGER.error(
        "uncaught exception\n%s",
        "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
    )
    _send_admin_alert(
        "⚠️ <b>Необработанная ошибка процесса</b>\n"
        f"<code>{html.escape(str(exc_value))[:2500]}</code>"
    )


threading.excepthook = _threading_excepthook
sys.excepthook = _sys_excepthook

BACKUP_FOLDER = os.getenv("BACKUP_FOLDER", "backups")
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", 24))
KEEP_BACKUPS_DAYS = int(os.getenv("KEEP_BACKUPS_DAYS", 30))
JSON_FILES_TO_BACKUP = ["bot_data.json", "quests.json", "lang.json"]


_DB_RECOVERY_NOTE = initialize_storage(DB_FILE, DATA_FILE, BACKUP_FOLDER)
if _DB_RECOVERY_NOTE:
    LOGGER.error("Восстановление БД при старте: %s", _DB_RECOVERY_NOTE)

BROADCAST_SETTINGS = {
    "msg": "",
    "btn_text": "Открыть",
    "btn_type": "link",
    "btn_link": "https://t.me/minigamesbottgk"
}
ROOM_FREE_TITLE = "Свободно"
ROOM_TTL_SECONDS = 3600
ROOM_CODE_LEN = 5
ROOM_VOTE_SECONDS = 60
ROOM_MESSAGE_BUFFER = 0
USER_COOLDOWN_SECONDS = float(os.getenv("USER_COOLDOWN_SECONDS", "0.8"))
ROOM_IDLE_TIMEOUT_SECONDS = int(os.getenv("ROOM_IDLE_TIMEOUT_SECONDS", "900"))
_user_cooldowns = {}

def load_data():
    try:
        data = load_state(DB_FILE)
    except Exception as e:
        log_exception("load_data", e, notify_admin=True)
        data = {"users": {}}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("users", {})
    data.setdefault("global_game_stats", {})
    data.setdefault("premium", {})
    data.setdefault("rooms", {"pool": [], "active": {}, "free_title": ROOM_FREE_TITLE})
    return data

def save_data(data):
    save_state_to_db(data, DB_FILE)


def _is_user_banned(user_id):
    rec = load_data().get("users", {}).get(str(user_id), {})
    return bool(rec.get("is_banned")), str(rec.get("ban_reason") or "без причины")


def _deny_if_banned(user_id, chat_id=None, call_id=None):
    banned, reason = _is_user_banned(user_id)
    if not banned:
        return False
    text = f"⛔ Доступ к боту ограничен.\nПричина: {reason}"
    try:
        if call_id:
            bot.answer_callback_query(call_id, text, show_alert=True)
        elif chat_id:
            bot.send_message(chat_id, text)
    except Exception:
        pass
    return True


def _cooldown_allows(user_id, action="global"):
    key = (int(user_id), str(action))
    now_ts = time.monotonic()
    last_ts = _user_cooldowns.get(key, 0)
    if now_ts - last_ts < USER_COOLDOWN_SECONDS:
        return False
    _user_cooldowns[key] = now_ts
    if len(_user_cooldowns) > 5000:
        cutoff = now_ts - 60
        for old_key, old_ts in list(_user_cooldowns.items()):
            if old_ts < cutoff:
                _user_cooldowns.pop(old_key, None)
    return True


def _deny_if_spam(user_id, chat_id=None, call_id=None, action="global"):
    if _is_bot_admin(user_id) or _cooldown_allows(user_id, action):
        return False
    try:
        if call_id:
            bot.answer_callback_query(call_id, "⏳ Слишком быстро. Попробуйте через секунду.")
        elif chat_id:
            bot.send_message(chat_id, "⏳ Слишком быстро. Попробуйте через секунду.")
    except Exception:
        pass
    return True


def _subscription_keyboard():
    kb = types.InlineKeyboardMarkup()
    url = _channel_url() or "https://t.me/"
    kb.add(types.InlineKeyboardButton("📣 Подписаться", url=url))
    kb.add(types.InlineKeyboardButton("✅ Проверить", callback_data="check_subscription"))
    return kb


def _deny_if_not_subscribed(user_id, chat_id=None, call_id=None):
    if not REQUIRED_CHANNEL or _is_bot_admin(user_id) or is_user_subscribed(user_id):
        return False
    text = "⚠️ Подпишитесь на канал, чтобы использовать бота."
    try:
        if call_id:
            bot.answer_callback_query(call_id, "Нужна подписка на канал", show_alert=True)
        if chat_id:
            bot.send_message(chat_id, text, reply_markup=_subscription_keyboard())
    except Exception:
        pass
    return True


def _guard_user(user_id, chat_id=None, call_id=None, action="global", require_subscription=True):
    if _deny_if_banned(user_id, chat_id=chat_id, call_id=call_id):
        return False
    if _deny_if_spam(user_id, chat_id=chat_id, call_id=call_id, action=action):
        return False
    if require_subscription and _deny_if_not_subscribed(user_id, chat_id=chat_id, call_id=call_id):
        return False
    return True


def _set_user_ban(admin_id, target_user_id, is_banned, reason=""):
    data = load_data()
    rec = data.setdefault("users", {}).setdefault(str(target_user_id), {})
    rec["is_banned"] = bool(is_banned)
    rec["ban_reason"] = str(reason or "").strip() if is_banned else ""
    data["users"][str(target_user_id)] = rec
    save_data(data)
    log_admin_action(
        admin_id,
        "ban_user" if is_banned else "unban_user",
        target_user_id=target_user_id,
        details={"reason": rec.get("ban_reason", "")},
        db_path=DB_FILE,
    )


def _persist_room_runtime_state(code, runtime_state):
    d, rooms = _rooms_get_data()
    room = rooms.get("active", {}).get(code)
    if not isinstance(room, dict):
        return
    room["runtime_state"] = runtime_state or {}
    room["last_activity_at"] = time.time()
    rooms["active"][code] = room
    save_data(d)


configure_room_game_persistence(_persist_room_runtime_state)


def backup_json_files():
    try:
        Path(BACKUP_FOLDER).mkdir(exist_ok=True)
        export_state_to_json(DATA_FILE, DB_FILE)
        db_backup_path = backup_database(DB_FILE, BACKUP_FOLDER)
        checkpoint_database(DB_FILE)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        for file_path in JSON_FILES_TO_BACKUP:
            if os.path.exists(file_path):
                suffix = Path(file_path).suffix or ".bak"
                shutil.copy2(file_path, f"{BACKUP_FOLDER}/{Path(file_path).stem}_{timestamp}{suffix}")

        cleanup_old_backups()
        LOGGER.info("[BACKUP] Backup completed at %s (%s)", datetime.now(), db_backup_path)
        return db_backup_path
    except Exception as e:
        log_exception("backup_json_files", e)
        return None


def cleanup_old_backups():
    try:
        backup_path = Path(BACKUP_FOLDER)
        if not backup_path.exists():
            return
        cutoff_time = time.time() - KEEP_BACKUPS_DAYS * 24 * 3600
        for backup_file in list(backup_path.glob("*.json")) + list(backup_path.glob("*.sqlite3")):
            if backup_file.stat().st_mtime < cutoff_time:
                backup_file.unlink()
                LOGGER.info("[BACKUP] Deleted old backup: %s", backup_file.name)
    except Exception as e:
        log_exception("cleanup_old_backups", e)


def start_backup_scheduler():
    def backup_loop():
        while not _shutdown_event.wait(BACKUP_INTERVAL_HOURS * 3600):
            backup_json_files()

    Thread(target=backup_loop, daemon=True).start()
    LOGGER.info("[BACKUP] Scheduler started (interval: %sh)", BACKUP_INTERVAL_HOURS)


try:
    _stored_broadcast = load_data().get("broadcast")
    if _stored_broadcast:
        BROADCAST_SETTINGS.update(_stored_broadcast)
except Exception:
    pass

start_backup_scheduler()

def update_user_streak(user_id, display_name=None):
    d = load_data()
    users = d.setdefault("users", {})
    rec = users.setdefault(str(user_id), {})

    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_day = rec.get("streak_last_day")
    cur = int(rec.get("streak_current", 0) or 0)

    if last_day == yesterday:
        cur = cur + 1 if cur > 0 else 1
    elif last_day != today:
        cur = 1

    rec["streak_current"] = cur
    rec["streak_last_day"] = today
    rec["streak_best"] = max(int(rec.get("streak_best", 0) or 0), cur)
    if display_name:
        rec["display_name"] = str(display_name)[:64]
    users[str(user_id)] = rec
    save_data(d)
    _check_achievements(user_id, rec)
    return cur

def get_user(uid):
    data = load_data()
    users = data["users"]
    today = date.today().isoformat()

    if not isinstance(users.get(str(uid)), dict):
        users[str(uid)] = {}

    user = users[str(uid)]
    user.setdefault("premium_until", 0)
    if not isinstance(user.get("pending"), dict):
        user["pending"] = {}
    if user.get("date") != today:
        user["date"] = today
        user["count"] = 0
    user.setdefault("count", 0)

    save_data(data)
    return user

GAME_TITLES = {**GAME_TITLES_LANG["ru"], "poker": "Покер"}

SHOP_ITEMS = {
    "avatar_fire": {"name": "Аватар: Огонь", "type": "avatar", "value": "🔥", "price": 40},
    "avatar_star": {"name": "Аватар: Звезда", "type": "avatar", "value": "⭐", "price": 40},
    "avatar_robot": {"name": "Аватар: Робот", "type": "avatar", "value": "🤖 ", "price": 50},
    "avatar_diamond": {"name": "Аватар: Даймонд", "type": "avatar", "value": "💎", "price": 120},
    "frame_gold": {"name": "Рамка: Золото", "type": "frame", "value": "gold", "price": 60},
    "frame_neon": {"name": "Рамка: Неон", "type": "frame", "value": "neon", "price": 70},
    "frame_news": {"name": "Рамка: NewsEmoji", "type": "frame", "value": "news", "price": 110},
    "theme_dark": {"name": "Тема: Dark", "type": "theme", "value": "dark", "price": 50},
    "theme_cyber": {"name": "Тема: Cyber", "type": "theme", "value": "cyber", "price": 80},
    "theme_news": {"name": "Тема: News", "type": "theme", "value": "news", "price": 100},
    "victory_crown": {"name": "Эффект победы: Корона", "type": "victory", "value": "👑", "price": 90},
    "victory_trophy": {"name": "Эффект победы: Кубок", "type": "victory", "value": "🏆 ", "price": 90},
    "victory_confetti": {"name": "Эффект победы: Конфетти", "type": "victory", "value": "🎉", "price": 100},
}


def _ensure_profile_fields(rec):
    if not isinstance(rec, dict):
        rec = {}
    if not isinstance(rec.get("inventory"), list):
        rec["inventory"] = []
    if "coins" not in rec:
        rec["coins"] = 0
    if not isinstance(rec.get("achievements"), dict):
        rec["achievements"] = {}
    if "rooms_created" not in rec:
        rec["rooms_created"] = 0
    rec["avatar_emoji"] = str(rec.get("avatar_emoji") or "🙂")
    rec["frame_style"] = str(rec.get("frame_style") or "base")
    rec["theme_style"] = str(rec.get("theme_style") or "classic")
    rec["victory_emoji"] = str(rec.get("victory_emoji") or "🎉")
    rec["notifications_enabled"] = bool(rec.get("notifications_enabled", True))
    rec["onboarding_completed"] = bool(rec.get("onboarding_completed", False))
    return rec

ACHIEVEMENTS = {
    "first_game": {"title": "Первый шаг", "desc": "Сыграть 1 игру"},
    "gamer_20": {"title": "Игроман", "desc": "Сыграть 20 игр"},
    "gamer_100": {"title": "Марафон", "desc": "Сыграть 100 игр"},
    "collector_5": {"title": "Коллекционер", "desc": "Сыграть в 5 разных игр"},
    "streak_7": {"title": "Ритм", "desc": "Серия 7 дней"},
    "coins_200": {"title": "Копилка", "desc": "Накопить 200 монет"},
    "coins_1000": {"title": "Мешок монет", "desc": "Накопить 1000 монет"},
    "room_creator": {"title": "Хозяин комнаты", "desc": "Создать 1 комнату"},
    "blackjack_5": {"title": "Везунчик", "desc": "Выиграть 5 партий в блэкджек"},
    "hidden_collector": {"title": "Секретный коллекционер", "desc": "Купить 5 предметов", "hidden": True},
    "hidden_night": {"title": "Ночной игрок", "desc": "Сыграть ночью", "hidden": True},
}

def _game_stats(rec):
    gstats = rec.get("game_stats")
    return gstats if isinstance(gstats, dict) else {}


def _distinct_games_count(rec):
    return sum(1 for v in _game_stats(rec).values() if int((v or {}).get("played", 0) or 0) > 0)

def _get_blackjack_wins(rec):
    row = _game_stats(rec).get("blackjack")
    return int((row or {}).get("wins", 0) or 0)


def _reset_quests(user_id):
    reset_daily_quests(user_id)
    reset_weekly_quests(user_id)
    reset_seasonal_quests(user_id)


def _update_matching_quests(user_id, event_type, game_key=None, amount=1):
    _reset_quests(user_id)
    for quest_type in ("daily", "weekly", "seasonal"):
        for quest in QUESTS.get(quest_type, []):
            qtype = quest.get("type")
            qgame = quest.get("game")
            if qtype == event_type and (not qgame or qgame == game_key):
                update_quest_progress(user_id, quest_type, quest["id"], amount)


def _check_achievements(uid, rec=None):
    d = load_data()
    users = d.setdefault("users", {})
    rec = _ensure_profile_fields(rec or users.setdefault(str(uid), {}))
    achievements = rec.setdefault("achievements", {})

    total_games = int(rec.get("games_total", 0) or 0)
    streak_best = int(rec.get("streak_best", 0) or 0)
    coins = int(rec.get("coins", 0) or 0)
    distinct_games = _distinct_games_count(rec)
    rooms_created = int(rec.get("rooms_created", 0) or 0)
    bj_wins = _get_blackjack_wins(rec)
    inventory_count = len(rec.get("inventory", []))
    current_hour = datetime.now().hour

    checks = {
        "first_game": total_games >= 1,
        "gamer_20": total_games >= 20,
        "gamer_100": total_games >= 100,
        "collector_5": distinct_games >= 5,
        "streak_7": streak_best >= 7,
        "coins_200": coins >= 200,
        "coins_1000": coins >= 1000,
        "room_creator": rooms_created >= 1,
        "blackjack_5": bj_wins >= 5,
        "hidden_collector": inventory_count >= 5,
        "hidden_night": current_hour < 5 and total_games >= 1,
    }

    changed = False
    for key, ok in checks.items():
        if ok and key not in achievements:
            achievements[key] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            changed = True

    if changed:
        rec["achievements"] = achievements
        users[str(uid)] = rec
        save_data(d)
    return rec

def _record_game_play(user_id, game_key, display_name=None, session_id=None):
    if not game_key:
        return
    d = load_data()
    users = d.setdefault("users", {})
    rec = users.setdefault(str(user_id), {})
    rec = _ensure_profile_fields(rec)
    if display_name:
        rec["display_name"] = str(display_name)[:64]
    rec["last_game"] = game_key
    rec["last_game_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    gstats = rec["game_stats"] = _game_stats(rec)
    row = gstats.setdefault(game_key, {"played": 0, "wins": 0, "losses": 0, "draws": 0})
    row["played"] = int(row.get("played", 0) or 0) + 1

    rec["games_total"] = int(rec.get("games_total", 0) or 0) + 1
    history = rec.get("match_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "game": game_key,
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "session": str(session_id or ""),
    })
    rec["match_history"] = history[-50:]
    rec["coins"] = int(rec.get("coins", 0) or 0) + 2

    global_stats = d.setdefault("global_game_stats", {})
    global_stats[game_key] = int(global_stats.get(game_key, 0) or 0) + 1
    save_data(d)
    _update_matching_quests(user_id, "play_specific_game", game_key=game_key)
    if str(game_key).startswith("room_"):
        _update_matching_quests(user_id, "play_room_game", game_key=game_key)
    _update_matching_quests(user_id, "count_games", game_key=game_key)
    _update_matching_quests(user_id, "count_games_weekly", game_key=game_key)
    _update_matching_quests(user_id, "count_games_seasonal", game_key=game_key)
    _check_achievements(user_id, rec)

def _record_game_play_once(user_id, game_key, session_id, display_name=None):
    if not game_key:
        return
    sid = str(session_id or "").strip()
    if not sid:
        _record_game_play(user_id, game_key, display_name=display_name, session_id=session_id)
        return
    d = load_data()
    users = d.setdefault("users", {})
    rec = users.setdefault(str(user_id), {})
    seen = rec.get("tracked_sessions")
    if not isinstance(seen, list):
        seen = []
    uniq = f"{game_key}:{sid}"
    if uniq in seen:
        return
    seen.append(uniq)
    rec["tracked_sessions"] = seen[-1000:]
    users[str(user_id)] = rec
    save_data(d)
    _record_game_play(user_id, game_key, display_name=display_name, session_id=session_id)

def _record_game_result(user_id, game_key, result, extra=None):
    if result not in ("wins", "losses", "draws"):
        return
    d = load_data()
    users = d.setdefault("users", {})
    rec = users.setdefault(str(user_id), {})
    rec = _ensure_profile_fields(rec)
    gstats = rec["game_stats"] = _game_stats(rec)
    row = gstats.setdefault(game_key, {"played": 0, "wins": 0, "losses": 0, "draws": 0})
    row[result] = int(row.get(result, 0) or 0) + 1
    replay = {"game": game_key, "result": result, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if extra and isinstance(extra, dict):
        replay.update(extra)
    rec["last_replay"] = replay
    users[str(user_id)] = rec
    save_data(d)
    if result == "wins":
        _update_matching_quests(user_id, "count_wins", game_key=game_key)
        _update_matching_quests(user_id, "win_specific_game", game_key=game_key)
    _check_achievements(user_id, rec)


def _render_replay_text(uid):
    d = load_data()
    rec = _ensure_profile_fields(d.get("users", {}).get(str(uid), {}))
    replay = rec.get("last_replay")
    if not replay or not isinstance(replay, dict):
        return "📼 Нет данных о последней игре.\nСыграйте любую игру и сюда запишется результат."
    result_icons = {"wins": "🏆 Победа", "losses": "💀 Поражение", "draws": "🤝 Ничья"}
    game_key = replay.get("game", "?")
    title = GAME_TITLES.get(game_key, game_key)
    result_str = result_icons.get(replay.get("result"), "❓")
    at = replay.get("at", "")
    lines = [
        "📼 <b>Реплей последней игры</b>",
        f"🎮 Игра: {title}",
        f"🕓 Время: {at}",
        f"Результат: {result_str}",
    ]
    if "bet" in replay:
        bet = replay["bet"]
        change = bet if replay.get("result") == "wins" else (-bet if replay.get("result") == "losses" else 0)
        lines.append(f"💰 Ставка: {bet}🪙  ({'+' if change >= 0 else ''}{change}🪙)")
    for key, label in (
        ("player_combo", "🃏 Ваша комбинация"),
        ("bot_combo", "🤖 Комбинация бота"),
        ("score", "📊 Счёт"),
        ("opponent", "👤 Противник"),
        ("rounds", "⚔️ Раунды"),
    ):
        if key in replay:
            lines.append(f"{label}: {replay[key]}")
    row = _game_stats(rec).get(game_key)
    if row:
        lines.append(
            f"\n📈 Всего в <b>{title}</b>: {row.get('played', 0)} игр | "
            f"{row.get('wins', 0)}П / {row.get('losses', 0)}П / {row.get('draws', 0)}Н"
        )
    return "\n".join(lines)

# (префикс, игра, индекс session id в data.split("_"), мин. частей, пропускать если частей меньше)
_CALLBACK_GAME_RULES = (
    ("rps_move_", "rps", 2, 3, True),
    ("rps_join_", "rps", 2, 3, True),
    ("rps_", "rps", 1, 2, True),
    ("ttt_move_", "ttt", 2, 3, True),
    ("ttt_restart_", "ttt", 2, 3, True),
    ("ttt_join_", "ttt", 2, 3, True),
    ("millionaire_", "millionaire", 1, 3, True),
    ("wrdl_", "wordle", 2, 3, True),
    ("bship_", "bship", 2, 3, True),
    ("chess_", "chess", 2, 3, True),
    ("g2048_", "g2048", 1, 3, False),
    ("tetris_", "tetris", 1, 3, False),
    ("pong_", "pong", 1, 3, True),
    ("hangman_", "hangman", 1, 3, False),
    ("minesweeper_", "minesweeper", 1, 3, True),
    ("quizgame_", "quizgame", 2, 3, True),
    ("quiz_", "quizgame", 1, 3, True),
    ("combogame_", "combogame", 2, 3, True),
    ("combo_", "combogame", 1, 3, True),
    ("mafia_", "mafia", 2, 3, True),
    ("wordgame_join_", "wordgame", 2, 3, True),
    ("guess_inline_", "guess", None, 0, False),
    ("coin_flip", "coin", None, 0, False),
    ("slot_spin", "slot", None, 0, False),
    ("snake_", "snake", None, 0, False),
)


def _track_callback_game_play(call):
    try:
        data = str(call.data or "")
        parts = data.split("_")
        game_key = sid = None

        for prefix, key, sid_index, min_parts, required in _CALLBACK_GAME_RULES:
            if not data.startswith(prefix):
                continue
            if len(parts) < min_parts:
                if required:
                    continue
                game_key = key
            else:
                game_key = key
                if sid_index is not None and parts[sid_index] != "new":
                    sid = parts[sid_index]
            break

        if not game_key:
            return
        if not sid:
            sid = call.inline_message_id or (
                f"{call.message.chat.id}:{call.message.message_id}" if call.message else ""
            )
        uid = call.from_user.id
        name = call.from_user.first_name or call.from_user.username or str(uid)
        _record_game_play_once(uid, game_key, sid, display_name=name)
    except Exception as e:
        log_exception("track_callback_game_play", e)

def _load_profile(uid):
    user = _ensure_profile_fields(load_data().get("users", {}).get(str(uid)) or {})
    return user, user["achievements"]


def _render_profile_text(uid):
    user, unlocked = _load_profile(uid)
    return render_profile_text(
        uid=uid,
        user=user,
        lang=get_user_language(uid),
        achievements_count=len(unlocked),
        achievements_total=len(ACHIEVEMENTS),
        get_game_title=get_game_title,
    )

def _render_achievements_text(uid):
    _, unlocked = _load_profile(uid)
    visible = {
        key: meta
        for key, meta in ACHIEVEMENTS.items()
        if not meta.get("hidden") or key in unlocked
    }
    return render_achievements_text(get_user_language(uid), visible, unlocked)


def _quest_completion_summary(uid):
    _reset_quests(uid)
    progress = get_user_quests_progress(uid)
    completed = 0
    total = 0
    for quest_type in ("daily", "weekly", "seasonal"):
        for quest in QUESTS.get(quest_type, []):
            total += 1
            if int(progress.get(quest_type, {}).get(quest["id"], 0) or 0) >= int(quest.get("target", 0) or 0):
                completed += 1
    return completed, total


def _render_main_menu_status(uid):
    user, _ = _load_profile(uid)
    completed_quests, total_quests = _quest_completion_summary(uid)
    return render_main_menu_status(uid, user, completed_quests, total_quests, localized_text)


def _last_game_instruction(uid):
    user, _ = _load_profile(uid)
    return build_last_game_instruction(uid, user, get_game_title, localized_text, INLINE_BOT_USERNAME)


def _render_help_text(uid):
    return localized_text(
        uid,
        "📖 Помощь\n\n"
        "• /start или /menu — главное меню\n"
        "• /profile — профиль и статистика\n"
        "• /shop — магазин\n"
        "• /achievements — достижения\n"
        "• /find — поиск PvP соперника в ЛС\n"
        "• /party, /party_join — комнаты\n"
        "• /poker [ставка] — покер против бота\n"
        "• /replay — реплей последней игры\n"
        "• /support — связь с поддержкой\n"
        "• Напишите боту или используйте inline-режим через "
        f"<code>@{INLINE_BOT_USERNAME}</code> для запуска игр.",
        "📖 Help\n\n"
        "• /start or /menu — main menu\n"
        "• /profile — profile and stats\n"
        "• /shop — shop\n"
        "• /achievements — achievements\n"
        "• /find — find a PvP opponent in private chat\n"
        "• /party, /party_join — room games\n"
        "• /support — contact support\n"
        f"• Use inline mode with <code>@{INLINE_BOT_USERNAME}</code> to launch games.",
        "📖 Допомога\n\n"
        "• /start або /menu — головне меню\n"
        "• /profile — профіль і статистика\n"
        "• /shop — магазин\n"
        "• /achievements — досягнення\n"
        "• /find — пошук PvP суперника в особистому чаті\n"
        "• /party, /party_join — кімнати\n"
        "• /support — зв'язок із підтримкою\n"
        f"• Використовуйте inline-режим через <code>@{INLINE_BOT_USERNAME}</code> для запуску ігор.",
    )


def _render_whats_new_text(uid):
    title = localized_text(uid, "🆕 Что нового", "🆕 What's New", "🆕 Що нового")
    items = WHATS_NEW_ITEMS.get(get_user_language(uid), WHATS_NEW_ITEMS["ru"])
    return "\n".join([title, ""] + [f"• {item}" for item in items])


def _render_onboarding_text(uid):
    return localized_text(
        uid,
        "👋 Добро пожаловать!\n\n"
        "1. Откройте раздел игр.\n"
        "2. Выберите режим: соло, PvP или комната.\n"
        "3. Загляните в профиль и квесты, чтобы следить за прогрессом.\n"
        "4. Если что-то непонятно, используйте /help.",
        "👋 Welcome!\n\n"
        "1. Open the games section.\n"
        "2. Pick solo, PvP, or a room mode.\n"
        "3. Check profile and quests to track progress.\n"
        "4. Use /help if you need guidance.",
        "👋 Ласкаво просимо!\n\n"
        "1. Відкрийте розділ ігор.\n"
        "2. Оберіть соло, PvP або режим кімнати.\n"
        "3. Загляньте в профіль і квести, щоб бачити прогрес.\n"
        "4. Якщо щось незрозуміло, скористайтеся /help.",
    )


def _rooms_get_data():
    d = load_data()
    rooms = d.setdefault("rooms", {"pool": [], "active": {}, "free_title": ROOM_FREE_TITLE})
    rooms.setdefault("pool", [])
    rooms.setdefault("active", {})
    rooms.setdefault("free_title", ROOM_FREE_TITLE)
    return d, rooms

def _rooms_active(rooms):
    active = rooms.get("active")
    return active if isinstance(active, dict) else {}


def _room_generate_code(rooms):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    active = _rooms_active(rooms)
    while True:
        code = "".join(random.choice(alphabet) for _ in range(ROOM_CODE_LEN))
        if code not in active:
            return code

def _room_pick_free_chat(rooms):
    pool = rooms.get("pool") if isinstance(rooms.get("pool"), list) else []
    busy = {r.get("chat_id") for r in _rooms_active(rooms).values() if isinstance(r, dict)}
    for chat_id in pool:
        if chat_id not in busy:
            return chat_id
    return None

def _room_find_by_chat(rooms, chat_id):
    for code, room in _rooms_active(rooms).items():
        if isinstance(room, dict) and room.get("chat_id") == chat_id:
            return code, room
    return None, None


# game_key -> (название для подсказки, inline-запрос для кнопки запуска)
ROOM_INLINE_GAMES = {
    "ttt": ("крестики-нолики", "крестики-нолики"),
    "chess": ("шахматы", "шахматы"),
    "bship": ("морской бой", "морской бой"),
    "mafia": ("Мафию", "мафия"),
    "wordgame": ("словесную дуэль", "словесная дуэль"),
    "quizgame": ("Викторину", "викторина"),
    "combogame": ("комбо-битву", "комбо-битва"),
}


def _room_game_start_text(game_key):
    if is_room_game(game_key):
        return room_game_start_text(game_key)
    entry = ROOM_INLINE_GAMES.get(game_key)
    if not entry:
        return "Игра выбрана."
    return f"Запуск: напишите <code>@{INLINE_BOT_USERNAME}</code> и выберите {entry[0]}."

def _room_launch_kb(game_key):
    if is_room_game(game_key):
        return None
    entry = ROOM_INLINE_GAMES.get(game_key)
    if not entry:
        return None
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("▶️ Запустить игру", switch_inline_query_current_chat=entry[1]))
    return kb

def _room_post_game_prompt(chat_id, code):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✅ Да", callback_data=f"room_continue_yes_{code}"),
        types.InlineKeyboardButton("❌ Нет", callback_data=f"room_continue_no_{code}")
    )
    try:
        msg = bot.send_message(chat_id, "🔁 Продолжаем?", reply_markup=kb)
        _room_track_message_id(chat_id, getattr(msg, "message_id", None))
    except Exception:
        pass

def _room_game_end_kb(code):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🏁 Игра завершена", callback_data=f"room_game_end_{code}"))
    return kb

def _room_track_message_id(chat_id, message_id):
    if not chat_id or not message_id:
        return
    msg_list = room_messages.setdefault(chat_id, [])
    msg_list.append(message_id)
    if ROOM_MESSAGE_BUFFER and len(msg_list) > ROOM_MESSAGE_BUFFER:
        room_messages[chat_id] = msg_list[-ROOM_MESSAGE_BUFFER:]

def _room_start_vote(chat_id, code):
    options = [title for _, title in ROOM_VOTE_GAMES]
    keys = [key for key, _ in ROOM_VOTE_GAMES]
    try:
        msg = bot.send_poll(chat_id, "Выберите игру голосованием:", options, is_anonymous=False, allows_multiple_answers=False)
        poll_id = msg.poll.id
        _room_track_message_id(chat_id, msg.message_id)
    except Exception:
        poll_id = None
    d, rooms = _rooms_get_data()
    room = _rooms_active(rooms).get(code, {})
    room["vote_options"] = keys
    if poll_id:
        room["poll_id"] = poll_id
        room_polls[poll_id] = {"code": code, "options": keys}
    rooms["active"][code] = room
    save_data(d)

    def finalize():
        time.sleep(ROOM_VOTE_SECONDS)
        _room_finalize_vote(code)
    Thread(target=finalize, daemon=True).start()

def _room_finalize_vote(code):
    d, rooms = _rooms_get_data()
    room = rooms.get("active", {}).get(code)
    if not isinstance(room, dict):
        return
    if room.get("game"):
        return
    options = room.get("vote_options", [])
    votes = room.get("votes", {})
    if not options:
        return
    tally = {i: 0 for i in range(len(options))}
    if isinstance(votes, dict):
        for opt in votes.values():
            try:
                idx = int(opt)
            except Exception:
                continue
            if idx in tally:
                tally[idx] += 1
    best = max(tally.values())
    if best > 0:
        winner_idx = random.choice([i for i, v in tally.items() if v == best])
    else:
        winner_idx = random.randrange(0, len(options))
    chosen_key = options[winner_idx]
    room["game"] = chosen_key
    room["status"] = "active"
    room["last_activity_at"] = time.time()
    rooms["active"][code] = room
    save_data(d)
    for player_id in room.get("participants", []) or []:
        try:
            _record_game_play(int(player_id), chosen_key, session_id=code)
        except Exception:
            pass
    try:
        msg1 = bot.send_message(
            room["chat_id"],
            f"✅ Выбрана игра: {GAME_TITLES.get(chosen_key, chosen_key)}\n\n{_room_game_start_text(chosen_key)}",
            parse_mode="HTML"
        )
        _room_track_message_id(room["chat_id"], getattr(msg1, "message_id", None))
        launch_kb = _room_launch_kb(chosen_key)
        if launch_kb:
            msg_launch = bot.send_message(
                room["chat_id"],
                "Нажмите, чтобы сразу открыть игру в этом чате:",
                reply_markup=launch_kb
            )
            _room_track_message_id(room["chat_id"], getattr(msg_launch, "message_id", None))
        msg2 = bot.send_message(
            room["chat_id"],
            "Когда закончите партию, нажмите кнопку ниже.",
            reply_markup=_room_game_end_kb(code)
        )
        _room_track_message_id(room["chat_id"], getattr(msg2, "message_id", None))
    except Exception:
        pass

    try:
        if is_room_game(chosen_key):
            room_game_launch(bot, room["chat_id"], code, room)
            return
        if chosen_key == "reaction":
            _reaction_start(room["chat_id"], room.get("creator_id"))
        elif chosen_key == "blackjack":
            state = _bj_new_game(room.get("creator_id"), room["chat_id"])
            gid = short_id()
            blackjack_games[gid] = state
            text = _bj_render_text(state, reveal_dealer=state.get("status") != "playing")
            kb = _bj_keyboard(gid, state.get("status"))
            msg = bot.send_message(room["chat_id"], text, reply_markup=kb)
            _room_track_message_id(room["chat_id"], getattr(msg, "message_id", None))
    except Exception:
        pass

def _room_close(code, reason=""):
    d, rooms = _rooms_get_data()
    active = _rooms_active(rooms)
    room = active.pop(code, None)
    if not isinstance(room, dict):
        return False
    chat_id = room.get("chat_id")

    try:
        old_link = room.get("invite_link")
        if old_link:
            bot.revoke_chat_invite_link(chat_id, old_link)
        bot.create_chat_invite_link(chat_id)
    except Exception:
        pass

    try:
        bot.set_chat_title(chat_id, rooms.get("free_title", ROOM_FREE_TITLE))
    except Exception:
        pass

    participants = set(room.get("participants", []) or [])
    participants.update(room_participants.get(chat_id, set()))
    for uid in participants:
        try:
            bot.kick_chat_member(chat_id, uid)
            bot.unban_chat_member(chat_id, uid)
        except Exception:
            pass

    for mid in room_messages.get(chat_id, []):
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass

    room_messages.pop(chat_id, None)
    room_participants.pop(chat_id, None)
    cleanup_room_runtime_state(code)
    rooms["active"] = active
    save_data(d)

    try:
        bot.send_message(chat_id, f"⏳ Пати закрыто. {('Причина: ' + reason) if reason else ''}\nГруппа освобождена.")
    except Exception:
        pass
    return True

def _rooms_watchdog():
    while not _shutdown_event.is_set():
        try:
            _, rooms = _rooms_get_data()
            now_ts = time.time()
            for code, room in list(_rooms_active(rooms).items()):
                if not isinstance(room, dict):
                    continue
                ends_at = float(room.get("ends_at") or 0)
                if ends_at and now_ts >= ends_at:
                    _room_close(code, reason="таймер 1 час")
                    continue
                last_activity_at = float(room.get("last_activity_at") or room.get("created_at") or 0)
                if last_activity_at and now_ts - last_activity_at >= ROOM_IDLE_TIMEOUT_SECONDS:
                    _room_close(code, reason="нет активности")
        except Exception:
            LOGGER.exception("rooms watchdog failed")
        _shutdown_event.wait(30)


def _shop_render_text(uid):
    rec, _ = _load_profile(uid)
    owned = set(rec.get("inventory", []))
    lines = [
        "🛍 Магазин",
        f"🪙 Ваш баланс: {int(rec.get('coins', 0) or 0)} монет",
        "",
    ]
    sections = {
        "avatar": "👤 Аватары",
        "frame": "💎 Рамки",
        "theme": "⚙️ Темы",
        "victory": "🏆 Победные эффекты",
    }
    for item_type, title in sections.items():
        lines.append(title)
        for item_id, item in SHOP_ITEMS.items():
            if item.get("type") != item_type:
                continue
            mark = "✅ куплено" if item_id in owned else f"{item['price']} 🪙"
            lines.append(f"• {item['name']} — {mark}")
        lines.append("")
    return "\n".join(lines)


def _shop_items_kb(uid):
    rec, _ = _load_profile(uid)
    owned = set(rec.get("inventory", []))
    kb = types.InlineKeyboardMarkup()
    for item_id, item in SHOP_ITEMS.items():
        if item_id in owned:
            kb.add(types.InlineKeyboardButton(f"✅ {item['name']}", callback_data=f"shop_apply_{item_id}"))
        else:
            kb.add(types.InlineKeyboardButton(f"🛍 {item['name']} ({item['price']}🪙)", callback_data=f"shop_buy_{item_id}"))
    kb.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="shop_open"))
    return kb

def has_premium(uid):
    user = get_user(uid)
    return user["premium_until"] > time.time()

def can_use_ai(uid):
    user = get_user(uid)
    if has_premium(uid):
        return True, None
    cnt = int(user.get("count", 0) or 0)
    if cnt < FREE_DAILY_QUOTA:
        return True, None
    return False, f"⚠️ Лимит {FREE_DAILY_QUOTA} запросов в день. Купите премиум для неограниченного доступа."


def _empty_quests_progress():
    return {
        "daily": {},
        "weekly": {},
        "seasonal": {},
        "last_daily_reset": "",
        "last_weekly_reset": "",
        "last_seasonal_reset": "",
        "claimed": [],
        "notified": [],
    }


def get_user_quests_progress(user_id):
    data = load_data()
    rec = data.setdefault("users", {}).setdefault(str(user_id), {})
    progress = rec.setdefault("quests_progress", _empty_quests_progress())
    for key, default in _empty_quests_progress().items():
        progress.setdefault(key, default)
    save_data(data)
    return progress


def _notify_quest_completed(user_id, quest):
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(user_id), {})
    rec = _ensure_profile_fields(rec)
    if not rec.get("notifications_enabled", True):
        d["users"][str(user_id)] = rec
        save_data(d)
        return
    try:
        bot.send_message(
            user_id,
            "✅ Квест выполнен!\n"
            f"{quest['title']}\n"
            f"Награда: {quest['reward']}\n"
            "Откройте раздел квестов, чтобы забрать награду."
        )
    except Exception as e:
        log_exception("notify_quest_completed", e, user_id=user_id)

def _reset_quest_bucket(user_id, quest_type, period_id):
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(user_id), {})
    progress = rec.setdefault("quests_progress", _empty_quests_progress())
    stamp_key = f"last_{quest_type}_reset"
    if progress.get(stamp_key) == period_id:
        return
    quest_ids = {q["id"] for q in QUESTS.get(quest_type, [])}
    progress[stamp_key] = period_id
    progress[quest_type] = {qid: 0 for qid in quest_ids}
    progress["claimed"] = [qid for qid in progress.get("claimed", []) if qid not in quest_ids]
    save_data(d)


def reset_daily_quests(user_id):
    _reset_quest_bucket(user_id, "daily", date.today().isoformat())


def reset_weekly_quests(user_id):
    today = date.today()
    _reset_quest_bucket(user_id, "weekly", (today - timedelta(days=today.weekday())).isoformat())


def reset_seasonal_quests(user_id):
    _reset_quest_bucket(user_id, "seasonal", datetime.now().strftime("%Y-%m"))

def update_quest_progress(user_id, quest_type, quest_id, amount=1):
    reset_daily_quests(user_id)
    if quest_type == "weekly":
        reset_weekly_quests(user_id)
    elif quest_type == "seasonal":
        reset_seasonal_quests(user_id)
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(user_id), {})
    progress = rec.setdefault("quests_progress", _empty_quests_progress())
    for key, default in _empty_quests_progress().items():
        progress.setdefault(key, default)
    if quest_id in progress.get(quest_type, {}):
        previous = int(progress[quest_type].get(quest_id, 0) or 0)
        progress[quest_type][quest_id] = previous + amount
        save_data(d)
        quest = next((q for q in QUESTS.get(quest_type, []) if q.get("id") == quest_id), None)
        if quest and previous < int(quest.get("target", 0) or 0) <= progress[quest_type][quest_id]:
            if quest_id not in progress["notified"]:
                progress["notified"].append(quest_id)
                save_data(d)
                _notify_quest_completed(user_id, quest)

def claim_quest_reward(user_id, quest_type, quest_id):
    progress = get_user_quests_progress(user_id)
    if quest_id not in progress[quest_type]:
        return False
    quest = next((q for q in QUESTS[quest_type] if q["id"] == quest_id), None)
    if not quest:
        return False
    if progress[quest_type][quest_id] < quest["target"]:
        return False
    if quest_id in progress.get("claimed", []):
        return False
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(user_id), {})
    reward = quest["reward"]
    for field in ("coins", "xp"):
        if field in reward:
            rec[field] = rec.get(field, 0) + reward[field]
    progress["claimed"].append(quest_id)
    rec["quests_progress"] = progress
    save_data(d)
    return True

def pong_game_loop(gid, inline_id):
    while gid in games_pong:
        state = games_pong.get(gid)
        if not state:
            break
        if not state.get("started"):
            time.sleep(0.35)
            continue

        _pong_step(state)
        try:
            bot.edit_message_text(
                _render_pong_text(state),
                inline_message_id=inline_id,
                reply_markup=_pong_controls_markup(
                    gid,
                    started=state.get("started", False),
                    game_over=state.get("winner") is not None,
                ),
            )
        except Exception:
            break

        if state.get("winner"):
            games_pong.pop(gid, None)
            break
        time.sleep(0.6)

def clear_premium(user_id):
    d = load_data()
    if str(user_id) in d.get("premium", {}):
        del d["premium"][str(user_id)]
    user = d.setdefault("users", {}).setdefault(str(user_id), {})
    user["is_premium"] = False
    user["premium_until"] = None
    save_data(d)
    

def start_premium_watcher(bot_instance, check_interval=3600):
    """Напоминает о премиуме за 24 часа до конца и отключает его по истечении."""
    def watcher():
        while not _shutdown_event.is_set():
            try:
                data = load_data()
                pm = data.get("premium", {})
                now = datetime.utcnow()
                for uid_str, info in list(pm.items()):
                    try:
                        until_ts = info.get("until")
                        if not until_ts:
                            continue
                        until_dt = datetime.fromtimestamp(until_ts)
                        seconds_left = (until_dt - now).total_seconds()
                        uid = int(uid_str)
                        if 0 < seconds_left <= 24 * 3600 and not info.get("reminded_24h"):
                            try:
                                bot_instance.send_message(uid, f"⚠️ Ваша премиум-подписка истекает {until_dt.isoformat()} UTC. Продлите, чтобы не потерять доступ.")
                            except Exception as e:
                                log_exception("premium_notify_24h", e, user_id=uid)
                            info["reminded_24h"] = True
                        if seconds_left <= 0:
                            try:
                                bot_instance.send_message(uid, "⚠️ Ваша премиум-подписка окончена. Пока не продлите — премиум приостановлен.")
                            except Exception as e:
                                log_exception("premium_notify_expired", e, user_id=uid)
                            clear_premium(uid)
                            pm.pop(str(uid), None)
                    except Exception as e:
                        log_exception("premium_watcher_entry", e)
                data["premium"] = pm
                save_data(data)
            except Exception as e:
                log_exception("premium_watcher", e)
            _shutdown_event.wait(check_interval)
    Thread(target=watcher, daemon=True).start()

def hide_keyboard(prefix):
    kb = types.InlineKeyboardMarkup()
    for r in range(3):
        kb.row(*[types.InlineKeyboardButton("⬜", callback_data=f"{prefix}_{r * 3 + c}") for c in range(3)])
    return kb


def _channel_url():
    if not REQUIRED_CHANNEL:
        return None
    return f"https://t.me/{REQUIRED_CHANNEL.lstrip('@')}"

def is_user_subscribed(user_id):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member", "restricted")
    except Exception:
        return False


@bot.callback_query_handler(func=lambda c: c.data == "check_subscription")
def check_subscription_callback(call):
    uid = call.from_user.id
    if is_user_subscribed(uid):
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена.")
        show_main_menu(call.message.chat.id if call.message else uid, uid)
    else:
        bot.answer_callback_query(call.id, "⚠️ Подписка пока не найдена.", show_alert=True)


def _is_group_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

def _inline_guard(query):
    """Отмечает активность и проверяет подписку; False — ответ уже отправлен."""
    user = query.from_user
    update_user_streak(user.id, user.first_name or user.username or str(user.id))
    if REQUIRED_CHANNEL and not is_user_subscribed(user.id):
        inline_subscription_prompt(query)
        return False
    return True


def inline_subscription_prompt(query):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📣 Подписаться", url=_channel_url() or "https://t.me/"))
    art = types.InlineQueryResultArticle(
        id="must_subscribe",
        title="⚠️ Вы не подписаны на канал!",
        description="Чтобы использовать этого бота — подпишитесь на его канал.",
        input_message_content=types.InputTextMessageContent(
            "⚠️ Для использования бота необходимо подписаться на официальный канал. Нажмите кнопку ниже, затем повторите действие."
        ),
        reply_markup=kb
    )
    try:
        bot.answer_inline_query(query.id, [art], cache_time=1, is_personal=True)
    except Exception:
        pass

register_business_handlers(
    bot,
    required_channel=REQUIRED_CHANNEL,
    is_user_subscribed=is_user_subscribed,
)


def _is_unchanged_message_error(exc):
    msg = str(exc)
    return "message is not modified" in msg or "exactly the same" in msg


def safe_edit_message(call, text, reply_markup=None, parse_mode=None):
    """Правит inline-сообщение или обычное; при отсутствии обоих шлёт новое."""
    try:
        if getattr(call, "inline_message_id", None):
            bot.edit_message_text(text, inline_message_id=call.inline_message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        elif call.message:
            bot.edit_message_text(text, chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            bot.send_message(call.from_user.id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        if not _is_unchanged_message_error(e):
            log_exception("safe_edit_message", e)


def _send_or_edit_tracked(store, uid, text, reply_markup=None, context="private_view"):
    """Обновляет ранее отправленное личное сообщение из store или шлёт новое."""
    cur = store.get(uid) if isinstance(store, dict) else None
    try:
        if isinstance(cur, dict) and cur.get("chat_id") and cur.get("message_id"):
            bot.edit_message_text(text, chat_id=cur["chat_id"], message_id=cur["message_id"], reply_markup=reply_markup)
            return True
    except Exception as e:
        if not _is_unchanged_message_error(e):
            log_exception(context, e, user_id=uid)
    try:
        msg = bot.send_message(uid, text, reply_markup=reply_markup)
        store[uid] = {"chat_id": msg.chat.id, "message_id": msg.message_id}
        return True
    except Exception as e:
        log_exception(context, e, user_id=uid)
        return False


questions = [
    {
        "question": "Что такое Python?",
        "options": ["Язык программирования", "Программа", "Страна", "Ничего не подходит"],
        "answer": "Язык программирования"
    },
    {
        "question": "Что такое Roblox?",
        "options": ["Язык программирования", "Приложение", "Игра", "Платформа"],
        "answer": "Платформа"
    },
    {
        "question": "Какой тип данных используется для хранения текста в Python?",
        "options": ["int", "str", "float", "bool"],
        "answer": "str"
    },
    {
        "question": "Столица Франции?",
        "options": ["Париж", "Берлин", "Мадрид", "Рим"],
        "answer": "Париж"
    },
    {
        "question": "Сколько будет 2 + 2?",
        "options": ["3", "4", "5", "22"],
        "answer": "5"
    },
    {
        "question": "Какой океан самый большой?",
        "options": ["Тихий", "Атлантический", "Индийский", "Северный Ледовитый"],
        "answer": "Тихий"
    }
]

inline_ttt_games = {}
inline_guess_games = {}
inline_snake_games = {}
inline_duel_games = {}
user_sys_settings = {}
system_notify_wait = {}
telos_input_wait = {}
support_chat_wait = {}
admin_wait = {}
millionaire_games = {}
user_show_easter_egg = {}
pm_flappy_games = {}
games_2048 = {}
games_pong = {}
user_ai_mode = {}
rps_games = {}
hide_games = {}
hangman_games = {}
mafia_games = {}
games_tetris = {}
reaction_games = {}
blackjack_games = {}
room_polls = {}
pm_ttt_games = {}
find_queue = {}
find_matches = {}

FIND_ONLINE_TTL = 300
FIND_VOTE_SECONDS = 45
MATCHMAKING_GAMES = ("ttt", "bship", "chess")
room_messages = {}
room_participants = {}

# Слово -> подсказка
HANGMAN_WORDS = {
    "пайтон": "Язык программирования с именем змеи",
    "программист": "Человек, который пишет код",
    "компьютер": "Электронная вычислительная машина",
    "интернет": "Глобальная сеть связи",
    "телефон": "Устройство для связи",
    "клавиатура": "Устройство для ввода текста",
    "монитор": "Экран для вывода информации",
    "сервер": "Компьютер, предоставляющий услуги",
    "приложение": "Программное обеспечение",
    "функция": "Блок кода, который выполняет задачу",
    "переменная": "Контейнер для хранения данных",
    "алгоритм": "Последовательность шагов для решения задачи",
    "данные": "Информация для обработки",
    "байт": "Единица измерения информации",
    "пиксель": "Точка на экране",
    "игра": "Развлечение с правилами",
    "музыка": "Искусство звуков",
    "книга": "Сшитые листы с текстом",
    "машина": "Транспортное средство",
    "птица": "Животное, которое летает",
    "цветок": "Растение с яркими лепестками",
    "звезда": "Небесное тело на ночном небе",
    "луна": "Спутник земли",
    "солнце": "Звезда нашей системы",
    "океан": "Очень большой водный массив",
    "гора": "Высокое возвышение земли",
    "река": "Поток воды на земле",
    "лес": "Большое скопление деревьев",
    "город": "Населённый пункт с домами",
    "дорога": "Путь для передвижения",
    "школа": "Учебное заведение для детей",
    "учитель": "Человек, который учит",
    "ученик": "Человек, который учится",
    "друг": "Близкий человек",
    "семья": "Группа близких людей",
    "мама": "Женщина, которая родила вас",
    "папа": "Мужчина, который родил вас",
    "сестра": "Женская сестра",
    "брат": "Мужская сестра",
    "дом": "Здание для проживания",
    "окно": "Отверстие в стене для света",
    "дверь": "Вход в комнату или здание",
    "стол": "Мебель для работы или еды",
    "стул": "Мебель для сидения",
    "кровать": "Мебель для сна",
    "хлеб": "Продукт из муки и воды",
    "молоко": "Жидкость от коров",
    "масло": "Жидкий продукт для готовки",
    "сыр": "Молочный продукт",
    "яйцо": "Продукт от птиц",
    "рыба": "Животное, которое живёт в воде",
    "мясо": "Животный продукт питания",
    "салат": "Блюдо из овощей",
    "суп": "Жидкое блюдо",
    "радость": "Положительное чувство",
    "грусть": "Отрицательное чувство",
    "любовь": "Сильное положительное чувство",
    "надежда": "Вера в будущее",
    "вера": "Уверенность в чём-то",
    "сила": "Способность что-то делать",
    "ум": "Способность думать",
    "душа": "Внутренний мир человека",
    "сердце": "Орган, который качает кровь",
    "разум": "Способность к логике",
    "воля": "Определённость в действиях",
    "честь": "Репутация и достоинство",
    "долг": "Обязательство перед другими",
    "подвиг": "Героический поступок",
    "война": "Вооружённый конфликт",
    "мир": "Отсутствие войны",
    "победа": "Успех в борьбе",
    "поражение": "Неудача в борьбе",
    "истина": "То, что соответствует реальности",
    "ложь": "То, что не соответствует реальности",
    "справедливость": "Честное обращение",
    "несправедливость": "Нечестное обращение"
}

word_games = {}
quiz_games = {}
combo_games = {}
wordle_games = {}
chess_games = {}
battleship_games = {}

WORDLE_WORDS = [
    "абзац", "аванс", "аврал", "автор", "агент", "адрес", "азарт", "актер",
    "акция", "алмаз", "аллея", "амбар", "ангел", "арбат", "арбуз", "арена",
    "архив", "астра", "атлас", "багаж", "багет", "байка", "балет", "балка",
    "банан", "банка", "барин", "башня", "берег", "билет", "блеск", "блюдо",
    "бобер", "богач", "бокал", "бочка", "брешь", "бровь", "брюки", "буква",
    "буран", "бутон", "вагон", "вдова", "весна", "ветер", "ветка", "вечер",
    "вилка", "вирус", "вишня", "влага", "взлет", "видео", "визит", "виток",
    "вокал", "волна", "время", "входы", "выдох", "выход", "гений", "герой",
    "глава", "глина", "голод", "голос", "гонка", "город", "горох", "гость",
    "графа", "гроза", "груша", "дебют", "дверь", "девиз", "декор", "диван",
    "дождь", "доска", "доход", "драка", "дрема", "дрель", "дымка", "жажда",
    "жизнь", "живот", "жираф", "завод", "загар", "закон", "замок", "запах",
    "заряд", "зебра", "земля", "зерно", "зверь", "зубок", "игрок", "идеал",
    "износ", "искра", "исход", "какао", "казна", "камин", "канат", "канон",
    "капля", "карта", "катер", "кепка", "киоск", "кисть", "кивок", "класс",
    "книга", "кобра", "ковер", "койка", "кольт", "конус", "копия", "корка",
    "корма", "кошка", "краса", "крона", "крупа", "крыло", "купол", "курок",
    "кухня", "ласка", "лавка", "лазер", "лампа", "лапша", "левша", "лента",
    "лимон", "линия", "лодка", "ложка", "локон", "лучик", "лыжня", "магия",
    "майка", "майор", "манго", "манеж", "марка", "маска", "масса", "медик",
    "мелок", "место", "метод", "метро", "мечта", "мираж", "минус", "миска",
    "модем", "мойка", "мороз", "моряк", "мосты", "мотор", "музей", "набор",
    "навык", "напев", "наряд", "нация", "недра", "нерпа", "нитка", "ночка",
    "номер", "норма", "носок", "ножик", "облик", "обман", "обмен", "образ",
    "обувь", "обряд", "огонь", "океан", "оклад", "окрас", "олень", "омлет",
    "опека", "орден", "осень", "отдых", "отель", "ответ", "отзыв", "отряд",
    "очерк", "падеж", "пакет", "палец", "палка", "панно", "парус", "паста",
    "пауза", "певец", "пенал", "перец", "песня", "печка", "пиала", "пилот",
    "пирог", "пламя", "плита", "повар", "повод", "поезд", "поиск", "показ",
    "полет", "полка", "порог", "порыв", "поток", "почка", "почва", "поэма",
    "право", "проза", "птица", "пчела", "пульт", "пункт", "пучок", "радар",
    "район", "раунд", "ребро", "рейка", "робот", "ролик", "роман", "рубин",
    "рубль", "ручей", "ручка", "рыбак", "рынок", "садик", "салют", "сапог",
    "сахар", "сборы", "свеча", "север", "секта", "семья", "сетка", "синий",
    "сироп", "скала", "сквер", "склад", "скрип", "скука", "слава", "слеза",
    "слово", "слуга", "слюна", "смесь", "снова", "сокол", "сосна", "совет",
    "спазм", "спина", "спорт", "спуск", "спрос", "среда", "старт", "стена",
    "страж", "стихи", "стриж", "струя", "сумка", "сушка", "суета", "судно",
    "сфера", "сцена", "сыщик", "тайна", "такси", "танго", "танец", "театр",
    "телец", "тембр", "тепло", "тесто", "тираж", "товар", "тонус", "топаз",
    "топор", "торец", "точка", "трава", "трель", "тропа", "труба", "тучка",
    "туман", "турок", "уголь", "удача", "уклад", "улика", "уроки", "устои",
    "утиль", "утром", "факел", "фауна", "ферма", "фикус", "финик", "фирма",
    "флора", "фокус", "форма", "фраза", "халва", "хвост", "хижак", "хитон",
    "хлеба", "холод", "хомяк", "хорек", "хруст", "цветы", "цифра", "цапля",
    "центр", "чайка", "часть", "чашка", "череп", "честь", "чехол", "число",
    "чулок", "шайба", "шаман", "шапка", "шарик", "шепот", "школа", "шорох",
    "шпага", "штиль", "шторм", "шторы", "шутка", "щенок", "щепка", "щетка",
    "щиток", "экран", "эскиз", "этажи", "этика", "юниор", "юрист", "ягода",
    "ямщик", "ясень"
]

WORD_LIST = [
    "абрикос", "авокадо", "апельсин", "арбуз", "баклажан", "батон", "белок", "берёза",
    "билет", "блюдо", "борода", "ботинок", "будка", "булка", "булочка", "буква", "бульон",
    "вагон", "ванна", "ведро", "век", "велосипед", "весёлый", "веселье", "весна", "ветер",
    "ветка", "видео", "вилка", "виноград", "виолончель", "висок", "вода", "водитель", "воланчик",
    "волк", "волос", "волшебник", "волшебство", "вольтметр", "ворона", "вороны", "воротник", "ворошилка",
    "воспитание", "восток", "восьмой", "вот", "вохра", "впадина", "впечатление", "вперёд", "вперёди",
    "вперемешку", "впереди", "вплотную", "вполголоса", "вполне", "вполовину", "впопыхах",
    "впорядке", "вправду", "вправо", "впредь", "впроголодь", "впрок", "вскипание", "вскипать",
    "вскладчину", "вскользь", "вскрик", "вскрыть", "вскрытие", "вскрывать", "вскрывает", "вскупорить",
    "вскучу", "вслед", "вследствие", "вслепую", "вслух", "всмятку", "всосать", "всполох",
    "всполошить", "всю", "всюду", "вта", "втайне", "втаптывать", "втаскивать", "втачивать",
    "втачка", "втачку", "вте", "втё", "втеснение", "втеснить", "втеснять", "втёртый"
]

QUIZ_QUESTIONS = [
    {"q": "Сколько планет в солнечной системе?", "a": "8"},
    {"q": "Какой язык программирования самый популярный?", "a": "пайтон"},
    {"q": "Столица России?", "a": "москва"},
    {"q": "Кто написал 'Войну и мир'?", "a": "толстой"},
    {"q": "Какой элемент имеет символ 'O'?", "a": "кислород"},
    {"q": "Сколько континентов на Земле?", "a": "7"},
    {"q": "Столица Украины?", "a": "киев"},
    {"q": "Кто изобрёл телефон?", "a": "грейм белл"},
    {"q": "Какое самое глубокое место в мировом океане?", "a": "марианская впадина"},
    {"q": "Сколько строк в каноне Уголовного кодекса РФ?", "a": "360"},
    {"q": "Какой элемент имеет символ 'Au'?", "a": "золото"},
    {"q": "Сколько струн на скрипке?", "a": "4"},
    {"q": "В каком году началась Вторая мировая война?", "a": "1939"},
    {"q": "Что изобрёл Томас Эдисон?", "a": "лампочка"},
    {"q": "Сколько букв в слове 'Телеграм'?", "a": "7"},
]

QUIZ_ALPHABET = "абвгдеёжзийклмнопрстуфхцчшщъйэюя"
COMBO_CHOICES = {"lightning": "⚡ Молния", "shield": "🛡️ Щит", "rock": "🪨 Камень"}
COMBO_BEATS = {"lightning": "rock", "shield": "lightning", "rock": "shield"}
COMBO_INTRO_TEXT = (
    "⚡ *Комбо-битва*\n\n"
    "Правила:\n"
    "⚡ Молния > 🪨 Камень\n"
    "🪨 Камень > 🛡️ Щит\n"
    "🛡️ Щит > ⚡ Молния\n\n"
    "Лучший из 3 раундов!"
)


def _quiz_new_game(qdata, owner_id, owner_name):
    return {
        "question": qdata["q"],
        "answer": qdata["a"].lower(),
        "players": [owner_id],
        "names": {owner_id: owner_name},
        "inputs": {},
        "answered": {},
        "correct": {},
        "max_players": 4,
        "started": False,
        "locked": False,
        "owner": owner_id,
    }


def _quiz_intro_text(question):
    return (
        "🧠 *Викторина*\n\n"
        f"❓ {question}\n\n"
        "Кто ответит первым правильно - выигрывает!"
    )


def _quiz_join_kb(gid, owner_can_start=False):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"quizgame_join_{gid}"))
    if owner_can_start:
        kb.add(types.InlineKeyboardButton("▶️ Старт", callback_data=f"quizgame_start_{gid}"))
    return kb


def _quiz_input_kb(gid):
    kb = types.InlineKeyboardMarkup()
    row = []
    for i, letter in enumerate(QUIZ_ALPHABET):
        if i % 6 == 0 and i > 0:
            kb.row(*row)
            row = []
        row.append(types.InlineKeyboardButton(letter.upper(), callback_data=f"quiz_{gid}_{letter}"))
    if row:
        kb.row(*row)
    kb.row(*[types.InlineKeyboardButton(str(i), callback_data=f"quiz_{gid}_{i}") for i in range(10)])
    kb.row(
        types.InlineKeyboardButton("⌫", callback_data=f"quiz_{gid}_back"),
        types.InlineKeyboardButton("✅ Готово", callback_data=f"quiz_{gid}_submit"),
    )
    return kb


def _quiz_status_text(game, footer):
    players = game["players"]
    text = "🧠 *Викторина*\n\n"
    text += f"❓ {game['question']}\n\n"
    text += f"Игроки ({len(players)}/{game.get('max_players', 4)}):\n\n"
    for pid in players:
        status = "✅ ответ готов" if game["answered"].get(pid) else "⌨️ вводит"
        text += f"- {game['names'].get(pid, 'Игрок')}: {status}\n\n"
    return text + footer


def _quiz_normalize_game(game):
    """Приводит старые записи викторины (p1/p2) к общему формату со списком players."""
    if "players" in game:
        return game
    players = [pid for pid in (game.get("p1"), game.get("p2")) if pid is not None]
    game["players"] = players
    names = game.setdefault("names", {})
    if game.get("p1") is not None:
        names.setdefault(game["p1"], game.get("p1_name", "Игрок 1"))
    if game.get("p2") is not None:
        names.setdefault(game["p2"], game.get("p2_name", "Игрок 2"))
    game.setdefault("inputs", {})
    game.setdefault("answered", {})
    game.setdefault("correct", {})
    game["max_players"] = 4
    game["started"] = len(players) >= 2
    game["locked"] = False
    game["owner"] = players[0] if players else None
    return game


def _combo_new_game(owner_id, owner_name):
    return {
        "p1": owner_id,
        "p1_name": owner_name,
        "p2": None,
        "p1_choice": None,
        "p2_choice": None,
        "round": 1,
        "scores": {owner_id: 0},
    }


def _combo_join_kb(gid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"combogame_join_{gid}"))
    return kb


def _combo_move_kb(gid):
    kb = types.InlineKeyboardMarkup()
    kb.row(*[types.InlineKeyboardButton(label, callback_data=f"combo_{gid}_{key}") for key, label in COMBO_CHOICES.items()])
    return kb


def short_id():
    return str(int(time.time()*1000))


def _reaction_new_state(uid, chat_id=None):
    return {"uid": uid, "chat_id": chat_id, "started": False, "start_at": None, "msg_id": None, "inline_id": None}

def _reaction_keyboard(gid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⚡ ЖМИ!", callback_data=f"reaction_hit_{gid}"))
    return kb

def _reaction_edit(state, text, reply_markup=None):
    try:
        if state.get("inline_id"):
            bot.edit_message_text(text, inline_message_id=state["inline_id"], reply_markup=reply_markup)
        elif state.get("msg_id") and state.get("chat_id"):
            bot.edit_message_text(text, chat_id=state["chat_id"], message_id=state["msg_id"], reply_markup=reply_markup)
    except Exception:
        pass


def _reaction_schedule_signal(gid):
    def trigger():
        time.sleep(random.uniform(2.0, 5.0))
        st = reaction_games.get(gid)
        if not st:
            return
        st["started"] = True
        st["start_at"] = time.time()
        _reaction_edit(st, "⚡ СИГНАЛ! ЖМИ СЕЙЧАС!", reply_markup=_reaction_keyboard(gid))

    Thread(target=trigger, daemon=True).start()


def _reaction_start(chat_id, uid):
    gid = short_id()
    state = reaction_games[gid] = _reaction_new_state(uid, chat_id)
    try:
        msg = bot.send_message(chat_id, "⚡ Блиц-реакция\nЖдите сигнала и нажмите кнопку!", reply_markup=_reaction_keyboard(gid))
        state["msg_id"] = msg.message_id
        _room_track_message_id(chat_id, msg.message_id)
    except Exception:
        state["msg_id"] = None

    _reaction_schedule_signal(gid)
    return gid


POKER_SUITS = ["♠", "♥", "♦", "♣"]
POKER_RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
POKER_RANK_VAL = {r: i for i, r in enumerate(POKER_RANKS)}
poker_games = {}

def _poker_make_deck():
    return [(r, s) for s in POKER_SUITS for r in POKER_RANKS]

def _poker_card_str(card):
    return f"{card[0]}{card[1]}"

def _poker_hand_rank(cards):
    """(ранг, список для сравнения) для комбинации из 5 карт; больше — лучше."""
    vals = sorted([POKER_RANK_VAL[r] for r, _ in cards], reverse=True)
    flush = len({s for _, s in cards}) == 1
    straight = (max(vals) - min(vals) == 4 and len(set(vals)) == 5)
    if set(vals) == {12, 0, 1, 2, 3}:
        straight = True
        vals = [3, 2, 1, 0, -1]
    counts = sorted([(vals.count(v), v) for v in set(vals)], reverse=True)
    c = [x[0] for x in counts]
    v = [x[1] for x in counts]
    if straight and flush:
        return (8, vals)
    if c[0] == 4:
        return (7, v)
    if c[0] == 3 and c[1] == 2:
        return (6, v)
    if flush:
        return (5, vals)
    if straight:
        return (4, vals)
    if c[0] == 3:
        return (3, v)
    if c[0] == 2 and c[1] == 2:
        return (2, v)
    if c[0] == 2:
        return (1, v)
    return (0, vals)

def _poker_best_hand(cards):
    return max(_poker_hand_rank(list(combo)) for combo in combinations(cards, 5))

_POKER_HAND_NAMES = {8: "Стрит-флеш", 7: "Каре", 6: "Фулл-хаус", 5: "Флеш",
                     4: "Стрит", 3: "Тройка", 2: "Две пары", 1: "Пара", 0: "Старшая карта"}

def _poker_hand_name(rank_tuple):
    return _POKER_HAND_NAMES.get(rank_tuple[0], "?")

POKER_STAGES = ["preflop", "flop", "turn", "river", "showdown"]
POKER_VISIBLE_CARDS = {"preflop": 0, "flop": 3, "turn": 4}


def _poker_new_game(uid, chat_id, bet):
    deck = _poker_make_deck()
    random.shuffle(deck)
    return {
        "uid": uid,
        "chat_id": chat_id,
        "bet": bet,
        "player": [deck.pop(), deck.pop()],
        "bot": [deck.pop(), deck.pop()],
        "community": [deck.pop() for _ in range(5)],
        "deck": deck,
        "stage": "preflop",
        "status": "playing",
        "result": None,
        "recorded": False,
    }

def _poker_visible_community(state):
    comm = state.get("community", [])
    return comm[:POKER_VISIBLE_CARDS.get(state.get("stage", "preflop"), len(comm))]

def _poker_render_text(state, reveal_bot=False):
    stage = state.get("stage", "preflop")
    player = state.get("player", [])
    bot_hand = state.get("bot", [])
    vis = _poker_visible_community(state)
    bet = state.get("bet", 10)

    player_str = " ".join(_poker_card_str(c) for c in player)
    bot_str = " ".join(_poker_card_str(c) for c in bot_hand) if reveal_bot else "?? ??"
    comm_str = " ".join(_poker_card_str(c) for c in vis) if vis else "—"

    stage_labels = {"preflop": "Префлоп", "flop": "Флоп", "turn": "Тёрн", "river": "Ривер", "showdown": "Вскрытие"}
    label = stage_labels.get(stage, stage)

    lines = [
        f"🃏 Покер  |  ставка: {bet} 🪙",
        f"📍 Стадия: {label}",
        f"🎴 Общие карты: {comm_str}",
        f"🙂 Вы: {player_str}",
        f"🤖 Бот: {bot_str}",
    ]

    if state.get("status") == "ended":
        result_map = {"wins": "🏆 Вы победили!", "losses": "💀 Бот победил.", "draws": "🤝 Ничья."}
        community = state.get("community", [])
        lines.append(result_map.get(state.get("result"), ""))
        lines.append(f"  Ваша комбинация: {_poker_hand_name(_poker_best_hand(player + community))}")
        lines.append(f"  Комбинация бота: {_poker_hand_name(_poker_best_hand(bot_hand + community))}")
        coins_change = bet if state["result"] == "wins" else (-bet if state["result"] == "losses" else 0)
        lines.append(f"  Монеты: {'+' if coins_change >= 0 else ''}{coins_change} 🪙")

    return "\n".join(lines)

def _poker_keyboard(gid, state):
    kb = types.InlineKeyboardMarkup()
    status = state.get("status")
    stage = state.get("stage", "preflop")
    if status == "playing":
        if stage != "showdown":
            kb.row(
                types.InlineKeyboardButton("➡️ Следующая стадия", callback_data=f"poker_next_{gid}"),
                types.InlineKeyboardButton("🏳️ Сдаться", callback_data=f"poker_fold_{gid}"),
            )
        else:
            kb.add(types.InlineKeyboardButton("🔍 Вскрыть карты", callback_data=f"poker_show_{gid}"))
    else:
        kb.row(
            types.InlineKeyboardButton("🔁 Снова (10🪙)", callback_data=f"poker_new_10_{gid}"),
            types.InlineKeyboardButton("🔁 Снова (50🪙)", callback_data=f"poker_new_50_{gid}"),
        )
    return kb

def _poker_advance_stage(state):
    idx = POKER_STAGES.index(state.get("stage", "preflop"))
    if idx < len(POKER_STAGES) - 1:
        state["stage"] = POKER_STAGES[idx + 1]
    return state

def _poker_resolve(state):
    comm = state.get("community", [])
    pr = _poker_best_hand(state.get("player", []) + comm)
    br = _poker_best_hand(state.get("bot", []) + comm)
    state["result"] = "wins" if pr > br else ("losses" if br > pr else "draws")
    state["status"] = "ended"
    return state

def _poker_apply_result(uid, state):
    if state.get("recorded"):
        return
    state["recorded"] = True
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(uid), {})
    rec = _ensure_profile_fields(rec)
    bet = int(state.get("bet", 10))
    coins = int(rec.get("coins", 0) or 0)
    result = state.get("result")
    if result == "wins":
        rec["coins"] = coins + bet
    elif result == "losses":
        rec["coins"] = max(0, coins - bet)
    rec["last_replay"] = {
        "game": "poker",
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": result,
        "bet": bet,
        "player_hand": [list(c) for c in state.get("player", [])],
        "bot_hand": [list(c) for c in state.get("bot", [])],
        "community": [list(c) for c in state.get("community", [])],
        "player_combo": _poker_hand_name(_poker_best_hand(state["player"] + state["community"])),
        "bot_combo": _poker_hand_name(_poker_best_hand(state["bot"] + state["community"])),
    }
    d["users"][str(uid)] = rec
    save_data(d)


@bot.message_handler(commands=["poker"])
def poker_cmd(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id):
        return
    parts = message.text.split()
    bet = max(1, min(500, int(parts[1]))) if len(parts) >= 2 and parts[1].isdigit() else 10
    rec, _ = _load_profile(uid)
    coins = int(rec.get("coins", 0) or 0)
    if coins < bet:
        bot.send_message(message.chat.id, f"❌ Недостаточно монет. У вас {coins}🪙, нужно {bet}🪙.")
        return
    gid = short_id()
    state = poker_games[gid] = _poker_new_game(uid, message.chat.id, bet)
    bot.send_message(message.chat.id, _poker_render_text(state), reply_markup=_poker_keyboard(gid, state))
    _record_game_play(uid, "poker", display_name=message.from_user.first_name or str(uid), session_id=f"poker_{gid}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("poker_"))
def poker_callback(call):
    try:
        parts = call.data.split("_")
        uid = call.from_user.id
        if len(parts) < 3:
            bot.answer_callback_query(call.id)
            return
        action = parts[1]

        if action == "new" and len(parts) >= 4:
            bet = int(parts[2])
            gid = parts[3]
            rec, _ = _load_profile(uid)
            if int(rec.get("coins", 0) or 0) < bet:
                bot.answer_callback_query(call.id, f"Недостаточно монет ({int(rec.get('coins',0))}🪙).")
                return
            chat_id = call.message.chat.id if call.message else None
            state = _poker_new_game(uid, chat_id, bet)
            poker_games[gid] = state
            safe_edit_message(call, _poker_render_text(state), reply_markup=_poker_keyboard(gid, state))
            _record_game_play(uid, "poker", display_name=call.from_user.first_name or str(uid), session_id=f"poker_{gid}")
            bot.answer_callback_query(call.id)
            return

        gid = parts[2]
        state = poker_games.get(gid)
        if not state:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        if state.get("uid") != uid:
            bot.answer_callback_query(call.id, "Это не ваша игра.")
            return
        if state.get("status") != "playing":
            bot.answer_callback_query(call.id, "Партия уже завершена.")
            return

        if action == "fold":
            state["result"] = "losses"
            state["status"] = "ended"
            state["stage"] = "showdown"
            _poker_apply_result(uid, state)
            _record_game_result(uid, "poker", "losses")
        elif action == "next":
            _poker_advance_stage(state)
        elif action == "show":
            _poker_resolve(state)
            _poker_apply_result(uid, state)
            _record_game_result(uid, "poker", state.get("result", "draws"))

        poker_games[gid] = state
        safe_edit_message(call, _poker_render_text(state, reveal_bot=state.get("status") == "ended"), reply_markup=_poker_keyboard(gid, state))
        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("poker_callback", e)
        try:
            bot.answer_callback_query(call.id, "Ошибка.")
        except Exception:
            pass


_IDUEL_MOVES = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
_RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

def _iduel_rps(m1, m2):
    """0 — ничья, 1 — победил первый, 2 — победил второй."""
    if m1 == m2:
        return 0
    return 1 if _RPS_BEATS[m1] == m2 else 2

def _iduel_score_line(st):
    p1, p2 = st["players"]
    return f"{st['names'].get(p1,'?')}: {st['scores'].get(p1,0)}  vs  {st['names'].get(p2,'?')}: {st['scores'].get(p2,0)}"

def _iduel_text(st):
    if st["status"] == "waiting":
        p1 = st["players"][0]
        return f"⚔️ Дуэль (KNB, 3 раунда)\n{st['names'].get(p1,'?')} ждёт соперника..."
    if st["status"] == "playing":
        rnd = st["round"]
        moved = [st["names"].get(u, "?") for u in st["players"] if u in st.get("moves", {})]
        waiting = [st["names"].get(u, "?") for u in st["players"] if u not in st.get("moves", {})]
        lines = [f"⚔️ Раунд {rnd}/3", _iduel_score_line(st)]
        if moved:
            lines.append(f"Сделали ход: {', '.join(moved)}")
        if waiting:
            lines.append(f"Ждём: {', '.join(waiting)}")
        return "\n".join(lines)
    return f"⚔️ Дуэль завершена\n{_iduel_score_line(st)}"

def _iduel_kb(gid, st):
    kb = types.InlineKeyboardMarkup()
    if st["status"] == "waiting":
        kb.add(types.InlineKeyboardButton("🤝 Присоединиться", callback_data=f"iduel_join_{gid}"))
    elif st["status"] == "playing":
        kb.row(
            types.InlineKeyboardButton("🪨", callback_data=f"iduel_move_{gid}_rock"),
            types.InlineKeyboardButton("📄", callback_data=f"iduel_move_{gid}_paper"),
            types.InlineKeyboardButton("✂️", callback_data=f"iduel_move_{gid}_scissors"),
        )
    return kb

@bot.callback_query_handler(func=lambda c: c.data.startswith("iduel_join_"))
def iduel_join(call):
    gid = call.data.split("_", 2)[2]
    uid = call.from_user.id
    st = inline_duel_games.get(gid)
    if not st:
        bot.answer_callback_query(call.id, "Дуэль не найдена.")
        return
    if uid in st["players"]:
        bot.answer_callback_query(call.id, "Вы уже участвуете.")
        return
    if len(st["players"]) >= 2:
        bot.answer_callback_query(call.id, "Места заняты.")
        return
    st["players"].append(uid)
    st["names"][uid] = call.from_user.first_name or str(uid)
    st["scores"][uid] = 0
    st["status"] = "playing"
    bot.answer_callback_query(call.id, "Вы в дуэли!")
    mid = call.inline_message_id
    if mid:
        bot.edit_message_text(_iduel_text(st), inline_message_id=mid, reply_markup=_iduel_kb(gid, st))
    elif call.message:
        safe_edit_message(call, _iduel_text(st), reply_markup=_iduel_kb(gid, st))

@bot.callback_query_handler(func=lambda c: c.data.startswith("iduel_move_"))
def iduel_move(call):
    parts = call.data.split("_", 3)
    if len(parts) < 4:
        bot.answer_callback_query(call.id)
        return
    gid, move = parts[2], parts[3]
    uid = call.from_user.id
    st = inline_duel_games.get(gid)
    if not st or st.get("status") != "playing":
        bot.answer_callback_query(call.id, "Дуэль не активна.")
        return
    if uid not in st["players"]:
        bot.answer_callback_query(call.id, "Вы не участник.")
        return
    if uid in st.get("moves", {}):
        bot.answer_callback_query(call.id, "Вы уже сделали ход.")
        return
    if move not in _IDUEL_MOVES:
        bot.answer_callback_query(call.id, "Неверный ход.")
        return
    st.setdefault("moves", {})[uid] = move
    bot.answer_callback_query(call.id, f"Ход принят: {_IDUEL_MOVES[move]}")

    mid = call.inline_message_id

    def _edit(text, kb=None):
        try:
            if mid:
                bot.edit_message_text(text, inline_message_id=mid, reply_markup=kb)
            elif call.message:
                safe_edit_message(call, text, reply_markup=kb)
        except Exception:
            pass

    if len(st["moves"]) < 2:
        _edit(_iduel_text(st), _iduel_kb(gid, st))
        return

    p1, p2 = st["players"]
    m1, m2 = st["moves"].get(p1), st["moves"].get(p2)
    res = _iduel_rps(m1, m2)
    round_line = (
        f"Раунд {st['round']}: "
        f"{st['names'].get(p1)} {_IDUEL_MOVES.get(m1,'?')} vs "
        f"{st['names'].get(p2)} {_IDUEL_MOVES.get(m2,'?')}"
    )
    if res == 1:
        st["scores"][p1] = st["scores"].get(p1, 0) + 1
        round_line += f" → {st['names'].get(p1)}"
    elif res == 2:
        st["scores"][p2] = st["scores"].get(p2, 0) + 1
        round_line += f" → {st['names'].get(p2)}"
    else:
        round_line += " → Ничья"

    st["moves"] = {}
    st["round"] = st.get("round", 1) + 1

    if st["round"] > 3:
        s1, s2 = st["scores"].get(p1, 0), st["scores"].get(p2, 0)
        winner = st["names"].get(p1) if s1 > s2 else (st["names"].get(p2) if s2 > s1 else None)
        outcome = f"🏆 Победитель: {winner}!" if winner else "🤝 Ничья!"
        st["status"] = "ended"
        inline_duel_games.pop(gid, None)
        _edit(f"⚔️ Дуэль завершена\n{round_line}\n{_iduel_score_line(st)}\n{outcome}")
    else:
        _edit(f"{round_line}\n\n⚔️ Раунд {st['round']}/3\n{_iduel_score_line(st)}", _iduel_kb(gid, st))


BJ_SUITS = ["♠", "♥", "♦", "♣"]
BJ_RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def _bj_make_deck():
    return [(r, s) for s in BJ_SUITS for r in BJ_RANKS]

def _bj_card_value(rank):
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11
    return int(rank)

def _bj_hand_value(hand):
    total = sum(_bj_card_value(r) for r, _ in hand)
    aces = sum(1 for r, _ in hand if r == "A")
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def _bj_card_str(card):
    return f"{card[0]}{card[1]}"

def _bj_render_text(state, reveal_dealer=False):
    player = state.get("player", [])
    dealer = state.get("dealer", [])
    player_val = _bj_hand_value(player)
    if reveal_dealer:
        dealer_line = f"🃏 Дилер: {' '.join(_bj_card_str(c) for c in dealer)} ({_bj_hand_value(dealer)})"
    else:
        dealer_line = f"🃏 Дилер: {_bj_card_str(dealer[0]) + ' ??' if dealer else '??'}"
    player_cards = " ".join(_bj_card_str(c) for c in player) if player else "—"
    return (
        "🃏 Блэкджек\n"
        f"{dealer_line}\n"
        f"🙂 Вы: {player_cards} ({player_val})"
    )

def _bj_keyboard(gid, status):
    kb = types.InlineKeyboardMarkup()
    if status == "playing":
        kb.row(
            types.InlineKeyboardButton("➕ Взять", callback_data=f"bj_hit_{gid}"),
            types.InlineKeyboardButton("🛑 Стоп", callback_data=f"bj_stand_{gid}")
        )
    else:
        kb.add(types.InlineKeyboardButton("🔁 Новая партия", callback_data=f"bj_new_{gid}"))
    return kb

def _bj_new_game(uid, chat_id):
    deck = _bj_make_deck()
    random.shuffle(deck)
    player = [deck.pop(), deck.pop()]
    dealer = [deck.pop(), deck.pop()]
    state = {
        "uid": uid,
        "chat_id": chat_id,
        "deck": deck,
        "player": player,
        "dealer": dealer,
        "status": "playing",
        "result": None,
        "recorded": False,
    }
    if _bj_hand_value(player) == 21:
        state["status"] = "ended"
        state["result"] = "draws" if _bj_hand_value(dealer) == 21 else "wins"
    return state

def _wordle_new_game(owner_id):
    return {
        "owner": owner_id,
        "target": random.choice(WORDLE_WORDS),
        "attempts": [],
        "current": "",
        "status": "playing",
    }

def _wordle_eval_guess(guess, target):
    marks = ["⬛"] * 5
    rem = {}
    for i in range(5):
        if guess[i] == target[i]:
            marks[i] = "🟩"
        else:
            rem[target[i]] = rem.get(target[i], 0) + 1
    for i in range(5):
        if marks[i] == "🟩":
            continue
        ch = guess[i]
        if rem.get(ch, 0) > 0:
            marks[i] = "🟨"
            rem[ch] -= 1
    return marks

def _wordle_render_text(game):
    lines = [f"{row['guess'].upper()}  {''.join(row['marks'])}" for row in game.get("attempts", [])]
    lines += ["_____  ⬜⬜⬜⬜⬜"] * (6 - len(lines))

    text = "🟩 Wordle\n\n"
    text += "\n".join(lines)
    text += f"\n\nТекущий ввод: {(game.get('current') or '').upper() or '_____'}"
    text += f"\nПопыток: {len(game.get('attempts', []))}/6"
    if game.get("status") == "won":
        text += "\n\n🎉 Победа! Вы угадали слово."
    elif game.get("status") == "lost":
        text += f"\n\n💀 Поражение. Слово: {game.get('target','').upper()}"
    else:
        text += "\n\nВведите слово из 5 букв и нажмите «✅ Готово»."
    return text

def _wordle_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup()
    if game.get("status") != "playing":
        kb.add(types.InlineKeyboardButton("🔄 Новая игра", callback_data=f"wrdl_new_{gid}"))
        return kb

    for row in ("йцукенгшщзх", "фывапролджэ", "ячсмитьбю"):
        kb.row(*[types.InlineKeyboardButton(ch.upper(), callback_data=f"wrdl_l_{gid}_{ch}") for ch in row])
    kb.row(
        types.InlineKeyboardButton("⌫", callback_data=f"wrdl_back_{gid}"),
        types.InlineKeyboardButton("✅ Готово", callback_data=f"wrdl_submit_{gid}")
    )
    return kb

def _chess_new_game(owner_id, owner_name=None):
    board = [
        ["br", "bn", "bb", "bq", "bk", "bb", "bn", "br"],
        ["bp", "bp", "bp", "bp", "bp", "bp", "bp", "bp"],
        [None, None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None],
        [None, None, None, None, None, None, None, None],
        ["wp", "wp", "wp", "wp", "wp", "wp", "wp", "wp"],
        ["wr", "wn", "wb", "wq", "wk", "wb", "wn", "wr"],
    ]
    return {
        "owner": owner_id,
        "p1": owner_id,
        "p1_name": owner_name or str(owner_id),
        "p2": None,
        "p2_name": None,
        "turn": "w",
        "board": board,
        "selected": None,
        "status": "waiting",
        "winner": None,
    }

def _chess_lost_counts(game):
    """Сколько фигур потеряли белые и чёрные."""
    pieces = [p for row in game["board"] for p in row if p]
    white_now = sum(1 for p in pieces if p[0] == "w")
    return 16 - white_now, 16 - (len(pieces) - white_now)

CHESS_PIECE_EMOJI = {
    "wp": "♙", "wr": "♖", "wn": "♘", "wb": "♗", "wq": "♕", "wk": "♔",
    "bp": "♟", "br": "♜", "bn": "♞", "bb": "♝", "bq": "♛", "bk": "♚",
}

def _chess_piece_emoji(piece):
    return CHESS_PIECE_EMOJI.get(piece, "·")

def _chess_in_bounds(r, c):
    return 0 <= r < 8 and 0 <= c < 8

def _chess_get_player_color(game, user_id):
    if user_id == game.get("p1"):
        return "w"
    if user_id == game.get("p2"):
        return "b"
    return None

def _chess_legal_moves(board, r, c):
    piece = board[r][c]
    if not piece:
        return []
    color = piece[0]
    kind = piece[1]
    enemy = "b" if color == "w" else "w"
    moves = []

    def add_line(dr, dc):
        nr, nc = r + dr, c + dc
        while _chess_in_bounds(nr, nc):
            target = board[nr][nc]
            if target is None:
                moves.append((nr, nc))
            else:
                if target[0] == enemy:
                    moves.append((nr, nc))
                break
            nr += dr
            nc += dc

    if kind == "p":
        step = -1 if color == "w" else 1
        start_row = 6 if color == "w" else 1
        nr = r + step
        if _chess_in_bounds(nr, c) and board[nr][c] is None:
            moves.append((nr, c))
            nr2 = r + 2 * step
            if r == start_row and _chess_in_bounds(nr2, c) and board[nr2][c] is None:
                moves.append((nr2, c))
        for dc in (-1, 1):
            nc = c + dc
            if _chess_in_bounds(nr, nc) and board[nr][nc] is not None and board[nr][nc][0] == enemy:
                moves.append((nr, nc))
    elif kind == "n":
        for dr, dc in [(-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1)]:
            nr, nc = r + dr, c + dc
            if not _chess_in_bounds(nr, nc):
                continue
            target = board[nr][nc]
            if target is None or target[0] == enemy:
                moves.append((nr, nc))
    elif kind in ("b", "r", "q"):
        diagonals = ((1, 1), (1, -1), (-1, 1), (-1, -1))
        straights = ((1, 0), (-1, 0), (0, 1), (0, -1))
        directions = diagonals if kind == "b" else straights if kind == "r" else diagonals + straights
        for dr, dc in directions:
            add_line(dr, dc)
    elif kind == "k":
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if not _chess_in_bounds(nr, nc):
                    continue
                target = board[nr][nc]
                if target is None or target[0] == enemy:
                    moves.append((nr, nc))
    return moves

def _chess_apply_move(game, fr, fc, tr, tc):
    board = game["board"]
    piece = board[fr][fc]
    target = board[tr][tc]
    board[tr][tc] = piece
    board[fr][fc] = None
    if piece in ("wp", "bp") and tr in (0, 7):
        board[tr][tc] = piece[0] + "q"
    if target in ("wk", "bk"):
        game["status"] = "ended"
        game["winner"] = piece[0]
    else:
        game["turn"] = "b" if game["turn"] == "w" else "w"
    game["selected"] = None

def _chess_render_text(game):
    board = game["board"]
    w_lost, b_lost = _chess_lost_counts(game)
    p1_name = game.get("p1_name") or str(game.get("p1", "Игрок 1"))
    p2_name = game.get("p2_name") or (str(game.get("p2")) if game.get("p2") else "ожидается")
    lines = [f"{8 - r} " + " ".join(_chess_piece_emoji(board[r][c]) for c in range(8)) for r in range(8)]
    lines.append("  a b c d e f g h")
    text = "♟ Шахматы\n\n"
    text += f"Белые: {p1_name} | Цвет: белый | Потеряно фигур: {w_lost}\n"
    text += f"Черные: {p2_name} | Цвет: черный | Потеряно фигур: {b_lost}\n\n"
    text += "\n".join(lines)

    if game.get("status") == "waiting":
        text += "\n\nОжидание второго игрока."
    elif game.get("status") == "ended":
        winner = "Белые" if game.get("winner") == "w" else "Черные"
        text += f"\n\nПобеда: {winner}"
    else:
        turn_name = "Белые" if game.get("turn") == "w" else "Черные"
        text += f"\n\nХод: {turn_name}"
    return text

def _chess_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=8)
    if game.get("status") == "waiting":
        kb.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"chess_join_{gid}"))
        return kb
    if game.get("status") == "ended":
        kb.add(types.InlineKeyboardButton("Новая партия", callback_data=f"chess_new_{gid}"))
        return kb
    selected = game.get("selected")
    legal = set()
    if selected:
        sr, sc = selected
        legal = set(_chess_legal_moves(game["board"], sr, sc))
    for r in range(8):
        row = []
        for c in range(8):
            piece = game["board"][r][c]
            mark = _chess_piece_emoji(piece)
            if selected == (r, c):
                mark = "🔷"
            elif (r, c) in legal:
                mark = "🟩"
            row.append(types.InlineKeyboardButton(mark, callback_data=f"chess_c_{gid}_{r}_{c}"))
        kb.row(*row)
    kb.add(types.InlineKeyboardButton("Сброс выбора", callback_data=f"chess_reset_{gid}"))
    return kb

def snake_controls():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("⬆️", callback_data="snake_up"))
    kb.row(types.InlineKeyboardButton("⬅️", callback_data="snake_left"),
           types.InlineKeyboardButton("➡️", callback_data="snake_right"))
    kb.row(types.InlineKeyboardButton("⬇️", callback_data="snake_down"))
    return kb

def telos_main_menu():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📁 Файлы", callback_data="os_files"),
           types.InlineKeyboardButton("📝 Заметки", callback_data="os_notes"))
    kb.add(types.InlineKeyboardButton("🎮 Игры", callback_data="os_games"),
           types.InlineKeyboardButton("💬 Терминал", callback_data="os_terminal"))
    kb.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data="os_settings"))
    kb.add(types.InlineKeyboardButton("⏻ Выключить", callback_data="os_shutdown"))
    return kb

def _telos_default_state():
    return {
        "booted": True,
        "settings": {"os_name": "TELOS", "theme": "classic"},
        "files": [{"name": "readme.txt", "content": "Добро пожаловать в TELOS! Эта система разработана для демонстрации возможностей бота. Вы можете создавать свои файлы и заметки, а также играть в мини-игры. Наслаждайтесь! :)"}],
        "notes": [],
        "terminal_history": [],
        "mini_games": {"guess_target": None},
        "created_at": int(time.time()),
    }

def _telos_get_state(user_id):
    data = load_data()
    users = data.setdefault("users", {})
    user = users.setdefault(str(user_id), {})
    state = user.get("telos")
    if not isinstance(state, dict):
        state = _telos_default_state()
    state.setdefault("booted", True)
    state.setdefault("settings", {})
    state["settings"].setdefault("os_name", "TELOS")
    state["settings"].setdefault("theme", "classic")
    state.setdefault("files", [{"name": "readme.txt", "content": "Добро пожаловать в TELOS"}])
    state.setdefault("notes", [])
    state.setdefault("terminal_history", [])
    state.setdefault("mini_games", {"guess_target": None})
    state.setdefault("created_at", int(time.time()))
    user["telos"] = state
    users[str(user_id)] = user
    save_data(data)
    return state

def _telos_save_state(user_id, state):
    data = load_data()
    users = data.setdefault("users", {})
    user = users.setdefault(str(user_id), {})
    user["telos"] = state
    users[str(user_id)] = user
    save_data(data)

def _telos_home_text(user_id):
    st = _telos_get_state(user_id)
    return (
        f"🖥 *{st['settings'].get('os_name', 'TELOS')} v1.1*\n"
        f"👤 ID пользователя: `{user_id}`\n\n"
        f"📁 Файлов: {len(st.get('files', []))}\n"
        f"📝 Заметок: {len(st.get('notes', []))}\n"
        f"🎨 Тема: {st['settings'].get('theme', 'classic')}\n\n"
        "Выбирайте приложение:"
    )

def _telos_files_kb(st):
    kb = types.InlineKeyboardMarkup()
    for i, fobj in enumerate(st.get("files", [])[:6]):
        kb.add(types.InlineKeyboardButton(f"📄 {str(fobj.get('name', 'file.txt'))[:24]}", callback_data=f"os_file_{i}"))
    kb.row(
        types.InlineKeyboardButton("➕ Добавить", callback_data="os_files_new"),
        types.InlineKeyboardButton("🧹 Очистить", callback_data="os_files_clear"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="os_back"))
    return kb

def _telos_notes_kb(st):
    kb = types.InlineKeyboardMarkup()
    for i, note in enumerate(st.get("notes", [])[:6]):
        kb.add(types.InlineKeyboardButton(f"🗒 {str(note)[:24]}", callback_data=f"os_note_{i}"))
    kb.row(
        types.InlineKeyboardButton("➕ Добавить", callback_data="os_notes_add"),
        types.InlineKeyboardButton("🧹 Очистить", callback_data="os_notes_clear"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="os_back"))
    return kb

def _telos_terminal_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("❓ Помощь", callback_data="os_term_help"),
        types.InlineKeyboardButton("🕒 Дата", callback_data="os_term_date"),
        types.InlineKeyboardButton("⏱ Аптайм", callback_data="os_term_uptime"),
    )
    kb.row(
        types.InlineKeyboardButton("📁 Файлы", callback_data="os_term_ls"),
        types.InlineKeyboardButton("🧹 Очистить", callback_data="os_term_clear"),
        types.InlineKeyboardButton("⌨️ Ввести", callback_data="os_term_input"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="os_back"))
    return kb

def _telos_settings_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("✏️ Имя ОС", callback_data="os_set_name"),
        types.InlineKeyboardButton("🎨 Тема", callback_data="os_set_theme"),
    )
    kb.row(
        types.InlineKeyboardButton("♻️ Сброс", callback_data="os_set_reset"),
        types.InlineKeyboardButton("⬅️ Назад", callback_data="os_back"),
    )
    return kb

def _telos_games_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🪙 Монетка", callback_data="os_game_coin"),
        types.InlineKeyboardButton("🎰 Слот", callback_data="os_game_slot"),
    )
    kb.row(
        types.InlineKeyboardButton("✂ КНБ", callback_data="os_game_rps"),
        types.InlineKeyboardButton("🔢 Угадай число", callback_data="os_game_guess"),
    )
    kb.add(types.InlineKeyboardButton("🚪 Кубик", callback_data="os_game_dice"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="os_back"))
    return kb

def _telos_rps_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🪨", callback_data="os_game_rps_rock"),
        types.InlineKeyboardButton("📄", callback_data="os_game_rps_paper"),
        types.InlineKeyboardButton("✂️", callback_data="os_game_rps_scissors"),
    )
    kb.add(types.InlineKeyboardButton("⬅️ Назад к играм", callback_data="os_games"))
    return kb

def _number_grid(kb, callback_prefix, per_row=5):
    row = []
    for i in range(1, 11):
        row.append(types.InlineKeyboardButton(str(i), callback_data=f"{callback_prefix}{i}"))
        if i % per_row == 0:
            kb.row(*row)
            row = []
    return kb


def _telos_guess_kb():
    kb = _number_grid(types.InlineKeyboardMarkup(), "os_game_guess_pick_")
    kb.add(types.InlineKeyboardButton("⬅️ Назад к играм", callback_data="os_games"))
    return kb

def _telos_run_command(st, cmd):
    cmd = (cmd or "").strip().lower()
    alias = {
        "помощь": "help",
        "дата": "date",
        "аптайм": "uptime",
        "файлы": "ls",
        "очистить": "clear",
        "ктоя": "whoami",
        "заметки": "notes",
    }
    cmd = alias.get(cmd, cmd)
    if cmd == "help":
        return "Команды: help/помощь, date/дата, uptime/аптайм, ls/файлы, notes/заметки, whoami/ктоя, clear/очистить"
    if cmd == "date":
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if cmd == "uptime":
        return f"{max(0, int(time.time()) - int(st.get('created_at', int(time.time()))))} сек."
    if cmd == "ls":
        files = [x.get("name", "file.txt") for x in st.get("files", [])]
        return "\n".join(files) if files else "(пусто)"
    if cmd == "notes":
        notes = st.get("notes", [])
        return "\n".join([f"{i+1}. {str(n)[:60]}" for i, n in enumerate(notes[:8])]) if notes else "(нет заметок)"
    if cmd == "whoami":
        return "пользователь"
    if cmd == "clear":
        st["terminal_history"] = []
        return "История очищена."
    return "Команда не найдена. Введите help."

def ask_ai(prompt: str, user_id: int) -> str:
    if not prompt.strip():
        return "⚠️ Напишите вопрос текстом"
    if not nvmapi_client:
        return "⚠️ AI временно недоступен: не задан NVMAPI_KEY."

    mode = user_ai_mode.get(user_id, "chat")
    system_prompt = AI_MODES.get(mode, AI_MODES["chat"])

    def _is_retryable_ai_error(err: Exception) -> bool:
        msg = str(err).lower()
        retry_markers = (
            "client_responce_parse_failed",
            "client_response_parse_failed",
            "timeout",
            "timed out",
            "connection",
            "temporar",
            "429",
            "rate limit",
            "service unavailable",
            "bad gateway",
        )
        return any(marker in msg for marker in retry_markers)

    last_err = None
    for attempt in range(3):
        try:
            chat = nvmapi_client.chat.completions.create(
                model=NVMAPI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt[:2000]}
                ],
                temperature=0.7,
                max_tokens=900
            )
            return chat.choices[0].message.content
        except Exception as e:
            last_err = e
            LOGGER.warning("AI ERROR attempt %s/3: %r", attempt + 1, e)
            if attempt < 2 and _is_retryable_ai_error(e):
                time.sleep(1.2 + attempt)
                continue
            break

    LOGGER.error("AI FINAL ERROR: %r", last_err)
    return "❌ Временная ошибка AI-сервиса. Нажмите «Обновить» или «Получить ответ» ещё раз."

def _user_display_name_from_id(uid):
    try:
        u = bot.get_chat(uid)
        return u.username or u.first_name or f"Player_{uid}"
    except Exception:
        return f"Player_{uid}"

TTT_X = "❌ "
TTT_SYMBOLS = {" ": "⬜️", TTT_X: TTT_X, "⭕": "⭕️"}
TTT_WIN_PATTERNS = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6),
)


def ttt_render_header(game):
    p1_id, p2_id = game["players"][0], game["players"][1]
    p1_name = game["names"].get(p1_id, _user_display_name_from_id(p1_id))
    p2_name = game["names"].get(p2_id, _user_display_name_from_id(p2_id))
    score1 = game["scores"].get(p1_id, 0)
    score2 = game["scores"].get(p2_id, 0)
    line1 = f"{TTT_X} {p1_name} — {score1}"
    line2 = f"⭕ {p2_name} — {score2}"
    turn_symbol = TTT_X if game["turn"] == p1_id else "⭕"
    return f"{line1}\n{line2}\n\nХодит: {turn_symbol}\n\n"

def ttt_render_board(board):
    return "\n".join(
        " ".join(board[r * 3 + c] if board[r * 3 + c].strip() else "⬜️" for c in range(3))
        for r in range(3)
    )

def ttt_build_keyboard(gid, board):
    kb = types.InlineKeyboardMarkup()
    for r in range(3):
        kb.row(*[
            types.InlineKeyboardButton(TTT_SYMBOLS.get(board[r * 3 + c], "⬜️"), callback_data=f"ttt_move_{gid}_{r * 3 + c}")
            for c in range(3)
        ])
    kb.row(types.InlineKeyboardButton("🔁 Сыграть ещё", callback_data=f"ttt_restart_{gid}"))
    return kb


def _find_player_name(user=None, uid=None):
    if user is not None:
        return user.first_name or user.username or f"Player_{user.id}"
    if uid is not None:
        return _user_display_name_from_id(uid)
    return "Игрок"


def _find_active_match_for_user(uid):
    for match_id, match in find_matches.items():
        if uid in match.get("players", []) and match.get("status") == "voting":
            return match_id, match
    return None, None


def _find_prune_queue():
    now = time.time()
    for uid, entry in list(find_queue.items()):
        if now - float(entry.get("started_at", 0) or 0) > FIND_ONLINE_TTL:
            find_queue.pop(uid, None)


def _find_waiting_text(uid):
    _find_prune_queue()
    online = len(find_queue)
    you = find_queue.get(uid, {})
    started_at = int(you.get("started_at", time.time()) or time.time())
    waited = max(0, int(time.time()) - started_at)
    return (
        "🔎 Поиск игрока\n\n"
        f"Игроков онлайн в поиске: {online}\n"
        f"Ваш статус: ищем соперника\n"
        f"Ожидание: {waited} сек.\n\n"
        "Как только найдется второй игрок, бот запустит голосование за игру."
    )


def _find_waiting_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("❌ Отменить поиск", callback_data="find_cancel"))
    return kb


def _find_vote_text(match, chosen_game=None):
    p1, p2 = match["players"]
    n1 = match["names"].get(p1, "Игрок 1")
    n2 = match["names"].get(p2, "Игрок 2")
    lines = [
        "🎮 Игрок найден!",
        "",
        f"{n1} vs {n2}",
        "",
    ]
    if chosen_game:
        lines.append(f"Выбрана игра: {GAME_TITLES.get(chosen_game, chosen_game)}")
        lines.append("")
        lines.append("Запускаю матч в личных сообщениях.")
        return "\n".join(lines)

    lines.append("Голосование за игру:")
    for game_key in match.get("options", []):
        count = sum(1 for vote in match.get("votes", {}).values() if vote == game_key)
        lines.append(f"- {GAME_TITLES.get(game_key, game_key)}: {count}")
    lines.append("")
    lines.append(f"У вас есть {FIND_VOTE_SECONDS} сек. на выбор.")
    return "\n".join(lines)


def _find_vote_kb(match_id, options):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for game_key in options:
        kb.add(types.InlineKeyboardButton(GAME_TITLES.get(game_key, game_key), callback_data=f"find_vote_{match_id}_{game_key}"))
    return kb


def _find_refresh_vote_messages(match_id, chosen_game=None):
    match = find_matches.get(match_id)
    if not match:
        return
    reply_markup = None if chosen_game else _find_vote_kb(match_id, match.get("options", []))
    text = _find_vote_text(match, chosen_game=chosen_game)
    store = match.setdefault("vote_messages", {})
    for uid in match.get("players", []):
        _send_or_edit_tracked(store, uid, text, reply_markup, context="find_vote_message")


def _pm_ttt_new_game(p1, p2, p1_name, p2_name):
    first_turn = random.choice([p1, p2])
    return {
        "players": [p1, p2],
        "names": {p1: p1_name, p2: p2_name},
        "scores": {p1: 0, p2: 0},
        "board": [" "] * 9,
        "turn": first_turn,
        "status": "playing",
        "winner": None,
        "pm": {},
        "session_recorded": False,
        "results_recorded": False,
    }


def _pm_ttt_render_text(game):
    p1, p2 = game["players"]
    n1 = game["names"].get(p1, "Игрок 1")
    n2 = game["names"].get(p2, "Игрок 2")
    score1 = game["scores"].get(p1, 0)
    score2 = game["scores"].get(p2, 0)
    lines = [
        f"{TTT_X} Крестики-нолики",
        "",
        f"{TTT_X} {n1} — {score1}",
        f"⭕ {n2} — {score2}",
        "",
    ]
    if game.get("status") == "ended":
        winner = game.get("winner")
        if winner == "draw":
            lines.append("Итог: ничья")
        else:
            symbol = TTT_X if winner == p1 else "⭕"
            lines.append(f"Победил: {symbol} {game['names'].get(winner, 'Игрок')}")
    else:
        turn_symbol = TTT_X if game.get("turn") == p1 else "⭕"
        lines.append(f"Ходит: {turn_symbol} {game['names'].get(game.get('turn'), 'Игрок')}")
    lines.append("")
    lines.append(ttt_render_board(game["board"]))
    return "\n".join(lines)


def _pm_ttt_keyboard(gid, game, viewer_id):
    kb = types.InlineKeyboardMarkup()
    if viewer_id not in game.get("players", []):
        return kb
    if game.get("status") == "ended":
        kb.add(types.InlineKeyboardButton("🔁 Новая партия", callback_data=f"pmttt_new_{gid}"))
        return kb
    active_turn = viewer_id == game.get("turn")
    board = game["board"]
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            cb = f"pmttt_move_{gid}_{idx}" if active_turn and board[idx] == " " else "none"
            row.append(types.InlineKeyboardButton(TTT_SYMBOLS.get(board[idx], "⬜️"), callback_data=cb))
        kb.row(*row)
    if not active_turn:
        kb.add(types.InlineKeyboardButton("⏳ Ход соперника", callback_data="none"))
    return kb


def _sync_two_player_views(game, render, keyboard, players, context):
    """Обновляет личные экраны обоих игроков; возвращает (ок1, ок2)."""
    pm = game.setdefault("pm", {})
    results = []
    for uid in players:
        if uid is None:
            results.append(False)
            continue
        results.append(_send_or_edit_tracked(pm, uid, render(uid), keyboard(uid), context=context))
    return tuple(results)


def _pm_ttt_sync_views(gid, game):
    return _sync_two_player_views(
        game,
        lambda uid: _pm_ttt_render_text(game),
        lambda uid: _pm_ttt_keyboard(gid, game, uid),
        game.get("players", [None, None]),
        "pm_ttt_view",
    )


def _pm_ttt_record_session(gid, game):
    if game.get("session_recorded"):
        return
    game["session_recorded"] = True
    for uid in game.get("players", []):
        _record_game_play_once(uid, "ttt", f"pmttt_{gid}", display_name=game["names"].get(uid))


def _pm_ttt_record_results(game):
    if game.get("results_recorded"):
        return
    game["results_recorded"] = True
    winner = game.get("winner")
    for uid in game.get("players", []):
        if winner == "draw":
            _record_game_result(uid, "ttt", "draws")
        elif winner == uid:
            _record_game_result(uid, "ttt", "wins")
        else:
            _record_game_result(uid, "ttt", "losses")


def _chess_sync_private_views(gid, game):
    return _sync_two_player_views(
        game,
        lambda uid: _chess_render_text(game),
        lambda uid: _chess_keyboard(gid, game),
        (game.get("p1"), game.get("p2")),
        "chess_private_view",
    )


def _chess_refresh_views(gid, game, call=None):
    if game.get("private_mode"):
        _chess_sync_private_views(gid, game)
    elif call is not None:
        safe_edit_message(call, _chess_render_text(game), reply_markup=_chess_keyboard(gid, game))


def _private_chess_new_game(p1, p2, p1_name, p2_name):
    game = _chess_new_game(p1, p1_name)
    game["p2"] = p2
    game["p2_name"] = p2_name
    game["status"] = "playing"
    game["private_mode"] = True
    game["pm"] = {}
    return game


def _notify_launch_failure(players):
    for uid in players:
        try:
            bot.send_message(uid, "Не удалось открыть поле. Нажмите /start и попробуйте /find еще раз.")
        except Exception:
            pass


def _launch_private_ttt(match):
    p1, p2 = match["players"]
    gid = short_id()
    game = pm_ttt_games[gid] = _pm_ttt_new_game(
        p1, p2, match["names"].get(p1, "Игрок 1"), match["names"].get(p2, "Игрок 2")
    )
    _pm_ttt_record_session(gid, game)
    return gid, all(_pm_ttt_sync_views(gid, game))


def _launch_private_bship(match):
    p1, p2 = match["players"]
    name1 = match["names"].get(p1, "Игрок 1")
    name2 = match["names"].get(p2, "Игрок 2")
    gid = short_id()
    game = _bship_new_game(p1, name1)
    game["p2"] = p2
    game["p2_name"] = name2
    game["ships"][p2] = _bship_random_ships(game["size"], game["ships_count"])
    game["shots"][p2] = set()
    game["status"] = "playing"
    game["turn"] = random.choice([p1, p2])
    game["private_mode"] = True
    game["pm"] = {}
    battleship_games[gid] = game
    _record_game_play_once(p1, "bship", f"find_bship_{gid}", display_name=name1)
    _record_game_play_once(p2, "bship", f"find_bship_{gid}", display_name=name2)
    return gid, all(_bship_sync_views(gid, game))


def _launch_private_chess(match):
    p1, p2 = match["players"]
    gid = short_id()
    game = chess_games[gid] = _private_chess_new_game(
        p1, p2, match["names"].get(p1, "Игрок 1"), match["names"].get(p2, "Игрок 2")
    )
    _record_game_play_once(p1, "chess", f"find_chess_{gid}", display_name=match["names"].get(p1))
    _record_game_play_once(p2, "chess", f"find_chess_{gid}", display_name=match["names"].get(p2))
    return gid, all(_chess_sync_private_views(gid, game))


def _find_launch_game(match_id, chosen_game):
    match = find_matches.get(match_id)
    if not match:
        return
    launcher = {"ttt": _launch_private_ttt, "bship": _launch_private_bship}.get(chosen_game, _launch_private_chess)
    gid, ok = launcher(match)
    if not ok:
        _notify_launch_failure(match["players"])
    match["status"] = "playing"
    match["game_id"] = gid


def _find_finalize_vote(match_id):
    match = find_matches.get(match_id)
    if not match or match.get("status") != "voting":
        return
    match["status"] = "finalizing"
    counts = {game_key: 0 for game_key in match.get("options", [])}
    for vote in match.get("votes", {}).values():
        if vote in counts:
            counts[vote] += 1
    max_votes = max(counts.values()) if counts else 0
    leaders = [game_key for game_key, value in counts.items() if value == max_votes] if counts else list(MATCHMAKING_GAMES)
    chosen_game = random.choice(leaders) if leaders else "ttt"
    match["chosen_game"] = chosen_game
    _find_refresh_vote_messages(match_id, chosen_game=chosen_game)
    _find_launch_game(match_id, chosen_game)


def _find_schedule_vote_finalize(match_id):
    def finalize():
        time.sleep(FIND_VOTE_SECONDS)
        _find_finalize_vote(match_id)
    Thread(target=finalize, daemon=True).start()


def _find_create_match(entry1, entry2):
    match_id = short_id()
    p1 = int(entry1["uid"])
    p2 = int(entry2["uid"])
    match = {
        "players": [p1, p2],
        "names": {
            p1: entry1.get("name", f"Player_{p1}"),
            p2: entry2.get("name", f"Player_{p2}"),
        },
        "options": list(MATCHMAKING_GAMES),
        "votes": {},
        "vote_messages": {},
        "status": "voting",
        "created_at": time.time(),
    }
    find_matches[match_id] = match
    _find_refresh_vote_messages(match_id)
    _find_schedule_vote_finalize(match_id)
    return match_id


def _find_try_match_players():
    _find_prune_queue()
    while len(find_queue) >= 2:
        first, second = sorted(
            ({"uid": uid, **entry} for uid, entry in find_queue.items()),
            key=lambda item: float(item.get("started_at", 0) or 0),
        )[:2]
        find_queue.pop(first["uid"], None)
        find_queue.pop(second["uid"], None)
        _find_create_match(first, second)

def mafia_role_counts(n_players):
    mafia_cnt = 1 if n_players < 7 else 2
    doctor_cnt = 1 if n_players >= 5 else 0
    detective_cnt = 1 if n_players >= 6 else 0
    civ_cnt = n_players - mafia_cnt - doctor_cnt - detective_cnt
    return mafia_cnt, doctor_cnt, detective_cnt, civ_cnt

MAFIA_ROLE_NAMES = {"mafia": "мафия", "doctor": "доктор", "detective": "детектив", "citizen": "мирный"}


def mafia_new_game(host_id, host_name):
    return {
        "owner": host_id,
        "players": [host_id],
        "alive": [host_id],
        "names": {host_id: host_name},
        "roles": {},
        "phase": "lobby",
        "round": 1,
        "night": {"kill": None, "heal": None, "check": None},
        "votes": {},
        "last_event": "Лобби создано.",
    }


def mafia_assign_roles(players):
    p = players[:]
    random.shuffle(p)
    mafia_cnt, doctor_cnt, detective_cnt, _ = mafia_role_counts(len(players))
    order = ["mafia"] * mafia_cnt + ["doctor"] * doctor_cnt + ["detective"] * detective_cnt
    order += ["citizen"] * (len(p) - len(order))
    return dict(zip(p, order))

def mafia_alive_mafia_count(game):
    return sum(1 for uid in game["alive"] if game["roles"].get(uid) == "mafia")

def mafia_check_winner(game):
    mafia_left = mafia_alive_mafia_count(game)
    citizens_left = len(game["alive"]) - mafia_left
    if mafia_left <= 0:
        return "citizens"
    if mafia_left >= citizens_left:
        return "mafia"
    return None

def mafia_render_text(game):
    phase_title = {
        "lobby": "🎭 Мафия - Лобби",
        "night": "🌙 Мафия - Ночь",
        "day": "☀️ Мафия - День",
        "ended": "🏁 Мафия - Конец игры",
    }.get(game.get("phase"), "🎭 Мафия")
    text = f"{phase_title}\n\n"
    text += f"Раунд: {game.get('round', 1)}\n"
    text += f"Игроки: {len(game.get('players', []))} (живых: {len(game.get('alive', []))})\n\n"
    text += "Живые игроки:\n"
    for uid in game.get("alive", []):
        text += f"- {game['names'].get(uid, 'Игрок')}\n"
    if game.get("last_event"):
        text += f"\n{game['last_event']}"
    hints = {
        "lobby": "\n\nНужно 4-10 игроков. Создатель нажимает «Старт».",
        "night": "\n\nНочные роли делают действия. Нажмите «Моя роль», чтобы посмотреть роль.",
        "day": "\n\nДневное голосование: выберите подозреваемого.",
    }
    return text + hints.get(game.get("phase"), "")

def mafia_build_lobby_kb(gid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Присоединиться", callback_data=f"mafia_join_{gid}"))
    kb.add(types.InlineKeyboardButton("▶️ Старт", callback_data=f"mafia_start_{gid}"))
    kb.add(types.InlineKeyboardButton("🎭 Моя роль", callback_data=f"mafia_role_{gid}"))
    return kb

def mafia_build_night_kb(gid, game):
    kb = types.InlineKeyboardMarkup()
    for label, action, skip_mafia in (("🔪 Убить", "nkill", True), ("💊 Лечить", "heal", False), ("🕵️ Проверить", "check", False)):
        for uid in game.get("alive", []):
            if skip_mafia and game["roles"].get(uid) == "mafia":
                continue
            kb.add(types.InlineKeyboardButton(f"{label}: {game['names'].get(uid, 'Игрок')}", callback_data=f"mafia_{action}_{gid}_{uid}"))
    kb.add(types.InlineKeyboardButton("🎭 Моя роль", callback_data=f"mafia_role_{gid}"))
    return kb

def mafia_build_day_kb(gid, game):
    kb = types.InlineKeyboardMarkup()
    for uid in game.get("alive", []):
        kb.add(types.InlineKeyboardButton(f"🗳 Голос: {game['names'].get(uid,'Игрок')}", callback_data=f"mafia_vote_{gid}_{uid}"))
    kb.add(types.InlineKeyboardButton("🎭 Моя роль", callback_data=f"mafia_role_{gid}"))
    return kb

def mafia_resolve_night(game):
    target = game["night"].get("kill")
    if target and target in game["alive"] and target != game["night"].get("heal"):
        game["alive"].remove(target)
        game["last_event"] = f"🌙 Ночью убит: {game['names'].get(target,'Игрок')}"
    else:
        game["last_event"] = "🌙 Ночью никто не погиб."
    game["phase"] = "day"
    game["votes"] = {}
    game["night"] = {"kill": None, "heal": None, "check": None}

def mafia_resolve_day(game):
    tally = {}
    for target in game.get("votes", {}).values():
        tally[target] = tally.get(target, 0) + 1
    if not tally:
        game["last_event"] = "☀️ Голосов нет. Никто не изгнан."
    else:
        max_votes = max(tally.values())
        top = [uid for uid, v in tally.items() if v == max_votes]
        if len(top) != 1:
            game["last_event"] = "☀️ Ничья в голосовании. Никто не изгнан."
        else:
            out_uid = top[0]
            if out_uid in game["alive"]:
                game["alive"].remove(out_uid)
            role_ru = MAFIA_ROLE_NAMES.get(game["roles"].get(out_uid, "citizen"), "мирный")
            game["last_event"] = f"☀️ Изгнан: {game['names'].get(out_uid,'Игрок')} ({role_ru})."
    game["phase"] = "night"
    game["round"] += 1
    game["votes"] = {}
    game["night"] = {"kill": None, "heal": None, "check": None}


def _complete_onboarding(uid):
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(uid), {})
    rec = _ensure_profile_fields(rec)
    rec["onboarding_completed"] = True
    d["users"][str(uid)] = rec
    save_data(d)


def _send_home_menu(message):
    uid = message.from_user.id
    display_name = message.from_user.first_name or message.from_user.username or str(uid)
    update_user_streak(uid, display_name)

    user = get_user(uid)
    data = load_data()
    data["users"][str(uid)]["started"] = True
    data["users"][str(uid)]["display_name"] = display_name[:64]
    save_data(data)

    if REQUIRED_CHANNEL and not is_user_subscribed(uid):
        bot.send_message(
            message.chat.id,
            "⚠️ Подпишитесь на канал, чтобы использовать этого бота.",
            reply_markup=_subscription_keyboard(),
        )
        return

    show_main_menu(message.chat.id, uid)

    d2 = load_data()
    rec = _ensure_profile_fields(d2.setdefault("users", {}).setdefault(str(uid), {}))
    if not rec.get("onboarding_completed"):
        bot.send_message(message.chat.id, _render_onboarding_text(uid))
        _complete_onboarding(uid)


@bot.message_handler(commands=["start"])
def start(message):
    if not _guard_user(message.from_user.id, chat_id=message.chat.id, action="start", require_subscription=False):
        return
    _send_home_menu(message)

@bot.message_handler(commands=["topusers"])
def topusers_cmd(message):
    uid = message.from_user.id
    update_user_streak(uid, message.from_user.first_name or message.from_user.username or str(uid))

    d = load_data()
    users = d.get("users", {})
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    rows = []
    for uid_str, rec in users.items():
        if not isinstance(rec, dict):
            continue
        streak = int(rec.get("streak_current", 0) or 0)
        last_day = rec.get("streak_last_day")
        if streak <= 0:
            continue
        # "Не сбивается серия": активность сегодня или вчера.
        if last_day not in (today, yesterday):
            continue
        name = rec.get("display_name") or f"user_{uid_str}"
        rows.append((streak, name, last_day))

    if not rows:
        bot.send_message(message.chat.id, "Пока нет активных серий. Начните использовать бота ежедневно.")
        return

    rows.sort(key=lambda x: (-x[0], x[1].lower()))
    top = rows[:15]
    text = "🏆 *Топ пользователей по серии*\n"
    text += "_Серия считается по дням активности в боте._\n\n"
    for i, (streak, name, last_day) in enumerate(top, 1):
        status = "✅ сегодня" if last_day == today else "⌛ вчера"
        text += f"{i}. {name} — {streak} дн. ({status})\n"

    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=["profile"])
def profile_cmd(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="profile"):
        return
    update_user_streak(uid, message.from_user.first_name or message.from_user.username or str(uid))
    bot.send_message(message.chat.id, _render_profile_text(uid))


@bot.message_handler(commands=["replay"])
def replay_cmd(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="replay"):
        return
    bot.send_message(message.chat.id, _render_replay_text(uid), parse_mode="HTML")


@bot.message_handler(commands=["shop"])
def shop_cmd(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="shop"):
        return
    bot.send_message(message.chat.id, _shop_render_text(uid), reply_markup=_shop_items_kb(uid))


@bot.callback_query_handler(func=lambda c: c.data.startswith("shop_"))
def shop_callbacks(call):
    try:
        uid = call.from_user.id
        chat_id = call.message.chat.id if call.message else uid
        if not _guard_user(uid, chat_id=chat_id, call_id=call.id, action="shop_callback"):
            return
        data = call.data
        if data == "shop_open":
            safe_edit_message(call, _shop_render_text(uid), reply_markup=_shop_items_kb(uid))
            bot.answer_callback_query(call.id)
            return

        parts = data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные")
            return
        action = parts[1]
        item_id = parts[2]
        item = SHOP_ITEMS.get(item_id)
        if not item:
            bot.answer_callback_query(call.id, "Товар не найден")
            return

        d = load_data()
        rec = d.setdefault("users", {}).setdefault(str(uid), {})
        rec = _ensure_profile_fields(rec)
        inv = rec.setdefault("inventory", [])
        coins = int(rec.get("coins", 0) or 0)

        if action == "buy":
            if item_id in inv:
                bot.answer_callback_query(call.id, "Уже куплено")
            elif coins < int(item["price"]):
                bot.answer_callback_query(call.id, "Недостаточно монет")
            else:
                rec["coins"] = coins - int(item["price"])
                inv.append(item_id)
                rec[f"{item['type']}_item_id"] = item_id
                d["users"][str(uid)] = rec
                save_data(d)
                bot.answer_callback_query(call.id, f"Покупка: {item['name']}")
            safe_edit_message(call, _shop_render_text(uid), reply_markup=_shop_items_kb(uid))
            return

        if action == "apply":
            if item_id not in inv:
                bot.answer_callback_query(call.id, "Сначала купите товар")
                return
            if item["type"] == "avatar":
                rec["avatar_emoji"] = item["value"]
                rec["avatar_item_id"] = item_id
            elif item["type"] == "frame":
                rec["frame_style"] = item["value"]
                rec["frame_item_id"] = item_id
            elif item["type"] == "theme":
                rec["theme_style"] = item["value"]
                rec["theme_item_id"] = item_id
            elif item["type"] == "victory":
                rec["victory_emoji"] = item["value"]
                rec["victory_item_id"] = item_id
            d["users"][str(uid)] = rec
            save_data(d)
            safe_edit_message(call, _shop_render_text(uid), reply_markup=_shop_items_kb(uid))
            bot.answer_callback_query(call.id, f"Применено: {item['name']}")
            return

        bot.answer_callback_query(call.id, "Неизвестное действие")
    except Exception as e:
        log_exception("shop_callbacks", e, user_id=getattr(call.from_user, "id", None))
        try:
            bot.answer_callback_query(call.id, "Ошибка магазина")
        except Exception:
            pass

def _admin_panel_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📊 Сводка", callback_data="admin_stats"))
    kb.add(types.InlineKeyboardButton("👥 Все игроки", callback_data="admin_users"))
    kb.add(types.InlineKeyboardButton("📈 Популярные игры", callback_data="admin_games"))
    kb.add(types.InlineKeyboardButton("💰 Топ по монетам", callback_data="admin_coins"))
    kb.add(types.InlineKeyboardButton("🏠 Комнаты", callback_data="admin_rooms"))
    kb.add(types.InlineKeyboardButton("🧹 Закрыть комнату", callback_data="admin_close_room"))
    kb.add(types.InlineKeyboardButton("⛔ Бан", callback_data="admin_ban_user"))
    kb.add(types.InlineKeyboardButton("✅ Разбан", callback_data="admin_unban_user"))
    kb.add(types.InlineKeyboardButton("💾 Backup", callback_data="admin_backup"))
    kb.add(types.InlineKeyboardButton("🏆 Достижения", callback_data="admin_achievements"))
    kb.add(types.InlineKeyboardButton("📣 Рассылка", callback_data="admin_broadcast"))
    return kb

def _broadcast_menu_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1. Изменить текст сообщения", callback_data="messagenot_msg"))
    kb.add(types.InlineKeyboardButton("2. Изменить текст кнопки", callback_data="messagenot_btn"))
    kb.add(types.InlineKeyboardButton("3. Изменить тип кнопки", callback_data="messagenot_type"))
    kb.add(types.InlineKeyboardButton("4. Отправить всем", callback_data="messagenot_send"))
    return kb

def _send_broadcast_menu(chat_id):
    bot.send_message(chat_id, "⚙️ Настройки рассылки — выберите действие:", reply_markup=_broadcast_menu_kb())

@bot.message_handler(commands=["adminpanel"])
def admin_panel_cmd(message):
    uid = message.from_user.id
    if not _is_bot_admin(uid):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return
    bot.send_message(message.chat.id, "🛠 Админ-панель", reply_markup=_admin_panel_kb())


@bot.message_handler(commands=["backup"])
def backup_cmd(message):
    uid = message.from_user.id
    if not _is_bot_admin(uid):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return
    path = backup_json_files()
    log_admin_action(uid, "backup", details={"path": path}, db_path=DB_FILE)
    bot.send_message(message.chat.id, f"✅ Backup создан: {path or 'не удалось создать'}")


@bot.message_handler(commands=["ban"])
def ban_cmd(message):
    uid = message.from_user.id
    if not _is_bot_admin(uid):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Формат: /ban <user_id> [причина]")
        return
    reason = parts[2] if len(parts) > 2 else "нарушение правил"
    _set_user_ban(uid, int(parts[1]), True, reason)
    bot.send_message(message.chat.id, f"⛔ Пользователь {parts[1]} забанен.")


@bot.message_handler(commands=["unban"])
def unban_cmd(message):
    uid = message.from_user.id
    if not _is_bot_admin(uid):
        bot.send_message(message.chat.id, "⛔ Доступ запрещен.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.send_message(message.chat.id, "Формат: /unban <user_id>")
        return
    _set_user_ban(uid, int(parts[1]), False)
    bot.send_message(message.chat.id, f"✅ Пользователь {parts[1]} разбанен.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def admin_panel_callbacks(call):
    uid = call.from_user.id
    if not _is_bot_admin(uid):
        try:
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        except Exception:
            pass
        return

    data = call.data
    if data == "admin_stats":
        d = load_data()
        users = d.get("users", {})
        total_users = len(users)
        total_games = 0
        total_coins = 0
        premium_count = 0
        now_ts = time.time()
        for rec in users.values():
            if not isinstance(rec, dict):
                continue
            total_games += int(rec.get("games_total", 0) or 0)
            total_coins += int(rec.get("coins", 0) or 0)
            if int(rec.get("premium_until", 0) or 0) > now_ts:
                premium_count += 1
        rooms = d.get("rooms", {})
        pool_count = len(rooms.get("pool", []) or [])
        active_count = len(rooms.get("active", {}) or {})
        text = (
            "📊 Сводка\n\n"
            f"👥 Пользователей: {total_users}\n"
            f"🎮 Сыграно игр: {total_games}\n"
            f"🪙 Монет в системе: {total_coins}\n"
            f"💎 Премиум активен: {premium_count}\n"
            f"🏠 Комнаты: активные {active_count}, пул {pool_count}\n"
        )
        safe_edit_message(call, text, reply_markup=_admin_panel_kb())
        return
    if data == "admin_users":
        d = load_data()
        users = d.get("users", {})
        rows = []
        for uid_str, rec in users.items():
            if not isinstance(rec, dict):
                continue
            total = int(rec.get("games_total", 0) or 0)
            name = rec.get("display_name") or f"user_{uid_str}"
            rows.append((total, str(name), uid_str))
        rows.sort(key=lambda x: (-x[0], x[1].lower()))
        text = f"👥 Всего пользователей: {len(users)}\n\n"
        if rows:
            text += "Топ по сыгранным играм:\n"
            for i, (total, name, uid_str) in enumerate(rows[:20], 1):
                text += f"{i}. {name} (ID {uid_str}) — {total}\n"
        else:
            text += "Нет данных."
        safe_edit_message(call, text, reply_markup=_admin_panel_kb())
        return

    if data == "admin_games":
        d = load_data()
        global_stats = d.get("global_game_stats", {})
        rows = sorted(global_stats.items(), key=lambda kv: int(kv[1] or 0), reverse=True)
        text = "📈 Популярные игры:\n\n"
        if rows:
            for i, (gk, cnt) in enumerate(rows[:20], 1):
                text += f"{i}. {GAME_TITLES.get(gk, gk)} — {int(cnt or 0)}\n"
        else:
            text += "Пока нет статистики."
        safe_edit_message(call, text, reply_markup=_admin_panel_kb())
        return

    if data == "admin_coins":
        d = load_data()
        users = d.get("users", {})
        rows = []
        for uid_str, rec in users.items():
            if not isinstance(rec, dict):
                continue
            coins = int(rec.get("coins", 0) or 0)
            name = rec.get("display_name") or f"user_{uid_str}"
            rows.append((coins, str(name), uid_str))
        rows.sort(key=lambda x: (-x[0], x[1].lower()))
        text = "💰 Топ по монетам:\n\n"
        if rows:
            for i, (coins, name, uid_str) in enumerate(rows[:20], 1):
                text += f"{i}. {name} (ID {uid_str}) — {coins}\n"
        else:
            text += "Пока нет данных."
        safe_edit_message(call, text, reply_markup=_admin_panel_kb())
        return

    if data == "admin_rooms":
        d = load_data()
        rooms = d.get("rooms", {})
        active = rooms.get("active", {}) or {}
        text = "🏠 Активные комнаты:\n\n"
        if active:
            for code, room in active.items():
                if not isinstance(room, dict):
                    continue
                chat_id = room.get("chat_id")
                creator = room.get("creator_name") or room.get("creator_id")
                ends_at = room.get("ends_at")
                ends_str = datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d %H:%M:%S") if ends_at else "—"
                text += f"• {code} | chat {chat_id} | {creator} | до {ends_str}\n"
        else:
            text += "Нет активных комнат."
        safe_edit_message(call, text, reply_markup=_admin_panel_kb())
        return

    if data == "admin_close_room":
        admin_wait[uid] = {"action": "close_room"}
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        bot.send_message(uid, "Введите код комнаты для закрытия (например: A1B2C):")
        return

    if data == "admin_ban_user":
        admin_wait[uid] = {"action": "ban_user"}
        bot.answer_callback_query(call.id)
        bot.send_message(uid, "Введите: <user_id> <причина>")
        return

    if data == "admin_unban_user":
        admin_wait[uid] = {"action": "unban_user"}
        bot.answer_callback_query(call.id)
        bot.send_message(uid, "Введите user_id для разбана:")
        return

    if data == "admin_backup":
        bot.answer_callback_query(call.id, "Создаю backup...")
        path = backup_json_files()
        log_admin_action(uid, "backup", details={"path": path}, db_path=DB_FILE)
        safe_edit_message(call, f"✅ Backup создан:\n{path or 'не удалось создать'}", reply_markup=_admin_panel_kb())
        return

    if data == "admin_achievements":
        d = load_data()
        users = d.get("users", {})
        counts = {k: 0 for k in ACHIEVEMENTS.keys()}
        for rec in users.values():
            if not isinstance(rec, dict):
                continue
            ach = rec.get("achievements", {})
            if not isinstance(ach, dict):
                continue
            for key in counts.keys():
                if key in ach:
                    counts[key] += 1
        text = "🏆 Достижения (кол-во открытий):\n\n"
        for key, meta in ACHIEVEMENTS.items():
            text += f"• {meta['title']}: {counts.get(key, 0)}\n"
        safe_edit_message(call, text, reply_markup=_admin_panel_kb())
        return

    if data == "admin_broadcast":
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        _send_broadcast_menu(call.message.chat.id if call.message else uid)
        return

@bot.message_handler(commands=["settext"])
def settext_cmd(message):
    uid = message.from_user.id

    if uid not in user_sys_settings:
        user_sys_settings[uid] = {
            "msg": "Ваше сообщение",
            "btn": "ОК",
            "title": "Заголовок",
            "gui": "Текст внутри GUI"
        }

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("1. Изменить текст сообщения", callback_data="set_msg"))
    kb.add(types.InlineKeyboardButton("2. Изменить текст кнопки", callback_data="set_btn"))
    kb.add(types.InlineKeyboardButton("3. Изменить заголовок сообщения", callback_data="set_title"))
    kb.add(types.InlineKeyboardButton("4. Изменить текст popup-окна", callback_data="set_gui"))

    bot.send_message(
        message.chat.id,
        "🔧 *Настройки системного уведомления*\nВыберите, что изменить:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@bot.message_handler(commands=["messagenot"])
def messagenot_cmd(message):
    uid = message.from_user.id
    if not _is_bot_admin(uid):
        bot.send_message(message.chat.id, "⛔ Только администратор бота может использовать рассылку.")
        return
    if REQUIRED_CHANNEL and not is_user_subscribed(uid):
        url = _channel_url() or "https://t.me/"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Подписаться", url=url))
        bot.send_message(message.chat.id, "⚠️ Для использования этой функции подпишитесь на канал.", reply_markup=kb)
        return

    _send_broadcast_menu(message.chat.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith(("messagenot_msg","messagenot_btn","messagenot_type","messagenot_send")))
def messagenot_callback(call):
    try:
        uid = call.from_user.id
        if not _is_bot_admin(uid):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        action = call.data.split("_")[1]
        if action == "msg":
            system_notify_wait[uid] = "broadcast_msg"
            bot.answer_callback_query(call.id)
            bot.send_message(uid, "✏ Введите текст рассылки (сообщение):")
            return
        if action == "btn":
            system_notify_wait[uid] = "broadcast_btn"
            bot.answer_callback_query(call.id)
            bot.send_message(uid, "✏ Введите текст кнопки:")
            return
        if action == "type":
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("Ссылка", callback_data="messagenot_type_link"))
            kb.add(types.InlineKeyboardButton("Без кнопки", callback_data="messagenot_type_none"))
            safe_edit_message(call, "Выберите тип кнопки:", reply_markup=kb)
            bot.answer_callback_query(call.id)
            return
        if action == "send":
            bot.answer_callback_query(call.id, "Запускаю отправку...")
            d = load_data()
            users = d.get("users", {})
            sent = 0
            skipped = 0
            for uid_str, info in users.items():
                try:
                    dest = int(uid_str)
                    if not info.get("started"):
                        skipped += 1
                        continue
                    if REQUIRED_CHANNEL and not is_user_subscribed(dest):
                        skipped += 1
                        continue
                    btn_type = BROADCAST_SETTINGS.get("btn_type")
                    if btn_type == "link":
                        kb = types.InlineKeyboardMarkup()
                        kb.add(types.InlineKeyboardButton(BROADCAST_SETTINGS.get("btn_text","Открыть"), url=BROADCAST_SETTINGS.get("btn_link")))
                        bot.send_message(dest, BROADCAST_SETTINGS.get("msg", ""), reply_markup=kb)
                    elif btn_type == "callback":
                        kb = types.InlineKeyboardMarkup()
                        kb.add(types.InlineKeyboardButton(BROADCAST_SETTINGS.get("btn_text","Открыть"), callback_data="broadcast_open"))
                        bot.send_message(dest, BROADCAST_SETTINGS.get("msg", ""), reply_markup=kb)
                    else:
                        bot.send_message(dest, BROADCAST_SETTINGS.get("msg", ""))
                    sent += 1
                    time.sleep(0.05)
                except Exception:
                    skipped += 1
            bot.send_message(uid, f"Готово. Доставлено: {sent}, пропущено: {skipped}")
            return
    except Exception as e:
        log_exception("messagenot", e)
        bot.answer_callback_query(call.id, "Ошибка в редакторе сообщений")


@bot.callback_query_handler(func=lambda c: c.data.startswith("messagenot_type_link"))
def messagenot_type_choice(call):
    try:
        uid = call.from_user.id
        if call.data.endswith("link"):
            system_notify_wait[uid] = "broadcast_btn_link"
            bot.answer_callback_query(call.id)
            bot.send_message(uid, "✏ Введите ссылку для кнопки (напр. https://t.me/minigamesisbot):")
            return
        else:
            BROADCAST_SETTINGS["btn_type"] = "none"
            BROADCAST_SETTINGS["btn_text"] = ""
            BROADCAST_SETTINGS["btn_link"] = ""
            try:
                d = load_data()
                d["broadcast"] = BROADCAST_SETTINGS
                save_data(d)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Готово — кнопка будет убрана из рассылки.")
            bot.send_message(uid, "✅ Тип кнопки: без кнопки. При рассылке кнопка не будет отображаться.")
            return
    except Exception as e:
        log_exception("type_choice", e)
        bot.answer_callback_query(call.id, "Ошибка выбора типа")

@bot.callback_query_handler(func=lambda c: c.data == "broadcast_open")
def broadcast_open(call):
    try:
        bot.answer_callback_query(call.id)
        bot.send_message(call.from_user.id, f"📌 Открытие рассылки:\n\n{BROADCAST_SETTINGS.get('msg','')}")
    except Exception as e:
        log_exception("broadcast_open", e)

@bot.message_handler(commands=["mode"])
def set_mode(message):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💬 Чат", callback_data="mode_chat"))
    kb.add(types.InlineKeyboardButton("⚡ Кратко", callback_data="mode_short"))
    kb.add(types.InlineKeyboardButton("🧠 Подробно", callback_data="mode_long"))
    kb.add(types.InlineKeyboardButton("💻 Код", callback_data="mode_code"))

    bot.send_message(
        message.chat.id,
        "🎛 Выберите режим ответа AI:",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("mode_"))
def mode_callback(call):
    try:
        uid = call.from_user.id
        mode = call.data.split("_")[1]
        user_ai_mode[uid] = mode
        
        mode_names = {
            "chat": "💬 Чат",
            "short": "⚡ Кратко",
            "long": "🧠 Подробно",
            "code": "💻 Код"
        }
        
        bot.answer_callback_query(call.id, f"✅ Режим выбран: {mode_names.get(mode, mode)}")
        bot.edit_message_text(f"✅ Выбран режим: {mode_names.get(mode, mode)}", inline_message_id=call.inline_message_id)
    except Exception as e:
        log_exception("mode_callback", e)
        bot.answer_callback_query(call.id, "Ошибка")

@bot.message_handler(commands=["anim"])
def toggle_anim(message):
    uid = message.from_user.id
    current_state = user_show_easter_egg.get(uid, False)
    user_show_easter_egg[uid] = not current_state
    
    if user_show_easter_egg[uid]:
        bot.send_message(message.chat.id, "🐣 Пасхалка включена! Теперь она будет отображаться в инлайн меню.\n\nЧтобы её выключить, напишите /anim")
    else:
        bot.send_message(message.chat.id, "🐣 Пасхалка отключена. Она больше не будет отображатся в меню.\n\nЧтобы её включить, напишите /anim")

# Текст кнопки главного меню -> чем её подписать в подсказке про inline-режим
INLINE_HINT_BUTTONS = {
    "🧱 Тетрис": "играть в тетрис",
    "🕵️‍♀️ Прятки": "играть в прятки",
    "🎭 Мафия": "играть в мафию",
    "✖️ Крестики-нолики": "играть в крестики-нолики",
    "💰 Миллионер": "играть в миллионер",
    "🟢 Wordle": "играть в Wordle",
    "♟ Шахматы": "играть в шахматы",
    "💬 Режим ИИ": "использовать режим ИИ",
    "🐣 Пасхалка": "запустить анимацию пасхалки",
    "🪙 Орёл или решка": "играть в орёл или решка",
    "🔢 Угадай число": "играть в угадай число",
    "✂ Камень-ножницы-бумага": "играть в камень-ножницы-бумагу",
    "🐍 Змейка": "играть в змейку",
    "🎰 Казино": "запустить казино",
    "🔢 2048": "играть в 2048",
    "🏓 Пинг-понг": "играть в пинг-понг",
    "🔤 Виселица": "играть в Виселицу",
    "🔤 Викторина": "играть в викторину",
    "⚡ Комбо-битва": "играть в комбо-битву",
}


@bot.message_handler(func=lambda m: m.text in INLINE_HINT_BUTTONS)
def inline_hint_button(message):
    action = INLINE_HINT_BUTTONS[message.text]
    bot.send_message(
        message.chat.id,
        f"Чтобы {action} — напишите <code>@{INLINE_BOT_USERNAME}</code> в любом чате!",
        parse_mode="HTML",
    )

@bot.message_handler(func=lambda m: m.text == "ℹ️ Информация о боте")
def bot_info(message):
    bot.send_message(
        message.chat.id,
        "Этот бот создан для мини-игр в Telegram.\n"
        "Он позволяет играть одному и с друзьями через inline-режим, "
        "а также использовать дополнительные функции: профиль, поддержку и рассылку.",
    )

@bot.message_handler(func=lambda m: m.text == "🏠 Скопировать username")
def copy_bot_username(message):
    bot.send_message(
        message.chat.id,
        f"Username бота: <code>@{INLINE_BOT_USERNAME}</code>\n"
        "Нажмите и удерживайте, чтобы скопировать.",
        parse_mode="HTML",
    )

@bot.message_handler(func=lambda m: m.text == "📖 Инструкция")
def bot_instruction(message):
    bot.send_message(
        message.chat.id,
        "Как играть:\n"
        "1. В любом чате введите <code>@{}</code>\n"
        "2. Выберите игру из inline-списка\n"
        "3. Отправьте игру в чат и нажимайте кнопки\n"
        "4. Для личной статистики используйте /profile".format(INLINE_BOT_USERNAME),
        parse_mode="HTML",
    )

@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def profile_button(message):
    uid = message.from_user.id
    update_user_streak(uid, message.from_user.first_name or message.from_user.username or str(uid))
    bot.send_message(message.chat.id, _render_profile_text(uid))

@bot.callback_query_handler(func=lambda c: c.data.startswith("start_"))
def start_info_callbacks(call):
    try:
        if not _guard_user(call.from_user.id, chat_id=call.message.chat.id if call.message else call.from_user.id, call_id=call.id, action="start_callback", require_subscription=False):
            return
        data = call.data
        if data == "start_info":
            safe_edit_message(
                call,
                "Этот бот создан для мини-игр в Telegram.\n"
                "Он позволяет играть одному и с друзьями, "
                "полностью бесплатно!",
                reply_markup=_start_info_kb(),
            )
        elif data == "start_instruction":
            safe_edit_message(
                call,
                "Инструкция:\n"
                "1. Скопируйте юзернейм <code>@{}</code>\n"
                "2. Выберите игру из списка\n"
                "3. Нажмите на игру\n"
                "4. Играйте!".format(INLINE_BOT_USERNAME),
                parse_mode="HTML",
                reply_markup=_start_info_kb(),
            )
        elif data == "start_username":
            safe_edit_message(
                call,
                f"Скопировать юзернейм:\n<code>@{INLINE_BOT_USERNAME}</code>\n"
                "Нажмите и удерживайте юзернейм, затем выберите «Копировать».",
                parse_mode="HTML",
                reply_markup=_start_info_kb(),
            )
        elif data == "start_profile":
            uid = call.from_user.id
            update_user_streak(uid, call.from_user.first_name or call.from_user.username or str(uid))
            safe_edit_message(call, _render_profile_text(uid), reply_markup=_start_info_kb())
        elif data == "start_shop":
            uid = call.from_user.id
            safe_edit_message(call, _shop_render_text(uid), reply_markup=_shop_items_kb(uid))
        elif data == "start_support":
            safe_edit_message(call, _support_text(), reply_markup=_support_menu_kb())
        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("start_info_callback", e)
        try:
            bot.answer_callback_query(call.id, "Ошибка")
        except Exception:
            pass

@bot.message_handler(func=lambda m: m.text == "🔔 Ваше уведомление")
def notification(message):
    bot.send_message(message.chat.id, "Чтобы настроить системное уведомление - напишите <code>/settext</code>", parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "🖥 TELOS v1.0")
def telos(message):
    uid = message.from_user.id
    st = _telos_get_state(uid)
    st["booted"] = True
    _telos_save_state(uid, st)
    bot.send_message(message.chat.id, _telos_home_text(uid), parse_mode="Markdown", reply_markup=telos_main_menu())

def _start_flappy_pm(chat_id, user_id):
    state = _new_flappy_state()
    state["chat_id"] = chat_id
    state["owner_id"] = user_id
    state["message_id"] = None
    state["loop_running"] = False
    sent = bot.send_message(chat_id, _render_flappy_pm_text(state), reply_markup=_flappy_pm_markup(user_id))
    state["message_id"] = sent.message_id
    pm_flappy_games[user_id] = state
    return state

@bot.message_handler(commands=["flappy"])
def flappybird_command(message):
    if message.chat.type != "private":
        bot.send_message(message.chat.id, f"Эту версию Flappy Bird лучше запускать в ЛС с ботом: <code>@{INLINE_BOT_USERNAME}</code>", parse_mode="HTML")
        return
    _start_flappy_pm(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda m: m.text == "🐦 Flappy Bird")
def flappybird(message):
    if message.chat.type != "private":
        bot.send_message(message.chat.id, "Чтобы играть в flappy Bird - откройте ЛС с ботом и нажмите эту кнопку там.")
        return
    _start_flappy_pm(message.chat.id, message.from_user.id)

@bot.message_handler(commands=["connect"])
def connect(message):
    bot.send_message(
        message.chat.id,
        "⚙️ <b>Подключение через Telegram Business</b>\n\n"
        "ВНИМАНИЕ! Сейчас в разработке!\n"
        "AI-функции в business-режиме отключены.\n\n"
        "<b>Доступные игры (этап 1):</b>\n"
        "• тетрис\n"
        "• 2048\n"
        "• кнб (камень-ножницы-бумага)\n"
        "• угадай число\n"
        "• казино\n"
        "• орёл или решка\n\n"
        "<b>Как подключить:</b>\n"
        f"1. Скопируйте имя <code>@{INLINE_BOT_USERNAME}</code>\n"
        "2. Откройте: Настройки → Telegram для бизнеса → Чат-боты\n"
        "3. Добавьте бота и примените настройки\n\n"
        "После подключения просто отправьте в бизнес-чат название игры.",
        parse_mode="HTML",
    )

def _support_text():
    return "🛠 Выберите действие:"

def _support_menu_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💬 Написать модератору", callback_data="support_mode_moderator"))
    kb.add(types.InlineKeyboardButton("🐞 Отправить проблему", callback_data="support_mode_issue"))
    return kb

def _support_mode_prompt(mode):
    if mode == "moderator":
        return (
            "💬 Режим: написать модератору.\n"
            "Отправьте сообщение одним текстом.\n"
            "Для отмены: /cancelsupport"
        )
    return (
        "🐞 Режим: отправить проблему.\n"
        "Пришлите описание, скриншот или видео (можно с подписью).\n"
        "Для отмены: /cancelsupport"
    )

def _start_info_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("ℹ️ Информация о боте", callback_data="start_info"),
        types.InlineKeyboardButton("📖 Инструкция", callback_data="start_instruction"),
    )
    kb.row(
        types.InlineKeyboardButton("🏠 Скопировать юзернейм", callback_data="start_username"),
        types.InlineKeyboardButton("👤 Профиль", callback_data="start_profile"),
    )
    kb.row(
        types.InlineKeyboardButton("🛍 Магазин", callback_data="start_shop"),
        types.InlineKeyboardButton("🛠 Поддержка", callback_data="start_support"),
    )
    kb.row(
        types.InlineKeyboardButton("📖 Help", callback_data="menu_help"),
        types.InlineKeyboardButton("🆕", callback_data="menu_whats_new"),
    )
    return kb

@bot.message_handler(commands=["support"])
def support_command(message):
    bot.send_message(message.chat.id, _support_text(), reply_markup=_support_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🛠 Поддержка")
def support_menu(message):
    bot.send_message(message.chat.id, _support_text(), reply_markup=_support_menu_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("support_mode_"))
def support_mode_callback(call):
    mode = call.data.split("_", 2)[2] if "_" in call.data else ""
    if mode not in ("moderator", "issue"):
        try:
            bot.answer_callback_query(call.id, "Неизвестный режим")
        except Exception:
            pass
        return
    if not SUPPORT_ADMIN_IDS:
        try:
            bot.answer_callback_query(call.id, "Поддержка через модераторов недоступна", show_alert=True)
        except Exception:
            pass
        return
    uid = call.from_user.id
    support_chat_wait[uid] = mode
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    bot.send_message(uid, _support_mode_prompt(mode))

@bot.message_handler(commands=["cancelsupport"])
def cancel_support_chat(message):
    support_chat_wait.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "❌ Режим поддержки отменён.")

@bot.message_handler(commands=["reply"])
def support_admin_reply(message):
    uid = message.from_user.id
    if not _is_support_admin(uid):
        return
    text = (message.text or "").strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.send_message(
            message.chat.id,
            "Формат: /reply <user_id> <текст>\nПример: /reply 123456789 Здравствуйте, проверяем проблему."
        )
        return
    target_uid = int(parts[1])
    reply_text = parts[2].strip()
    if not reply_text:
        bot.send_message(message.chat.id, "Текст ответа пуст.")
        return
    try:
        bot.send_message(target_uid, f"💬 Ответ поддержки:\n{reply_text}")
        bot.send_message(message.chat.id, f"✅ Ответ отправлен пользователю {target_uid}.")
    except Exception:
        bot.send_message(message.chat.id, "❌ Не удалось отправить ответ пользователю.")

@bot.message_handler(func=lambda m: m.text == "🚀 Поддержать автора")
def support_donate(message):
    bot.send_message(message.chat.id, "Если вам нравится этот бот, вы можете поддержать автора отправив тон на адрес:\n\n💳 <code>UQDla14mdjvSsjI1KMJ8cktcbn-smuKXwmFJXPdRT95-k4qQ</code>\n\nЗаранее cпасибо вашу поддержку!", parse_mode="HTML")

@bot.message_handler(commands=["achievements"])
def achievements_cmd(message):
    if not _guard_user(message.from_user.id, chat_id=message.chat.id, action="achievements"):
        return
    bot.send_message(message.chat.id, _render_achievements_text(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "🏆 Достижения")
def achievements_btn(message):
    if not _guard_user(message.from_user.id, chat_id=message.chat.id, action="achievements"):
        return
    bot.send_message(message.chat.id, _render_achievements_text(message.from_user.id))

@bot.message_handler(func=lambda m: m.text == "🏠 Пати")
def room_menu_btn(message):
    bot.send_message(
        message.chat.id,
        "🏠 Пати:\n"
        "• Создать: /party\n"
        "• Войти по коду: /party_join <КОД>\n"
        "• Выйти: /party_leave <КОД>\n"
        "• Статус в группе: /party_status\n"
        "• Для админов групп: /party_register или /party_unregister"
    )

@bot.message_handler(commands=["party_register"])
def room_register_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.send_message(message.chat.id, "Команду /party_register можно использовать только в группе.")
        return
    if not _is_group_admin(message.chat.id, message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Только администратор может зарегистрировать пати.")
        return
    d, rooms = _rooms_get_data()
    pool = rooms.get("pool", [])
    if message.chat.id not in pool:
        pool.append(message.chat.id)
    rooms["pool"] = pool
    rooms["free_title"] = rooms.get("free_title", ROOM_FREE_TITLE)
    save_data(d)
    try:
        bot.set_chat_title(message.chat.id, rooms.get("free_title", ROOM_FREE_TITLE))
    except Exception:
        pass
    bot.send_message(message.chat.id, "✅ Группа зарегистрирована как пати. Статус: свободно.")

@bot.message_handler(commands=["room_unregister", "party_unregister"])
def room_unregister_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.send_message(message.chat.id, "Команду /room_unregister можно использовать только в группе.")
        return
    if not _is_group_admin(message.chat.id, message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Только администратор может удалить комнату из пула.")
        return
    d, rooms = _rooms_get_data()
    pool = rooms.get("pool", [])
    if message.chat.id in pool:
        pool = [cid for cid in pool if cid != message.chat.id]
        rooms["pool"] = pool
        save_data(d)
        bot.send_message(message.chat.id, "✅ Группа удалена из пула комнат.")
    else:
        bot.send_message(message.chat.id, "ℹ️ Эта группа не зарегистрирована.")

@bot.message_handler(commands=["room_status", "party_status"])
def room_status_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.send_message(message.chat.id, "Команда доступна только в группе.")
        return
    d, rooms = _rooms_get_data()
    code, room = _room_find_by_chat(rooms, message.chat.id)
    if not room:
        bot.send_message(message.chat.id, "ℹ️ Эта группа сейчас свободна.")
        return
    ends_at = room.get("ends_at")
    ends_str = datetime.fromtimestamp(ends_at).strftime("%Y-%m-%d %H:%M:%S") if ends_at else "—"
    bot.send_message(message.chat.id, f"🔒 Пати занят\nКод: {code}\nДо: {ends_str}")

@bot.message_handler(commands=["end"])
def room_end_cmd(message):
    if message.chat.type not in ("group", "supergroup"):
        bot.send_message(message.chat.id, "Команда /end работает только в группе.")
        return
    if not _is_group_admin(message.chat.id, message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Только администратор может завершить пати.")
        return
    d, rooms = _rooms_get_data()
    code, room = _room_find_by_chat(rooms, message.chat.id)
    if not room:
        bot.send_message(message.chat.id, "ℹ️ В этой группе нет активной пати.")
        return
    bot.send_message(message.chat.id, "⏳ Завершаю пати и очищаю участников...")
    _room_close(code, reason="завершено вручную")

@bot.message_handler(commands=["party"])
def party_create_cmd(message):
    if not _guard_user(message.from_user.id, chat_id=message.chat.id, action="party"):
        return
    if message.chat.type != "private":
        bot.send_message(message.chat.id, "Создание пати доступно только в личных сообщениях.")
        return
    d, rooms = _rooms_get_data()
    chat_id = _room_pick_free_chat(rooms)
    if not chat_id:
        bot.send_message(message.chat.id, "❌ Нет свободных групп. Попробуйте через 5 минут.")
        return
    code = _room_generate_code(rooms)
    creator = message.from_user
    creator_name = creator.username or creator.first_name or f"user_{creator.id}"
    now_ts = time.time()
    room = {
        "code": code,
        "chat_id": chat_id,
        "creator_id": creator.id,
        "creator_name": creator_name,
        "created_at": now_ts,
        "last_activity_at": now_ts,
        "ends_at": now_ts + ROOM_TTL_SECONDS,
        "status": "voting",
        "participants": [creator.id],
    }
    rooms["active"][code] = room
    save_data(d)

    try:
        bot.set_chat_title(chat_id, creator_name[:64])
    except Exception:
        pass

    invite_link = None
    try:
        invite = bot.create_chat_invite_link(chat_id)
        invite_link = invite.invite_link if invite else None
    except Exception:
        invite_link = None

    if invite_link:
        d3, rooms3 = _rooms_get_data()
        room3 = rooms3.get("active", {}).get(code, {})
        if isinstance(room3, dict):
            room3["invite_link"] = invite_link
            rooms3["active"][code] = room3
            save_data(d3)
        bot.send_message(
            message.chat.id,
            f"✅ Пати создан!\nКод: {code}\nСсылка для входа: {invite_link}\n"
            "В группе запускается голосование за игру."
        )
    else:
        bot.send_message(
            message.chat.id,
            f"✅ Пати создан!\nКод: {code}\n"
            "Не удалось создать ссылку — проверьте права бота в группе."
        )

    d2 = load_data()
    rec = d2.setdefault("users", {}).setdefault(str(creator.id), {})
    rec = _ensure_profile_fields(rec)
    rec["rooms_created"] = int(rec.get("rooms_created", 0) or 0) + 1
    d2["users"][str(creator.id)] = rec
    save_data(d2)
    _check_achievements(creator.id, rec)

    try:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🚪 Покинуть комнату", callback_data=f"room_leave_{code}"))
        bot.send_message(chat_id, f"🏠 Пати создан для {creator_name}\nКод пати: {code}\nГолосование за игру стартует сейчас.", reply_markup=kb)
    except Exception:
        pass
    _room_start_vote(chat_id, code)

@bot.message_handler(commands=["party_join"])
def room_join_cmd(message):
    if not _guard_user(message.from_user.id, chat_id=message.chat.id, action="party_join"):
        return
    if message.chat.type != "private":
        bot.send_message(message.chat.id, "Вход по коду доступен только в личных сообщениях.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "Использование: /room_join <КОД>")
        return
    code = parts[1].strip().upper()
    d, rooms = _rooms_get_data()
    room = rooms.get("active", {}).get(code)
    if not isinstance(room, dict):
        bot.send_message(message.chat.id, "❌ Пати с таким кодом не найден.")
        return
    if time.time() > float(room.get("ends_at") or 0):
        bot.send_message(message.chat.id, "❌ Пати уже закрыто.")
        return
    chat_id = room.get("chat_id")
    invite_link = room.get("invite_link")
    if not invite_link:
        try:
            invite = bot.create_chat_invite_link(chat_id)
            invite_link = invite.invite_link if invite else None
        except Exception:
            invite_link = None
        if invite_link:
            room["invite_link"] = invite_link
            rooms["active"][code] = room
            save_data(d)
    if invite_link:
        bot.send_message(message.chat.id, f"✅ Вход по коду {code}:\n{invite_link}")
    else:
        bot.send_message(message.chat.id, "❌ Не удалось создать ссылку. Проверьте права бота в группе.")
    room_participants.setdefault(chat_id, set()).add(message.from_user.id)
    if isinstance(room.get("participants", []), list) and message.from_user.id not in room.get("participants", []):
        room["participants"].append(message.from_user.id)
        room["last_activity_at"] = time.time()
        rooms["active"][code] = room
        save_data(d)


def _room_leave_user(code, user_id, notify_chat_id=None):
    d, rooms = _rooms_get_data()
    active = rooms.get("active", {}) if isinstance(rooms.get("active", {}), dict) else {}
    room = active.get(code)
    if not isinstance(room, dict):
        return False, "Пати не найдено."
    participants = room.get("participants", [])
    if isinstance(participants, list) and user_id in participants:
        participants.remove(user_id)
    room_participants.get(room.get("chat_id"), set()).discard(user_id)
    remove_room_game_player(code, user_id)
    room["participants"] = participants
    room["last_activity_at"] = time.time()
    active[code] = room
    rooms["active"] = active
    save_data(d)
    try:
        bot.kick_chat_member(room.get("chat_id"), user_id)
        bot.unban_chat_member(room.get("chat_id"), user_id)
    except Exception:
        pass
    try:
        bot.send_message(room.get("chat_id"), f"🚪 Игрок {user_id} покинул пати.")
    except Exception:
        pass
    if notify_chat_id:
        bot.send_message(notify_chat_id, f"✅ Вы покинули пати {code}.")
    return True, "ok"


@bot.message_handler(commands=["party_leave", "room_leave"])
def room_leave_cmd(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="party_leave", require_subscription=False):
        return
    code = ""
    if message.chat.type == "private":
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) > 1:
            code = parts[1].strip().upper()
    d, rooms = _rooms_get_data()
    if not code:
        for active_code, room in (rooms.get("active", {}) or {}).items():
            if isinstance(room, dict) and uid in (room.get("participants", []) or []):
                code = active_code
                break
    if not code:
        bot.send_message(message.chat.id, "Пати для выхода не найдено.")
        return
    ok, reason = _room_leave_user(code, uid, notify_chat_id=message.chat.id)
    if not ok:
        bot.send_message(message.chat.id, f"❌ {reason}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("room_leave_"))
def room_leave_callback(call):
    uid = call.from_user.id
    if not _guard_user(uid, chat_id=call.message.chat.id if call.message else uid, call_id=call.id, action="party_leave", require_subscription=False):
        return
    code = call.data.split("_", 2)[2]
    ok, reason = _room_leave_user(code, uid)
    bot.answer_callback_query(call.id, "✅ Вы вышли из пати." if ok else f"❌ {reason}", show_alert=not ok)

@bot.poll_answer_handler()
def room_poll_answer_handler(poll_answer):
    try:
        poll_id = poll_answer.poll_id
        info = room_polls.get(poll_id)
        if not info:
            return
        code = info.get("code")
        option_ids = poll_answer.option_ids or []
        if not option_ids:
            return
        d, rooms = _rooms_get_data()
        room = rooms.get("active", {}).get(code)
        if not isinstance(room, dict):
            return
        votes = room.get("votes", {})
        if not isinstance(votes, dict):
            votes = {}
        votes[str(poll_answer.user.id)] = int(option_ids[0])
        room["votes"] = votes
        room["last_activity_at"] = time.time()
        rooms["active"][code] = room
        save_data(d)
        room_participants.setdefault(room.get("chat_id"), set()).add(poll_answer.user.id)
    except Exception:
        pass

@bot.message_handler(commands=["reaction"])
def reaction_cmd(message):
    _reaction_start(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda m: m.text == "⚡ Блиц-реакция")
def reaction_btn(message):
    _reaction_start(message.chat.id, message.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("reaction_hit_"))
def reaction_hit_callback(call):
    try:
        gid = call.data.split("_", 2)[2]
        state = reaction_games.get(gid)
        if not state:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        if call.from_user.id != state.get("uid"):
            bot.answer_callback_query(call.id, "Это не ваша игра.")
            return
        if not state.get("started"):
            bot.answer_callback_query(call.id, "Слишком рано!")
            return
        rt_ms = int((time.time() - state.get("start_at", time.time())) * 1000)
        text = f"⚡ Реакция: {rt_ms} мс"
        _reaction_edit(state, text)
        _record_game_play(call.from_user.id, "reaction", display_name=call.from_user.first_name or call.from_user.username or str(call.from_user.id), session_id=f"reaction_{gid}")
        reaction_games.pop(gid, None)
        try:
            if call.message and call.message.chat and call.message.chat.type in ("group", "supergroup"):
                d, rooms = _rooms_get_data()
                code, room = _room_find_by_chat(rooms, call.message.chat.id)
                if room and room.get("game") == "reaction":
                    _room_post_game_prompt(call.message.chat.id, code)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Готово!")
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Ошибка.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("reaction_begin_"))
def reaction_begin_callback(call):
    try:
        gid = call.data.split("_", 2)[2]
        state = reaction_games.get(gid)
        if not state:
            state = {"uid": call.from_user.id, "chat_id": None, "started": False, "start_at": None, "msg_id": None, "inline_id": None}
        if call.from_user.id != state.get("uid"):
            bot.answer_callback_query(call.id, "Это не ваша игра.")
            return
        state["inline_id"] = call.inline_message_id
        state["started"] = False
        state["start_at"] = None
        reaction_games[gid] = state
        _reaction_edit(state, "⚡ Блиц-реакция\nЖдите сигнала и нажмите кнопку!", reply_markup=_reaction_keyboard(gid))

        def trigger():
            time.sleep(random.uniform(2.0, 5.0))
            if gid not in reaction_games:
                return
            st = reaction_games[gid]
            st["started"] = True
            st["start_at"] = time.time()
            reaction_games[gid] = st
            _reaction_edit(st, "⚡ СИГНАЛ! ЖМИ СЕЙЧАС!", reply_markup=_reaction_keyboard(gid))

        Thread(target=trigger, daemon=True).start()
        bot.answer_callback_query(call.id)
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Ошибка.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("room_continue_"))
def room_continue_callback(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            return
        action = parts[2]
        code = parts[3]
        d, rooms = _rooms_get_data()
        room = rooms.get("active", {}).get(code)
        if not isinstance(room, dict):
            bot.answer_callback_query(call.id, "Пати не найден.")
            return
        if action == "yes":
            room["game"] = None
            room["status"] = "voting"
            room["votes"] = {}
            room["poll_id"] = None
            rooms["active"][code] = room
            save_data(d)
            _room_start_vote(room["chat_id"], code)
            bot.answer_callback_query(call.id, "Новое голосование запущено.")
            return
        if action == "no":
            _room_close(code, reason="завершено игроками")
            bot.answer_callback_query(call.id, "Пати закрыто.")
            return
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Ошибка.")
        except Exception:
            pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("room_game_end_"))
def room_game_end_callback(call):
    try:
        code = call.data.split("_", 3)[3]
        d, rooms = _rooms_get_data()
        room = rooms.get("active", {}).get(code)
        if not isinstance(room, dict):
            bot.answer_callback_query(call.id, "Пати не найден.")
            return
        _room_post_game_prompt(room["chat_id"], code)
        bot.answer_callback_query(call.id, "Ок, что дальше?")
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Ошибка.")
        except Exception:
            pass

@bot.message_handler(commands=["blackjack"])
def blackjack_cmd(message):
    state = _bj_new_game(message.from_user.id, message.chat.id)
    gid = short_id()
    blackjack_games[gid] = state
    reveal = state.get("status") != "playing"
    text = _bj_render_text(state, reveal_dealer=reveal)
    kb = _bj_keyboard(gid, state.get("status"))
    msg = bot.send_message(message.chat.id, text, reply_markup=kb)
    try:
        if message.chat.type in ("group", "supergroup"):
            _room_track_message_id(message.chat.id, getattr(msg, "message_id", None))
    except Exception:
        pass
    if state.get("status") == "ended":
        _record_game_play(message.from_user.id, "blackjack", display_name=message.from_user.first_name or message.from_user.username or str(message.from_user.id), session_id=f"blackjack_{gid}")
        _record_game_result(message.from_user.id, "blackjack", state.get("result") or "draws")

@bot.message_handler(func=lambda m: m.text == "🃏 Блэкджек")
def blackjack_btn(message):
    blackjack_cmd(message)

@bot.callback_query_handler(func=lambda c: c.data.startswith("bj_"))
def blackjack_callback(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            return
        action = parts[1]
        gid = parts[2]
        state = blackjack_games.get(gid)
        if not state:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        if call.from_user.id != state.get("uid"):
            bot.answer_callback_query(call.id, "Это не ваша игра.")
            return

        if action == "new":
            state = _bj_new_game(call.from_user.id, call.message.chat.id)
            blackjack_games[gid] = state
            text = _bj_render_text(state, reveal_dealer=False)
            kb = _bj_keyboard(gid, state.get("status"))
            safe_edit_message(call, text, reply_markup=kb)
            bot.answer_callback_query(call.id)
            return

        if state.get("status") != "playing":
            bot.answer_callback_query(call.id, "Партия уже завершена.")
            return

        if action == "hit":
            deck = state.get("deck", [])
            if deck:
                state["player"].append(deck.pop())
            state["deck"] = deck
            player_val = _bj_hand_value(state.get("player", []))
            if player_val > 21:
                state["status"] = "ended"
                state["result"] = "losses"
            blackjack_games[gid] = state

        if action == "stand":
            deck = state.get("deck", [])
            dealer = state.get("dealer", [])
            while _bj_hand_value(dealer) < 17 and deck:
                dealer.append(deck.pop())
            state["dealer"] = dealer
            state["deck"] = deck
            pval = _bj_hand_value(state.get("player", []))
            dval = _bj_hand_value(dealer)
            if dval > 21 or pval > dval:
                state["result"] = "wins"
            elif pval < dval:
                state["result"] = "losses"
            else:
                state["result"] = "draws"
            state["status"] = "ended"
            blackjack_games[gid] = state

        reveal = state.get("status") != "playing"
        text = _bj_render_text(state, reveal_dealer=reveal)
        kb = _bj_keyboard(gid, state.get("status"))
        safe_edit_message(call, text, reply_markup=kb)

        if state.get("status") == "ended" and not state.get("recorded"):
            state["recorded"] = True
            blackjack_games[gid] = state
            _record_game_play(call.from_user.id, "blackjack", display_name=call.from_user.first_name or call.from_user.username or str(call.from_user.id), session_id=f"blackjack_{gid}")
            _record_game_result(call.from_user.id, "blackjack", state.get("result") or "draws")
            try:
                if call.message and call.message.chat and call.message.chat.type in ("group", "supergroup"):
                    d, rooms = _rooms_get_data()
                    code, room = _room_find_by_chat(rooms, call.message.chat.id)
                    if room and room.get("game") == "blackjack":
                        _room_post_game_prompt(call.message.chat.id, code)
            except Exception:
                pass
        bot.answer_callback_query(call.id)
    except Exception:
        try:
            bot.answer_callback_query(call.id, "Ошибка.")
        except Exception:
            pass

@bot.message_handler(func=lambda m: m.text == "💣 Сапёр")
def minesweeper_message(message):
    uid = message.from_user.id
    _record_game_play(uid, "minesweeper", display_name=message.from_user.first_name or message.from_user.username or str(uid), session_id=f"chat_{message.chat.id}_{int(time.time())}")
    start_minesweeper_in_chat(message.chat.id)

@bot.message_handler(func=lambda m: m.text == "🎮 Играть")
def play(message):
    bot.send_message(message.chat.id, f"Чтобы играть — используй инлайн через @{INLINE_BOT_USERNAME} в любом чате!")


def _ai_prompt_status_text(status):
    mapping = {
        "wait": "⏳ ожидание..",
        "process": "⏳ ответ генерируется..",
        "done": "✅ готово",
    }
    return mapping.get(status, "⏳ ожидание..")


def _ai_prompt_message(question, status, answer=None):
    text = (
        f"💬 Вопрос:\n{str(question or '').strip()}\n\n"
        f"Статус: {_ai_prompt_status_text(status)}"
    )
    if status == "done":
        text += "\n\n🤖 Ответ:\n" + str(answer or "")
    return text


def _ai_prompt_kb(uid, rid):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("📩 Получить ответ", callback_data=f"ai_{uid}_{rid}"),
        types.InlineKeyboardButton("🔄 Обновить", callback_data=f"ai_refresh_{uid}_{rid}"),
    )
    return kb


EXCLUDED_AI_QUERIES = {
    "2048", "tetris", "тетрис", "pong", "ping-pong", "hangman", "виселица",
    "слова", "word_duel", "викторина", "quiz", "комбо", "combo",
    "мафия", "mafia", "minesweeper",
}

@bot.inline_handler(lambda q: q.query.strip() != "" and q.query.lower().strip() not in EXCLUDED_AI_QUERIES)
def ai_inline(query):
    if not _inline_guard(query):
        return
    uid = query.from_user.id
    display_name = query.from_user.first_name or query.from_user.username or str(uid)
    text = query.query.strip()
    normalized = text.lower()

    if normalized in ("морской бой", "морскойбой", "battleship", "bship"):
        bgid = short_id()
        game = battleship_games[bgid] = _bship_new_game(uid, display_name)
        result = types.InlineQueryResultArticle(
            id=f"bship_{bgid}",
            title=f"🚢 {get_game_title(uid, 'bship')}",
            description=get_game_description(uid, "bship"),
            input_message_content=types.InputTextMessageContent(_bship_public_text(game)),
            reply_markup=_bship_public_keyboard(bgid, game),
        )
        bot.answer_inline_query(query.id, [result], cache_time=1, is_personal=True)
        return

    if normalized in ("шахматы", "шах", "chess"):
        cgid = short_id()
        game = chess_games[cgid] = _chess_new_game(uid, display_name)
        result = types.InlineQueryResultArticle(
            id=f"chess_{cgid}",
            title=f"♟ {get_game_title(uid, 'chess')}",
            description=get_game_description(uid, "chess"),
            input_message_content=types.InputTextMessageContent(_chess_render_text(game)),
            reply_markup=_chess_keyboard(cgid, game),
        )
        bot.answer_inline_query(query.id, [result], cache_time=1, is_personal=True)
        return

    allow, err = can_use_ai(uid)
    if not allow:
        bot.answer_inline_query(
            query.id,
            [types.InlineQueryResultArticle(
                id="nope",
                title=localized_text(uid, "⚠️ Лимит", "⚠️ Limit", "⚠️ Ліміт"),
                input_message_content=types.InputTextMessageContent(err)
            )],
            cache_time=1,
            is_personal=True
        )
        return

    req_id = uuid.uuid4().hex
    data = load_data()
    data["users"][str(uid)]["pending"][req_id] = {"q": text, "a": None, "status": "wait"}
    save_data(data)

    result = types.InlineQueryResultArticle(
        id=req_id,
        title=localized_text(uid, "🤖 Спросить ChatGPT", "🤖 Ask ChatGPT", "🤖 Запитати ChatGPT"),
        description=text[:60],
        input_message_content=types.InputTextMessageContent(_ai_prompt_message(text, "wait")),
        reply_markup=_ai_prompt_kb(uid, req_id)
    )

    bot.answer_inline_query(query.id, [result], cache_time=1, is_personal=True)

@bot.inline_handler(lambda q: q.query.strip() == "")
def inline_handler(query):
    try:
        if not _inline_guard(query):
            return
        user = query.from_user
        uid = user.id
        user_name = html.escape(user.first_name or localized_text(uid, "Игрок", "Player", "Гравець"))
        starter_id = user.id
        results = []

        rgid = short_id()
        rps_games[rgid] = {"uid": starter_id}
        rps_markup = types.InlineKeyboardMarkup()
        rps_markup.row(
            types.InlineKeyboardButton(localized_text(uid, "🪨 Камень", "🪨 Rock", "🪨 Камінь"), callback_data=f"rps_{rgid}_rock"),
            types.InlineKeyboardButton(localized_text(uid, "📄 Бумага", "📄 Paper", "📄 Папір"), callback_data=f"rps_{rgid}_paper"),
            types.InlineKeyboardButton(localized_text(uid, "✂️ Ножницы", "✂️ Scissors", "✂️ Ножиці"), callback_data=f"rps_{rgid}_scissors")
        )
        results.append(types.InlineQueryResultArticle(
            id=f"rps_{rgid}",
            title=f"✂ {get_game_title(uid, 'rps')}",
            description=get_game_description(uid, "rps"),
            input_message_content=types.InputTextMessageContent(
                localized_text(
                    uid,
                    "✂️ *Камень • Ножницы • Бумага*\nВыберите ход:",
                    "✂️ *Rock • Paper • Scissors*\nChoose your move:",
                    "✂️ *Камінь • Ножиці • Папір*\nОберіть хід:",
                ),
                parse_mode="Markdown"
            ),
            reply_markup=rps_markup
        ))


        join_markup = types.InlineKeyboardMarkup()
        join_markup.add(types.InlineKeyboardButton(
            localized_text(uid, "Присоединиться ⭕", "Join ⭕", "Приєднатися ⭕"),
            callback_data=f"ttt_join_{starter_id}"))
        ttext = localized_text(
            uid,
            f"🎮 {get_game_title(uid, 'ttt')}\n❌ {user_name}\n⭕ — (ожидается)\nНажмите «Присоединиться ⭕», чтобы начать.",
            f"🎮 {get_game_title(uid, 'ttt')}\n❌ {user_name}\n⭕ — (waiting)\nPress «Join ⭕» to start.",
            f"🎮 {get_game_title(uid, 'ttt')}\n❌ {user_name}\n⭕ — (очікується)\nНатисніть «Приєднатися ⭕», щоб почати.",
        )
        results.append(types.InlineQueryResultArticle(
            id=f"ttt_{short_id()}", title=f"❌ {get_game_title(uid, 'ttt')}",
            description=get_game_description(uid, "ttt"),
            input_message_content=types.InputTextMessageContent(message_text=ttext, parse_mode="HTML"),
            reply_markup=join_markup))

        qdata = random.choice(questions)
        gid = short_id()
        millionaire_games[gid] = {"question": qdata, "attempts": 3}
        markup_m = types.InlineKeyboardMarkup()
        for i, opt in enumerate(qdata["options"]):
            markup_m.add(types.InlineKeyboardButton(opt, callback_data=f"millionaire_{gid}_{i}"))
        results.append(types.InlineQueryResultArticle(
            id=f"millionaire_{gid}",
            title=f"💰 {get_game_title(uid, 'millionaire')}",
            description=get_game_description(uid, "millionaire"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                f"💰 {qdata['question']}\nОсталось попыток: 3",
                f"💰 {qdata['question']}\nAttempts left: 3",
                f"💰 {qdata['question']}\nЗалишилось спроб: 3",
            )),
            reply_markup=markup_m
        ))

        if user_show_easter_egg.get(starter_id, False):
            egg_markup = types.InlineKeyboardMarkup()
            egg_markup.add(types.InlineKeyboardButton(
                localized_text(uid, "🐣 Пасхалка", "🐣 Easter egg", "🐣 Пасхалка"), callback_data="easter_egg"))
            results.append(types.InlineQueryResultArticle(
                id=f"egg_{short_id()}",
                title=localized_text(uid, "🐣 Пасхалка", "🐣 Easter egg", "🐣 Пасхалка"),
                description=localized_text(uid, "Анимация", "Animation", "Анімація"),
                input_message_content=types.InputTextMessageContent(
                    localized_text(uid, "🐣 Нажмите кнопку ниже", "🐣 Press the button below", "🐣 Натисніть кнопку нижче")),
                reply_markup=egg_markup
            ))

        coin_m = types.InlineKeyboardMarkup()
        coin_m.add(types.InlineKeyboardButton(
            localized_text(uid, "Бросить 🪙", "Flip 🪙", "Кинути 🪙"), callback_data="coin_flip"))
        results.append(types.InlineQueryResultArticle(
            id=f"coin_{short_id()}",
            title=f"🪙 {get_game_title(uid, 'coin')}",
            description=get_game_description(uid, "coin"),
            input_message_content=types.InputTextMessageContent(
                localized_text(uid, "🪙 Орёл или решка?", "🪙 Heads or tails?", "🪙 Орел чи решка?")),
            reply_markup=coin_m
        ))

        wgid = short_id()
        wgame = _wordle_new_game(starter_id)
        wordle_games[wgid] = wgame
        results.append(types.InlineQueryResultArticle(
            id=f"wordle_{wgid}",
            title=f"🟩 {get_game_title(uid, 'wordle')}",
            description=get_game_description(uid, "wordle"),
            input_message_content=types.InputTextMessageContent(_wordle_render_text(wgame)),
            reply_markup=_wordle_keyboard(wgid, wgame)
        ))

        rgid = short_id()
        reaction_games[rgid] = {"uid": starter_id, "chat_id": None, "started": False, "start_at": None, "msg_id": None, "inline_id": None}
        rmarkup = types.InlineKeyboardMarkup()
        rmarkup.add(types.InlineKeyboardButton(
            localized_text(uid, "▶️ Начать", "▶️ Start", "▶️ Почати"), callback_data=f"reaction_begin_{rgid}"))
        results.append(types.InlineQueryResultArticle(
            id=f"reaction_{rgid}",
            title=f"⚡ {get_game_title(uid, 'reaction')}",
            description=get_game_description(uid, "reaction"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                "⚡ Блиц-реакция\nНажмите «Начать», затем ждите сигнал.",
                "⚡ Reaction blitz\nPress «Start», then wait for the signal.",
                "⚡ Бліц-реакція\nНатисніть «Почати», потім чекайте на сигнал.",
            )),
            reply_markup=rmarkup
        ))

        bjid = short_id()
        bjstate = _bj_new_game(starter_id, None)
        blackjack_games[bjid] = bjstate
        results.append(types.InlineQueryResultArticle(
            id=f"blackjack_{bjid}",
            title=f"🃏 {get_game_title(uid, 'blackjack')}",
            description=get_game_description(uid, "blackjack"),
            input_message_content=types.InputTextMessageContent(_bj_render_text(bjstate, reveal_dealer=bjstate.get("status") != "playing")),
            reply_markup=_bj_keyboard(bjid, bjstate.get("status"))
        ))

        bgid = short_id()
        battleship_games[bgid] = _bship_new_game(starter_id, user.first_name or user.username or str(starter_id))
        results.append(types.InlineQueryResultArticle(
            id=f"bship_{bgid}",
            title=f"\U0001f6a2 {get_game_title(uid, 'bship')}",
            description=get_game_description(uid, "bship"),
            input_message_content=types.InputTextMessageContent(_bship_public_text(battleship_games[bgid])),
            reply_markup=_bship_public_keyboard(bgid, battleship_games[bgid])
        ))

        cgid = short_id()
        chess_games[cgid] = _chess_new_game(starter_id, user.first_name or user.username or str(starter_id))
        results.append(types.InlineQueryResultArticle(
            id=f"chess_{cgid}",
            title=f"♟ {get_game_title(uid, 'chess')}",
            description=get_game_description(uid, "chess"),
            input_message_content=types.InputTextMessageContent(_chess_render_text(chess_games[cgid])),
            reply_markup=_chess_keyboard(cgid, chess_games[cgid])
        ))

        results.append(types.InlineQueryResultArticle(
            id=f"os_{short_id()}",
            title="🖥 TELOS v1.1 (macOS)",
            description=localized_text(
                uid,
                "Мини ОС в телеграме. Версия 1.1 с новыми функциями!",
                "A mini OS inside Telegram. Version 1.1 with new features!",
                "Міні ОС у телеграмі. Версія 1.1 з новими функціями!",
            ),
            input_message_content=types.InputTextMessageContent(
                localized_text(
                    uid,
                    "🖥 *TELOS v1.1*\nВыбирайте приложение:",
                    "🖥 *TELOS v1.1*\nPick an app:",
                    "🖥 *TELOS v1.1*\nОбирайте застосунок:",
                ),
                parse_mode="Markdown"),
            reply_markup=telos_main_menu()
        ))

        results.append(types.InlineQueryResultArticle(
            id=f"guess_{short_id()}",
            title=f"🔢 {get_game_title(uid, 'guess')}",
            description=localized_text(uid, "От 1 до 10", "From 1 to 10", "Від 1 до 10"),
            input_message_content=types.InputTextMessageContent(
                f"🔢 {get_game_title(uid, 'guess')} (1–10)"),
            reply_markup=_number_grid(types.InlineKeyboardMarkup(), "guess_inline_")
        ))

        u_uid = query.from_user.id
        if u_uid in user_sys_settings:
            data = user_sys_settings[u_uid]
            if data.get("title") or data.get("msg"):
                sys_preview_id = short_id()
                btn_text = data.get("btn") or localized_text(uid, "Открыть", "Open", "Відкрити")
                markup_sys = types.InlineKeyboardMarkup()
                markup_sys.add(types.InlineKeyboardButton(btn_text, callback_data=f"sysopen_{u_uid}_{sys_preview_id}"))
                results.append(types.InlineQueryResultArticle(
                    id=f"sys_{sys_preview_id}",
                    title=localized_text(uid, "🔔 Системное уведомление", "🔔 System notification", "🔔 Системне сповіщення"),
                    description=localized_text(uid, "Ваше уведомление", "Your notification", "Ваше сповіщення"),
                    input_message_content=types.InputTextMessageContent(
                        f"*{data.get('title') or localized_text(uid, 'Системное уведомление', 'System notification', 'Системне сповіщення')}*\n{data.get('msg','')}",
                        parse_mode="Markdown"
                    ),
                    reply_markup=markup_sys
                ))

        slot_m = types.InlineKeyboardMarkup()
        slot_m.add(types.InlineKeyboardButton(
            localized_text(uid, "🎰 Крутить", "🎰 Spin", "🎰 Крутити"), callback_data="slot_spin"))
        results.append(types.InlineQueryResultArticle(
            id=f"slot_{short_id()}",
            title=f"🎰 {get_game_title(uid, 'slot')}",
            description=get_game_description(uid, "slot"),
            input_message_content=types.InputTextMessageContent(
                localized_text(uid, "🎰 Нажмите ниже для запуска!", "🎰 Press below to spin!", "🎰 Натисніть нижче, щоб запустити!")),
            reply_markup=slot_m
        ))

        results.append(types.InlineQueryResultArticle(
            id=f"snake_{short_id()}",
            title=f"🐍 {get_game_title(uid, 'snake')}",
            description=get_game_description(uid, "snake"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                "🐍 Используйте кнопки для управления змейкой. ",
                "🐍 Use the buttons to steer the snake. ",
                "🐍 Використовуйте кнопки для керування змійкою. ",
            )),
            reply_markup=snake_controls()
        ))

        tgid = short_id()
        results.append(types.InlineQueryResultArticle(
            id=f"tetris_{tgid}",
            title=f"🧱 {get_game_title(uid, 'tetris')}",
            description=get_game_description(uid, "tetris"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                "🧱 Тетрис\nНажмите кнопку «Старт», чтобы начать.",
                "🧱 Tetris\nPress «Start» to begin.",
                "🧱 Тетріс\nНатисніть кнопку «Старт», щоб почати.",
            )),
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton(
                    localized_text(uid, "▶️ Старт", "▶️ Start", "▶️ Старт"), callback_data="tetris_new")
            )
        ))

        preview_markup = types.InlineKeyboardMarkup()
        preview_markup.row(types.InlineKeyboardButton("⬆️", callback_data="g2048_new_up"))
        preview_markup.row(types.InlineKeyboardButton("⬅️", callback_data="g2048_new_left"),
                           types.InlineKeyboardButton("➡️", callback_data="g2048_new_right"))
        preview_markup.row(types.InlineKeyboardButton("⬇️", callback_data="g2048_new_down"))
        results.append(types.InlineQueryResultArticle(
            id=f"g2048_{short_id()}",
            title=f"🔢 {get_game_title(uid, 'g2048')}",
            description=get_game_description(uid, "g2048"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                "🔢 2048\nНажмите кнопку, чтобы начать.",
                "🔢 2048\nPress a button to start.",
                "🔢 2048\nНатисніть кнопку, щоб почати.",
            )),
            reply_markup=preview_markup
        ))

        pgid = short_id()
        pm = types.InlineKeyboardMarkup()
        pm.add(types.InlineKeyboardButton(
            localized_text(uid, "Присоединиться", "Join", "Приєднатися"), callback_data=f"pong_{pgid}_join"))
        results.append(types.InlineQueryResultArticle(
            id=f"pong_{pgid}",
            title=f"🏓 {get_game_title(uid, 'pong')} " + localized_text(uid, "(2 игрока)", "(2 players)", "(2 гравці)"),
            description=localized_text(uid, "Сейчас в разработке", "Work in progress", "Зараз у розробці"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                "🏓 Пинг-понг\nНажмите 'Присоединиться' чтобы игра началась.",
                "🏓 Ping-Pong\nPress 'Join' to start the game.",
                "🏓 Пінг-понг\nНатисніть 'Приєднатися', щоб гра почалася.",
            )),
            reply_markup=pm
        ))

        gid = short_id()
        hide_games[gid] = {
            "host": starter_id,
            "secret": None,
            "guesser": None,
            "attempts": 5,
            "finished": False
        }

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                localized_text(uid, "🎯 Загадать клетку", "🎯 Pick a cell", "🎯 Загадати клітинку"),
                callback_data=f"hide_set_{gid}"
            )
        )

        results.append(
            types.InlineQueryResultArticle(
                id=f"hide_{gid}",
                title=localized_text(uid, "🕵️ Прятки", "🕵️ Hide and Seek", "🕵️ Хованки"),
                description=localized_text(
                    uid,
                    "Загадайте клетку - другой игрок угадает",
                    "Pick a cell - the other player guesses it",
                    "Загадайте клітинку - інший гравець вгадає",
                ),
                input_message_content=types.InputTextMessageContent(
                    localized_text(
                        uid,
                        "🕵️ *Прятки*\n\nИгрок 1 загадывает клетку.\nИгрок 2 угадывает за 5 попыток.",
                        "🕵️ *Hide and Seek*\n\nPlayer 1 picks a cell.\nPlayer 2 has 5 tries to guess it.",
                        "🕵️ *Хованки*\n\nГравець 1 загадує клітинку.\nГравець 2 вгадує за 5 спроб.",
                    ),
                    parse_mode="Markdown"
                ),
                reply_markup=kb
            )
        )

        hgid = short_id()
        hgame = hangman_games[hgid] = _hangman_new_game()
        results.append(types.InlineQueryResultArticle(
            id=f"hangman_{hgid}",
            title=f"🔤 {get_game_title(uid, 'hangman')}",
            description=get_game_description(uid, "hangman"),
            input_message_content=types.InputTextMessageContent(render_hangman_state(hgame)),
            reply_markup=render_hangman_keyboard(hgid, hgame)
        ))

        mgid = short_id()
        mboard, mmine_positions = generate_minesweeper_board()
        minesweeper_games[mgid] = {"board": mboard, "revealed": set(), "mine_positions": mmine_positions}
        results.append(types.InlineQueryResultArticle(
            id=f"minesweeper_{mgid}",
            title=f"💣 {get_game_title(uid, 'minesweeper')}",
            description=get_game_description(uid, "minesweeper"),
            input_message_content=types.InputTextMessageContent(
                f"💣 {get_game_title(uid, 'minesweeper')}\n{render_minesweeper_board(mboard, set())}"),
            reply_markup=_minesweeper_build_markup(mgid, mboard, set())
        ))

        qgid = short_id()
        qqdata = random.choice(QUIZ_QUESTIONS)
        quiz_games[qgid] = _quiz_new_game(qqdata, starter_id, user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"))
        results.append(types.InlineQueryResultArticle(
            id=f"quizgame_{qgid}",
            title=f"🧠 {get_game_title(uid, 'quizgame')}",
            description=get_game_description(uid, "quizgame"),
            input_message_content=types.InputTextMessageContent(_quiz_intro_text(qqdata["q"]), parse_mode="Markdown"),
            reply_markup=_quiz_join_kb(qgid)
        ))

        cgid = short_id()
        combo_games[cgid] = _combo_new_game(starter_id, user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"))
        results.append(types.InlineQueryResultArticle(
            id=f"combogame_{cgid}",
            title=f"⚡ {get_game_title(uid, 'combogame')}",
            description=get_game_description(uid, "combogame"),
            input_message_content=types.InputTextMessageContent(COMBO_INTRO_TEXT, parse_mode="Markdown"),
            reply_markup=_combo_join_kb(cgid)
        ))

        mgid = short_id()
        mafia_games[mgid] = mafia_new_game(starter_id, user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"))
        results.append(types.InlineQueryResultArticle(
            id=f"mafia_{mgid}",
            title=f"🎭 {get_game_title(uid, 'mafia')}",
            description=get_game_description(uid, "mafia"),
            input_message_content=types.InputTextMessageContent(localized_text(
                uid,
                "🎭 Мафия\n\nСоздано лобби. Нажмите «Присоединиться», затем «Старт».",
                "🎭 Mafia\n\nLobby created. Press «Join», then «Start».",
                "🎭 Мафія\n\nСтворено лобі. Натисніть «Приєднатися», потім «Старт».",
            )),
            reply_markup=mafia_build_lobby_kb(mgid)
        ))

        pkgid = short_id()
        pk_bet = 10
        pk_state = _poker_new_game(starter_id, None, pk_bet)
        poker_games[pkgid] = pk_state
        results.append(types.InlineQueryResultArticle(
            id=f"poker_{pkgid}",
            title=localized_text(
                uid,
                "🃏 Покер (Техасский холдем)",
                "🃏 Poker (Texas Hold'em)",
                "🃏 Покер (Техаський холдем)",
            ),
            description=localized_text(
                uid,
                f"Игра против бота, ставка {pk_bet}🪙",
                f"Play against the bot, bet {pk_bet}🪙",
                f"Гра проти бота, ставка {pk_bet}🪙",
            ),
            input_message_content=types.InputTextMessageContent(_poker_render_text(pk_state)),
            reply_markup=_poker_keyboard(pkgid, pk_state)
        ))

        dgid = short_id()
        p1_name = user.first_name or str(starter_id)
        inline_duel_games[dgid] = {
            "players": [starter_id],
            "names": {starter_id: p1_name},
            "moves": {},
            "scores": {starter_id: 0},
            "round": 1,
            "status": "waiting",
        }
        duel_st = inline_duel_games[dgid]
        results.append(types.InlineQueryResultArticle(
            id=f"iduel_{dgid}",
            title=localized_text(
                uid,
                "⚔️ Дуэль (КНБ, 3 раунда)",
                "⚔️ Duel (RPS, 3 rounds)",
                "⚔️ Дуель (КНП, 3 раунди)",
            ),
            description=localized_text(
                uid,
                "Камень-ножницы-бумага, 3 раунда против другого игрока",
                "Rock-paper-scissors, 3 rounds against another player",
                "Камінь-ножиці-папір, 3 раунди проти іншого гравця",
            ),
            input_message_content=types.InputTextMessageContent(_iduel_text(duel_st)),
            reply_markup=_iduel_kb(dgid, duel_st)
        ))

        bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

    except Exception as e:
        log_exception("inline", e)

def render_flappy_state(state):
    W, H = 10, 10
    field = [["⬛" for _ in range(W)] for _ in range(H)]
    for x, gap in state["pipes"]:
        for y in range(H):
            if not (gap <= y <= gap+2):
                if 0 <= x < W:
                    field[y][x] = "🟥"
    by = int(state["bird_y"])
    if 0 <= by < H:
        field[by][2] = "🐦"
    return "\n".join("".join(r) for r in field)

def _new_flappy_state():
    return {
        "bird_y": 5,
        "velocity": 0.0,
        "pipes": [(9, 3), (14, 4)],
        "score": 0,
        "started": False,
        "over": False,
        "loop_running": False,
        "inline_id": None,
    }

def _flappy_step(state):
    state["velocity"] = min(2.6, state.get("velocity", 0.0) + 0.6)
    state["bird_y"] += state["velocity"]

    new_pipes = []
    for pipe in state.get("pipes", []):
        x, gap = pipe
        x -= 1
        if x >= -1:
            new_pipes.append((x, gap))
        if x == 1:
            state["score"] += 1
    state["pipes"] = new_pipes

    if not state["pipes"] or state["pipes"][-1][0] <= 5:
        state["pipes"].append((9, random.randint(1, 6)))

    by = int(round(state["bird_y"]))
    if by < 0 or by >= 10:
        state["over"] = True
        state["started"] = False
        return

    for x, gap in state["pipes"]:
        if x == 2 and not (gap <= by <= gap + 2):
            state["over"] = True
            state["started"] = False
            return

def _flappy_pm_markup(uid, game_over=False):
    markup = types.InlineKeyboardMarkup()
    if game_over:
        markup.row(
            types.InlineKeyboardButton("🔄 Ещё раз", callback_data=f"flappy_pm_{uid}_restart"),
            types.InlineKeyboardButton("❌ Закрыть", callback_data=f"flappy_pm_{uid}_close"),
        )
        return markup
    markup.row(
        types.InlineKeyboardButton("▶️ Старт", callback_data=f"flappy_pm_{uid}_start"),
        types.InlineKeyboardButton("⬆️ Прыжок", callback_data=f"flappy_pm_{uid}_jump"),
    )
    markup.add(types.InlineKeyboardButton("❌ Закрыть", callback_data=f"flappy_pm_{uid}_close"))
    return markup

def _render_flappy_pm_text(state):
    lines = [
        "🐦 Flappy Bird в ЛС",
        f"Очки: {state['score']}",
    ]
    if state.get("over"):
        lines.append("Игра окончена.")
    elif not state.get("started"):
        lines.append("Нажмите «Старт», затем жмите «Прыжок».")
    lines.append("")
    lines.append(render_flappy_state(state))
    return "\n".join(lines)

def _edit_flappy_pm(uid):
    state = pm_flappy_games.get(uid)
    if not state:
        return False
    chat_id = state.get("chat_id")
    message_id = state.get("message_id")
    if not chat_id or not message_id:
        return False
    try:
        bot.edit_message_text(
            _render_flappy_pm_text(state),
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=_flappy_pm_markup(uid, game_over=state.get("over", False)),
        )
        return True
    except Exception as e:
        msg = str(e)
        if "message is not modified" in msg or "specified new message content and reply markup are exactly the same" in msg:
            return True
        log_exception("flappy_pm_edit", e)
        return False

def flappy_pm_loop(uid):
    while uid in pm_flappy_games:
        state = pm_flappy_games.get(uid)
        if not state:
            break
        if not state.get("started") or state.get("over"):
            time.sleep(0.25)
            continue
        _flappy_step(state)
        if not _edit_flappy_pm(uid):
            break
        if state.get("over"):
            break
        time.sleep(0.8)

@bot.callback_query_handler(func=lambda c: c.data.startswith("flappy_pm_"))
def flappy_pm_callback(call):
    try:
        parts = str(call.data or "").split("_", 3)  # flappy_pm_<uid>_<action>
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Некорректная команда")
            return
        owner_id = int(parts[2])
        action = parts[3]
        uid = call.from_user.id

        if uid != owner_id:
            bot.answer_callback_query(call.id, "Это не ваша игра", show_alert=True)
            return

        state = pm_flappy_games.get(owner_id)
        if action == "restart":
            state = _new_flappy_state()
            state["chat_id"] = call.message.chat.id
            state["message_id"] = call.message.message_id
            state["owner_id"] = owner_id
            pm_flappy_games[owner_id] = state
            _edit_flappy_pm(owner_id)
            bot.answer_callback_query(call.id, "Новая игра")
            return

        if action == "close":
            pm_flappy_games.pop(owner_id, None)
            try:
                bot.edit_message_text("🐦 Flappy Bird закрыт.", chat_id=call.message.chat.id, message_id=call.message.message_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Закрыто")
            return

        if not state:
            state = _new_flappy_state()
            state["chat_id"] = call.message.chat.id
            state["message_id"] = call.message.message_id
            state["owner_id"] = owner_id
            pm_flappy_games[owner_id] = state

        if action == "start":
            if state.get("started"):
                bot.answer_callback_query(call.id, "Игра уже идёт")
                return
            state["started"] = True
            state["over"] = False
            state["velocity"] = 0.0
            _edit_flappy_pm(owner_id)
            if not state.get("loop_running"):
                state["loop_running"] = True
                Thread(target=flappy_pm_loop, args=(owner_id,), daemon=True).start()
            bot.answer_callback_query(call.id, "Старт!")
            return

        if action == "jump":
            if state.get("over"):
                bot.answer_callback_query(call.id, "Игра окончена")
                return
            if not state.get("started"):
                state["started"] = True
                if not state.get("loop_running"):
                    state["loop_running"] = True
                    Thread(target=flappy_pm_loop, args=(owner_id,), daemon=True).start()
            state["velocity"] = -1.8
            _edit_flappy_pm(owner_id)
            bot.answer_callback_query(call.id, "Прыжок!")
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("flappy_pm", e)
        bot.answer_callback_query(call.id, "Ошибка Flappy Bird")

@bot.callback_query_handler(func=lambda c: c.data.startswith("guess_inline_"))
def guess_inline_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверный формат данных")
            return
        try:
            guess = int(parts[2])
        except Exception:
            bot.answer_callback_query(call.id, "Неверный выбор")
            return

        mid = call.inline_message_id
        if not mid:
            bot.answer_callback_query(call.id, "Эта игра доступна только в inline-режиме")
            return

        state = inline_guess_games.get(mid)
        if not state:
            state = {"target": random.randint(1, 10), "attempts": 3, "tried": []}
            inline_guess_games[mid] = state

        if guess == state["target"]:
            bot.edit_message_text(f"✅ Правильно! Загаданное число: {state['target']}", inline_message_id=mid)
            inline_guess_games.pop(mid, None)
            bot.answer_callback_query(call.id, "Правильно!")
            return

        state["attempts"] -= 1
        state["tried"].append(guess)
        if state["attempts"] <= 0:
            bot.edit_message_text(f"❌ Попытки кончились. Загаданное число: {state['target']}", inline_message_id=mid)
            inline_guess_games.pop(mid, None)
            bot.answer_callback_query(call.id, "Игра окончена")
            return

        hint = "меньше" if guess > state["target"] else "больше"
        bot.edit_message_text(
            f"🔢 Угадай число (1–10)\nПопыток осталось: {state['attempts']}\nВаше предположение: {guess} — {hint}",
            inline_message_id=mid,
            reply_markup=_number_grid(types.InlineKeyboardMarkup(), "guess_inline_")
        )
        bot.answer_callback_query(call.id, "Неправильно")

    except Exception as e:
        log_exception("guess_inline", e)
        bot.answer_callback_query(call.id, "Ошибка игры Угадай число")


@bot.callback_query_handler(func=lambda c: c.data.startswith("snake_"))
def snake_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "Неверный формат")
            return
        action = parts[1]  # up/left/right/down

        mid = call.inline_message_id
        if not mid:
            bot.answer_callback_query(call.id, "Эта игра доступна только в inline-режиме")
            return

        state = inline_snake_games.get(mid)
        if not state:
            W, H = 8, 6
            init_x, init_y = W // 2, H // 2
            snake = [(init_x, init_y), (init_x - 1, init_y), (init_x - 2, init_y)]
            food = (random.randint(0, W - 1), random.randint(0, H - 1))
            while food in snake:
                food = (random.randint(0, W - 1), random.randint(0, H - 1))
            state = {"W": W, "H": H, "snake": snake, "dir": action, "food": food, "score": 0}
            inline_snake_games[mid] = state

        dirs = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
        if action not in dirs:
            action = state.get("dir", "right")
        dx, dy = dirs[action]
        state["dir"] = action

        head_x, head_y = state["snake"][0]
        new_head = (head_x + dx, head_y + dy)

        W, H = state["W"], state["H"]
        if new_head[0] < 0 or new_head[0] >= W or new_head[1] < 0 or new_head[1] >= H or new_head in state["snake"]:
            bot.edit_message_text(f"💥 Вы проиграли! Очки: {state['score']}", inline_message_id=mid)
            inline_snake_games.pop(mid, None)
            bot.answer_callback_query(call.id, "Игра окончена")
            return

        state["snake"].insert(0, new_head)
        if new_head == state["food"]:
            state["score"] += 1
            food = (random.randint(0, W - 1), random.randint(0, H - 1))
            while food in state["snake"]:
                food = (random.randint(0, W - 1), random.randint(0, H - 1))
            state["food"] = food
        else:
            state["snake"].pop()

        field = [["⬛" for _ in range(W)] for _ in range(H)]
        fx, fy = state["food"]
        field[fy][fx] = "🍎"
        for idx, (sx, sy) in enumerate(state["snake"]):
            if 0 <= sy < H and 0 <= sx < W:
                field[sy][sx] = "🟢" if idx == 0 else "🟩"

        text = f"🐍 Змейка — очки: {state['score']}\n\n" + "\n".join("".join(row) for row in field)

        bot.edit_message_text(text, inline_message_id=mid, reply_markup=snake_controls())
        bot.answer_callback_query(call.id)

    except Exception as e:
        log_exception("snake", e)
        bot.answer_callback_query(call.id, "Ошибка игры Змейка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("hide_set_"))
def hide_set(call):
    gid = call.data.split("_")[2]
    game = hide_games.get(gid)

    if not game or call.from_user.id != game["host"]:
        bot.answer_callback_query(call.id, "❌ Только создатель игры")
        return

    kb = hide_keyboard(f"hide_secret_{gid}")

    bot.edit_message_text(
        "🎯 *Выберите клетку, где вы прячетесь:*",
        inline_message_id=call.inline_message_id,
        reply_markup=kb,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("hide_secret_"))
def hide_secret(call):
    _, _, gid, cell = call.data.split("_")
    cell = int(cell)
    game = hide_games.get(gid)

    if not game or game["finished"]:
        bot.answer_callback_query(call.id, "Игра завершена")
        return

    if call.from_user.id == game["host"]:
        bot.answer_callback_query(call.id, "❌ Вы не можете угадывать свою же клетку")
        return

    if game["guesser"] is None:
        game["guesser"] = call.from_user.id

    if call.from_user.id != game["guesser"]:
        bot.answer_callback_query(call.id, "❌ Сейчас ход другого игрока")
        return

    if game["attempts"] <= 0:
        game["finished"] = True
        bot.edit_message_text(
            f"💀 *Попытки закончились!*\nКлетка была: {game['secret'] + 1}",
            inline_message_id=call.inline_message_id,
            parse_mode="Markdown"
        )
        return

    kb = hide_keyboard(f"hide_guess_{gid}")

    if game.get("secret") == cell:
        game["finished"] = True
        try:
            bot.edit_message_text(
                f"🎉 *Угадали!*\nКлетка: {cell + 1}",
                inline_message_id=call.inline_message_id,
                parse_mode="Markdown"
            )
        except telebot.apihelper.ApiTelegramException as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                bot.answer_callback_query(call.id, "✅ Уже отмечено")
                return
            raise
        bot.answer_callback_query(call.id, "🎉 Правильно")
        return

    game["attempts"] = max(0, game.get("attempts", 0) - 1)
    if game["attempts"] <= 0:
        game["finished"] = True
        try:
            bot.edit_message_text(
                f"💀 *Попытки закончились!*\nКлетка была: {game.get('secret', 0) + 1}",
                inline_message_id=call.inline_message_id,
                parse_mode="Markdown"
            )
        except telebot.apihelper.ApiTelegramException as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                bot.answer_callback_query(call.id, "❌ Ничего не изменилось")
                return
            raise
        bot.answer_callback_query(call.id, "💀 Попытки кончились")
        return

    new_message = f"❌ Мимо!\n🔁 Осталось попыток: {game['attempts']}"
    try:
        bot.edit_message_text(
            new_message,
            inline_message_id=call.inline_message_id,
            reply_markup=kb
        )
    except telebot.apihelper.ApiTelegramException as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            bot.answer_callback_query(call.id, "❌ Ничего не изменилось")
            return
        raise
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ai_"))
def ai_callback(call):
    try:
        parts = call.data.split("_")
        action = "get"
        if len(parts) == 4 and parts[1] == "refresh":
            _, _, uid, rid = parts
            action = "refresh"
        elif len(parts) == 3:
            _, uid, rid = parts
        else:
            bot.answer_callback_query(call.id, "Неверные данные")
            return

        uid = int(uid)
        if call.from_user.id != uid:
            bot.answer_callback_query(call.id, "Это не ваш запрос")
            return

        data = load_data()
        user = data["users"].get(str(uid))
        if not user:
            bot.answer_callback_query(call.id, "Данные пользователя не найдены")
            return

        req = user.get("pending", {}).get(rid)
        if not req:
            bot.answer_callback_query(call.id, "Запрос устарел")
            return
        status = str(req.get("status", "wait")).strip().lower()
        if status not in ("wait", "process", "done"):
            status = "wait"
        if req.get("status") != status:
            req["status"] = status
            save_data(data)

        if action == "refresh":
            if status == "done":
                safe_edit_message(call, _ai_prompt_message(req.get("q"), "done", req.get("a")), reply_markup=_ai_prompt_kb(uid, rid))
                bot.answer_callback_query(call.id, "✅ Ответ готов")
            elif status == "process":
                safe_edit_message(call, _ai_prompt_message(req.get("q"), "process"), reply_markup=_ai_prompt_kb(uid, rid))
                bot.answer_callback_query(call.id, "⏳ Ответ ещё генерируется…")
            else:
                safe_edit_message(call, _ai_prompt_message(req.get("q"), "wait"), reply_markup=_ai_prompt_kb(uid, rid))
                bot.answer_callback_query(call.id, "⏳ Ожидание запуска")
            return

        if status == "wait":
            allow, err = can_use_ai(uid)
            if not allow:
                bot.answer_callback_query(call.id, err, show_alert=True)
                return
            req["status"] = "process"
            req["started_at"] = int(time.time())
            save_data(data)

            def work():
                try:
                    prompt = req["q"]
                    answer = ask_ai(prompt, uid)
                    d2 = load_data()
                    u2 = d2.setdefault("users", {}).setdefault(str(uid), {})
                    pending2 = u2.setdefault("pending", {})
                    req2 = pending2.get(rid)
                    if req2 is None:
                        return
                    if req2.get("status") != "process":
                        return

                    # списываем лимит только после фактического получения ответа
                    today = date.today().isoformat()
                    if u2.get("date") != today:
                        u2["date"] = today
                        u2["count"] = 0
                    if u2.get("daily_date") != today:
                        u2["daily_date"] = today
                        u2["daily_count"] = 0
                    u2["count"] = int(u2.get("count", 0) or 0) + 1
                    u2["daily_count"] = int(u2.get("daily_count", 0) or 0) + 1

                    req2["a"] = answer
                    req2["status"] = "done"
                    save_data(d2)

                except Exception as e:
                    d3 = load_data()
                    u3 = d3.setdefault("users", {}).setdefault(str(uid), {})
                    pending3 = u3.setdefault("pending", {})
                    req3 = pending3.get(rid)
                    if req3 is not None:
                        req3["a"] = "❌ Временная ошибка AI-сервиса. Нажмите «Обновить» или «Получить ответ» ещё раз."
                        req3["status"] = "done"
                        save_data(d3)

            Thread(target=work, daemon=True).start()
            safe_edit_message(call, _ai_prompt_message(req.get("q"), "process"), reply_markup=_ai_prompt_kb(uid, rid))
            bot.answer_callback_query(call.id, "⏳ Готовлю ответ…")
            return

        if status == "process":
            started_at = int(req.get("started_at", 0) or 0)
            if started_at and (int(time.time()) - started_at) > 180:
                req["status"] = "done"
                req["a"] = "❌ Ответ не был получен вовремя (таймаут 3 минуты). Нажмите «Получить ответ» ещё раз."
                save_data(data)
                safe_edit_message(call, _ai_prompt_message(req.get("q"), "done", req.get("a")), reply_markup=_ai_prompt_kb(uid, rid))
                bot.answer_callback_query(call.id, "⌛ Таймаут запроса")
                return
            safe_edit_message(call, _ai_prompt_message(req.get("q"), "process"), reply_markup=_ai_prompt_kb(uid, rid))
            bot.answer_callback_query(call.id, "⏳ Ответ ещё генерируется…")
            return

        if status == "done":
            answer = req["a"]
            safe_edit_message(call, _ai_prompt_message(req.get("q"), "done", answer), reply_markup=_ai_prompt_kb(uid, rid))
            bot.answer_callback_query(call.id, "✅ Ответ готов!")
            return

    except Exception as e:
        log_exception("ai_callback", e)
        bot.answer_callback_query(call.id, "Ошибка при получении ответа")

def _ttt_wins(board, symbol):
    return any(board[a] == board[b] == board[c] == symbol for a, b, c in TTT_WIN_PATTERNS)


def _ttt_restart_kb(gid):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔁 Сыграть ещё", callback_data=f"ttt_restart_{gid}"))
    return kb


def _ttt_show_board(call, gid, game, prefix="", finished=False):
    text = prefix + ttt_render_header(game) + ttt_render_board(game["board"])
    kb = _ttt_restart_kb(gid) if finished else ttt_build_keyboard(gid, game["board"])
    bot.edit_message_text(text, inline_message_id=call.inline_message_id, reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("ttt_join_"))
def ttt_join(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные.")
            return
        host_id = int(parts[2])
        guest_id = call.from_user.id

        if host_id == guest_id:
            bot.answer_callback_query(call.id, "Вы не можете играть сами с собой!")
            return

        gid = short_id()
        # players[0] ходит крестиком, но первым начинает гость (⭕)
        game = inline_ttt_games[gid] = {
            "board": [" "] * 9,
            "players": [host_id, guest_id],
            "names": {
                host_id: _user_display_name_from_id(host_id),
                guest_id: call.from_user.username or call.from_user.first_name or f"Player_{guest_id}",
            },
            "scores": {host_id: 0, guest_id: 0},
            "turn": guest_id,
        }

        _ttt_show_board(call, gid, game)
        bot.answer_callback_query(call.id, "Игра началась! Удачи.")
    except Exception as e:
        log_exception("ttt_join", e)
        bot.answer_callback_query(call.id, "Ошибка при создании игры TTT.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("ttt_move_"))
def ttt_move(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Неверные данные хода.")
            return
        gid = parts[2]
        cell = int(parts[3])
        game = inline_ttt_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена или завершена.")
            return

        uid = call.from_user.id
        if uid not in game["players"]:
            bot.answer_callback_query(call.id, "Вы не участник этой игры.")
            return
        if uid != game["turn"]:
            bot.answer_callback_query(call.id, "Сейчас не ваш ход!")
            return
        if not 0 <= cell < 9:
            bot.answer_callback_query(call.id, "Неверная клетка.")
            return
        if game["board"][cell].strip():
            bot.answer_callback_query(call.id, "Клетка уже занята!")
            return

        p1, p2 = game["players"]
        symbol = TTT_X if uid == p1 else "⭕"
        game["board"][cell] = symbol

        if _ttt_wins(game["board"], symbol):
            game["scores"][uid] = game["scores"].get(uid, 0) + 1
            name = game["names"].get(uid, _user_display_name_from_id(uid))
            _ttt_show_board(call, gid, game, prefix=f"🎉 Победил {symbol} — {name}!\n\n", finished=True)
            # доска сбрасывается, счёт сохраняется для реванша
            game["board"] = [" "] * 9
            game["turn"] = p1
            bot.answer_callback_query(call.id, "Победа!")
            return

        if " " not in game["board"]:
            _ttt_show_board(call, gid, game, prefix="🤝 Ничья!\n\n", finished=True)
            game["board"] = [" "] * 9
            game["turn"] = p1
            bot.answer_callback_query(call.id, "Ничья!")
            return

        game["turn"] = p2 if uid == p1 else p1
        _ttt_show_board(call, gid, game)
        bot.answer_callback_query(call.id, "Ход сделан.")
    except Exception as e:
        log_exception("ttt_move", e)
        bot.answer_callback_query(call.id, "Ошибка в ходе крестиков-ноликов.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("ttt_restart_"))
def ttt_restart(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные рестарта.")
            return
        gid = parts[2]
        game = inline_ttt_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена.")
            return
        game["board"] = [" "] * 9
        game["turn"] = game["players"][1]
        _ttt_show_board(call, gid, game)
        bot.answer_callback_query(call.id, "Новая партия — удачи!")
    except Exception as e:
        log_exception("ttt_restart", e)
        bot.answer_callback_query(call.id, "Ошибка при рестарте игры.")


def spawn_tile(board):
    empty = [(y, x) for y in range(4) for x in range(4) if board[y][x] == 0]
    if not empty:
        return board
    y, x = random.choice(empty)
    board[y][x] = 2 if random.random() < 0.9 else 4
    return board

G2048_COLORS = {
    0: "⬜", 2: "🟫", 4: "🟫", 8: "🟧", 16: "🟧", 32: "🟧",
    64: "🟨", 128: "🟨", 256: "🟦", 512: "🟦", 1024: "🟪", 2048: "🟧",
}


def render_2048(board):
    def cell(n):
        color = G2048_COLORS.get(n, "🟪")
        return f"{color}{(str(n) if n else '').center(4)}{color}"

    border = "───────" * 4
    lines = ["┌" + border + "┐"]
    for i, row in enumerate(board):
        lines.append("│" + "".join(cell(c) for c in row) + "│")
        if i < 3:
            lines.append("├" + border + "┤")
    lines.append("└" + border + "┘")

    return "\n".join(lines)

def move_row_left(row):
    new = [v for v in row if v != 0]
    res = []
    i = 0
    while i < len(new):
        if i+1 < len(new) and new[i] == new[i+1]:
            res.append(new[i]*2)
            i += 2
        else:
            res.append(new[i])
            i += 1
    res += [0]*(4-len(res))
    return res

def move_board(board, direction):
    moved = False
    new = [[board[y][x] for x in range(4)] for y in range(4)]
    if direction in ("left","right"):
        for y in range(4):
            row = list(new[y])
            if direction == "right":
                row = row[::-1]
            moved_row = move_row_left(row)
            if direction == "right":
                moved_row = moved_row[::-1]
            if moved_row != new[y]:
                moved = True
            new[y] = moved_row
    else:
        cols = [[new[y][x] for y in range(4)] for x in range(4)]
        for x in range(4):
            col = cols[x]
            if direction == "down":
                col = col[::-1]
            moved_col = move_row_left(col)
            if direction == "down":
                moved_col = moved_col[::-1]
            for y in range(4):
                if new[y][x] != moved_col[y]:
                    moved = True
                new[y][x] = moved_col[y]
    return new, moved

TETRIS_SHAPES = [
    [[1, 1, 1, 1]],               # I
    [[1, 1], [1, 1]],             # O
    [[1, 1, 1], [0, 1, 0]],       # T
    [[1, 1, 1], [1, 0, 0]],       # L
    [[1, 1, 1], [0, 0, 1]],       # J
    [[1, 1, 0], [0, 1, 1]],       # S
    [[0, 1, 1], [1, 1, 0]],       # Z
    [[1, 1, 1]],                  # mini I
    [[1], [1], [1]],              # mini I vertical
    [[1, 1], [1, 0]],             # small L
    [[1, 1], [0, 1]],             # small J
    [[1, 1, 1], [1, 0, 1]],       # U
    [[0, 1, 0], [1, 1, 1], [0, 1, 0]],  # plus
]
TETRIS_COLORS = ["🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫", "⬜"]

def tetris_new_state():
    st = {"w": 10, "h": 14, "board": [[0] * 10 for _ in range(14)], "piece": None, "score": 0, "over": False}
    tetris_spawn_piece(st)
    return st

def tetris_can_place(state, px, py, shape):
    for sy, row in enumerate(shape):
        for sx, v in enumerate(row):
            if not v:
                continue
            x = px + sx
            y = py + sy
            if x < 0 or x >= state["w"] or y < 0 or y >= state["h"]:
                return False
            if state["board"][y][x]:
                return False
    return True

def tetris_spawn_piece(state):
    shape = random.choice(TETRIS_SHAPES)
    color = random.randint(1, len(TETRIS_COLORS))
    px = (state["w"] - len(shape[0])) // 2
    py = 0
    if not tetris_can_place(state, px, py, shape):
        state["over"] = True
        return False
    state["piece"] = {"x": px, "y": py, "shape": shape, "color": color}
    return True

def tetris_lock_piece(state):
    p = state.get("piece")
    if not p:
        return
    for sy, row in enumerate(p["shape"]):
        for sx, v in enumerate(row):
            if v:
                state["board"][p["y"] + sy][p["x"] + sx] = p.get("color", 1)
    state["piece"] = None

def tetris_clear_lines(state):
    new_board = []
    cleared = 0
    for row in state["board"]:
        if all(c == 1 for c in row):
            cleared += 1
        else:
            new_board.append(row)
    while len(new_board) < state["h"]:
        new_board.insert(0, [0]*state["w"])
    state["board"] = new_board
    if cleared:
        state["score"] += cleared * 100
    return cleared

def tetris_move(state, dx):
    if state.get("over") or not state.get("piece"):
        return False
    p = state["piece"]
    nx = p["x"] + dx
    if tetris_can_place(state, nx, p["y"], p["shape"]):
        p["x"] = nx
        return True
    return False


def tetris_render(state):
    w, h = state["w"], state["h"]
    view = [[state["board"][y][x] for x in range(w)] for y in range(h)]
    p = state.get("piece")
    if p:
        for sy, row in enumerate(p["shape"]):
            for sx, v in enumerate(row):
                if v:
                    y, x = p["y"] + sy, p["x"] + sx
                    if 0 <= y < h and 0 <= x < w:
                        view[y][x] = p.get("color", 1)

    def cell(value):
        if value == 0:
            return "⬛"
        return TETRIS_COLORS[max(1, min(value, len(TETRIS_COLORS))) - 1]

    lines = ["".join(cell(view[y][x]) for x in range(w)) for y in range(h)]
    text = f"🧱 Тетрис\nОчки: {state['score']}\n\n" + "\n".join(lines)
    if state.get("over"):
        text += "\n\n💀 Игра окончена"
    return text

def tetris_controls(gid, over=False):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("⬅️", callback_data=f"tetris_{gid}_left"),
        types.InlineKeyboardButton("➡️", callback_data=f"tetris_{gid}_right")
    )
    kb.row(types.InlineKeyboardButton("⬇️ Отпустить", callback_data=f"tetris_{gid}_drop"))
    if over:
        kb.row(types.InlineKeyboardButton("🔁 Новая игра", callback_data="tetris_new"))
    return kb

def tetris_retry_after_seconds(err):
    msg = str(err).lower()
    marker = "retry after "
    if marker not in msg:
        return None
    digits = ""
    for ch in msg.split(marker, 1)[1]:
        if not ch.isdigit():
            break
        digits += ch
    return int(digits) if digits else None

def tetris_safe_edit(call, gid, st, force=False):
    now = time.time()
    next_edit_at = st.get("next_edit_at", 0.0)
    if (not force) and now < next_edit_at:
        return False
    try:
        text = tetris_render(st)
        kb = tetris_controls(gid, over=st.get("over", False))
        if getattr(call, "inline_message_id", None):
            bot.edit_message_text(
                text,
                inline_message_id=call.inline_message_id,
                reply_markup=kb
            )
        elif getattr(call, "message", None):
            bot.edit_message_text(
                text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=kb
            )
        else:
            return False
        st["next_edit_at"] = time.time() + 0.12
        return True
    except Exception as e:
        wait = tetris_retry_after_seconds(e)
        if wait:
            st["next_edit_at"] = time.time() + wait + 0.2
            return False
        raise

@bot.inline_handler(lambda q: q.query.lower() == "2048" or q.query.strip() == "2048")
def inline_2048(query):
    if not _inline_guard(query):
        return
    board = [[0]*4 for _ in range(4)]
    board = spawn_tile(board); board = spawn_tile(board)
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("⬆️", callback_data="g2048_new_up"))
    markup.row(types.InlineKeyboardButton("⬅️", callback_data="g2048_new_left"),
               types.InlineKeyboardButton("➡️", callback_data="g2048_new_right"))
    markup.row(types.InlineKeyboardButton("⬇️", callback_data="g2048_new_down"))
    uid = query.from_user.id
    results = [types.InlineQueryResultArticle(
        id=f"g2048_preview_{short_id()}",
        title=f"🔢 {get_game_title(uid, 'g2048')}",
        description=localized_text(uid, "Нажмите стрелку, чтобы начать", "Press an arrow to start", "Натисни стрілку, щоб почати"),
        input_message_content=types.InputTextMessageContent(localized_text(
            uid,
            "🔢 2048\nНажмите кнопку, чтобы начать.",
            "🔢 2048\nPress a button to start.",
            "🔢 2048\nНатисни кнопку, щоб почати.",
        )),
        reply_markup=markup
    )]
    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.inline_handler(lambda q: q.query.lower() == "tetris" or q.query.lower() == "тетрис")
def inline_tetris(query):
    if not _inline_guard(query):
        return
    gid = short_id()
    uid = query.from_user.id
    results = [types.InlineQueryResultArticle(
        id=f"tetris_preview_{gid}",
        title=f"🧱 {get_game_title(uid, 'tetris')}",
        description=localized_text(
            uid,
            "Кнопки влево/вправо/отпустить",
            "Left / right / drop buttons",
            "Кнопки вліво/вправо/відпустити",
        ),
        input_message_content=types.InputTextMessageContent(localized_text(
            uid,
            "🧱 Тетрис\nНажмите «Старт».",
            "🧱 Tetris\nPress «Start».",
            "🧱 Тетріс\nНатисніть «Старт».",
        )),
        reply_markup=types.InlineKeyboardMarkup().add(
            types.InlineKeyboardButton(
                localized_text(uid, "▶️ Старт", "▶️ Start", "▶️ Старт"), callback_data="tetris_new")
        )
    )]
    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rps_"))
def rps_callback(call):
    _track_callback_game_play(call)
    try:
        _, gid, user_choice = call.data.split("_")

        game = rps_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "❌ Игра устарела")
            return
        if game.get("uid") != call.from_user.id:
            bot.answer_callback_query(call.id, "Эта партия не ваша")
            return

        bot_choice = random.choice(["rock", "paper", "scissors"])

        icons = {
            "rock": "🪨 Камень",
            "paper": "📄 Бумага",
            "scissors": "✂️ Ножницы"
        }

        if user_choice == bot_choice:
            result = "🤝 Ничья!"
        elif (
            (user_choice == "rock" and bot_choice == "scissors") or
            (user_choice == "scissors" and bot_choice == "paper") or
            (user_choice == "paper" and bot_choice == "rock")
        ):
            result = "🎉 Вы победили!"
        else:
            result = "😢 Вы проиграли"

        text = (
            "✂️ *Камень • Ножницы • Бумага*\n\n"
            f"👤 Вы: {icons[user_choice]}\n"
            f"🤖 Бот: {icons[bot_choice]}\n\n"
            f"{result}"
        )

        new_gid = short_id()
        rps_games[new_gid] = {"uid": call.from_user.id}

        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🪨 Камень", callback_data=f"rps_{new_gid}_rock"),
            types.InlineKeyboardButton("📄 Бумага", callback_data=f"rps_{new_gid}_paper"),
            types.InlineKeyboardButton("✂️ Ножницы", callback_data=f"rps_{new_gid}_scissors")
        )

        bot.edit_message_text(
            text,
            inline_message_id=call.inline_message_id,
            parse_mode="Markdown",
            reply_markup=kb
        )

        rps_games.pop(gid, None)
        bot.answer_callback_query(call.id, "Игра окончена")
        return

    except Exception as e:
        log_exception("rps", e)
        bot.answer_callback_query(call.id, "❌ Ошибка игры")

@bot.callback_query_handler(func=lambda c: c.data in ["set_msg", "set_btn", "set_title", "set_gui"])
def sys_set_field(call):
    field = call.data.replace("set_", "")  # msg, btn, title, gui
    uid = call.from_user.id

    system_notify_wait[uid] = field
    bot.answer_callback_query(call.id)
    bot.send_message(uid, f"✏ Введите новое значение для поля: {field}")

@bot.callback_query_handler(func=lambda c: c.data == "tetris_new" or c.data.startswith("tetris_"))
def tetris_callback(call):
    _track_callback_game_play(call)
    try:
        data = call.data
        if data == "tetris_new":
            gid = short_id()
            games_tetris[gid] = tetris_new_state()
            st = games_tetris[gid]
            ok = tetris_safe_edit(call, gid, st, force=True)
            if not ok:
                bot.answer_callback_query(call.id, "Подождите 1-2 секунды и нажмите Старт снова", show_alert=True)
                return
            bot.answer_callback_query(call.id, "Тетрис запущен")
            return

        parts = data.split("_", 2)  # tetris_<gid>_<action>
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные")
            return
        gid = parts[1]
        action = parts[2]
        st = games_tetris.get(gid)
        if not st:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        if st.get("over"):
            tetris_safe_edit(call, gid, st, force=True)
            bot.answer_callback_query(call.id, "Игра завершена")
            return

        if action == "left":
            tetris_move(st, -1)
            bot.answer_callback_query(call.id)
        elif action == "right":
            tetris_move(st, 1)
            bot.answer_callback_query(call.id)
        elif action == "drop":
            bot.answer_callback_query(call.id, "Блок отпущен")
            # Плавное падение вместо мгновенного телепорта
            if st.get("piece") and not st.get("over"):
                p = st["piece"]
                start_y = p["y"]
                end_y = start_y
                while tetris_can_place(st, p["x"], end_y + 1, p["shape"]):
                    end_y += 1
                dist = end_y - start_y
                if dist > 0:
                    frames = min(4, dist)
                    last_y = p["y"]
                    for i in range(1, frames + 1):
                        ny = start_y + (dist * i) // frames
                        if ny == last_y:
                            continue
                        p["y"] = ny
                        last_y = ny
                        tetris_safe_edit(call, gid, st)
                        time.sleep(0.07)
                tetris_lock_piece(st)
                tetris_clear_lines(st)
                tetris_spawn_piece(st)
        else:
            bot.answer_callback_query(call.id)

        tetris_safe_edit(call, gid, st, force=True)
    except Exception as e:
        log_exception("tetris", e)
        bot.answer_callback_query(call.id, "Ошибка Тетриса")


@bot.callback_query_handler(func=lambda c: c.data.startswith("g2048_"))
def g2048_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_", 2)
        if parts[1] == "new":
            gid = short_id()
            board = [[0]*4 for _ in range(4)]
            board = spawn_tile(board); board = spawn_tile(board)
            games_2048[gid] = {"board": board}
            direction = parts[2]
        else:
            gid = parts[1]
            direction = parts[2]
            if gid not in games_2048:
                bot.answer_callback_query(call.id, "Игра не найдена")
                return
            board = games_2048[gid]["board"]

        new_board, moved = move_board(board, direction)
        if moved:
            new_board = spawn_tile(new_board)
        games_2048[gid] = {"board": new_board}

        flat = sum(new_board, [])
        if 2048 in flat:
            bot.edit_message_text("🎉 Вы собрали 2048! Победа!", inline_message_id=call.inline_message_id)
            games_2048.pop(gid, None)
            bot.answer_callback_query(call.id)
            return

        moves_possible = False
        for y in range(4):
            for x in range(4):
                if new_board[y][x] == 0:
                    moves_possible = True
                if x<3 and new_board[y][x] == new_board[y][x+1]:
                    moves_possible = True
                if y<3 and new_board[y][x] == new_board[y+1][x]:
                    moves_possible = True
        if not moves_possible:
            bot.edit_message_text("💀 Game over — ходов нет.", inline_message_id=call.inline_message_id)
            games_2048.pop(gid, None)
            bot.answer_callback_query(call.id)
            return

        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("⬆️", callback_data=f"g2048_{gid}_up"))
        markup.row(types.InlineKeyboardButton("⬅️", callback_data=f"g2048_{gid}_left"),
                   types.InlineKeyboardButton("➡️", callback_data=f"g2048_{gid}_right"))
        markup.row(types.InlineKeyboardButton("⬇️", callback_data=f"g2048_{gid}_down"))
        bot.edit_message_text(f"🔢 2048\n\n{render_2048(new_board)}", inline_message_id=call.inline_message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("2048", e)
        bot.answer_callback_query(call.id, "Ошибка 2048")

def render_pong_state(state):
    W, H = 11, 7
    field = [["⬛" for _ in range(W)] for _ in range(H)]
    p1x, p2x = 1, 9
    p1pos, p2pos = state["paddles"][0], state["paddles"][1]
    if 0 <= p1pos < H:
        field[p1pos][p1x] = "🟦"
    if 0 <= p2pos < H:
        field[p2pos][p2x] = "🟩"
    bx, by = state["ball"][0], state["ball"][1]
    if 0 <= bx < W and 0 <= by < H:
        field[by][bx] = "⚪"
    return "\n".join("".join(r) for r in field)

def _new_pong_state():
    return {
        "players": [None, None],
        "paddles": [3, 3],
        "ball": [5, 3, -1, 1],
        "started": False,
        "score": [0, 0],
        "winner": None,
        "loop_running": False,
        "inline_id": None,
    }

def _pong_controls_markup(gid, started=False, game_over=False):
    markup = types.InlineKeyboardMarkup()
    if game_over:
        markup.add(types.InlineKeyboardButton("🔄 Новая игра", callback_data=f"pong_{gid}_restart"))
        return markup
    markup.row(
        types.InlineKeyboardButton("⬆️", callback_data=f"pong_{gid}_U"),
        types.InlineKeyboardButton("⬇️", callback_data=f"pong_{gid}_D"),
    )
    if not started:
        markup.add(types.InlineKeyboardButton("▶️ Старт", callback_data=f"pong_{gid}_start"))
    return markup

def _render_pong_text(state):
    score = state.get("score", [0, 0])
    lines = [
        "🏓 Пинг-понг",
        f"Счёт: {score[0]} : {score[1]}",
    ]
    if state.get("winner") is not None:
        winner_idx = state["winner"] + 1
        side = "слева" if state["winner"] == 0 else "справа"
        lines.append(f"Победил Игрок {winner_idx} ({side})")
    elif not state.get("started"):
        lines.append("Подключитесь вдвоём и нажмите «Старт».")
    lines.append("")
    lines.append(render_pong_state(state))
    return "\n".join(lines)

def _pong_reset_ball(state, direction=None):
    dx = direction if direction in (-1, 1) else random.choice([-1, 1])
    dy = random.choice([-1, 1])
    state["ball"] = [5, random.randint(1, 5), dx, dy]

def _pong_step(state):
    W, H = 11, 7
    p1x, p2x = 1, 9
    bx, by, dx, dy = state["ball"]
    bx += dx
    by += dy

    if by <= 0:
        by = 0
        dy = 1
    elif by >= H - 1:
        by = H - 1
        dy = -1

    if bx == p1x and by == state["paddles"][0]:
        dx = 1
        bx = p1x + 1
    elif bx == p2x and by == state["paddles"][1]:
        dx = -1
        bx = p2x - 1
    elif bx < 0:
        state["score"][1] += 1
        if state["score"][1] >= 5:
            state["winner"] = 1
            state["started"] = False
        else:
            _pong_reset_ball(state, direction=1)
            return
    elif bx >= W:
        state["score"][0] += 1
        if state["score"][0] >= 5:
            state["winner"] = 0
            state["started"] = False
        else:
            _pong_reset_ball(state, direction=-1)
            return

    state["ball"] = [bx, by, dx, dy]

@bot.inline_handler(lambda q: q.query.lower() == "pong" or q.query.strip() == "pong" or q.query.lower() == "ping-pong")
def inline_pong(query):
    if not _inline_guard(query):
        return
    gid = short_id()
    markup = types.InlineKeyboardMarkup()
    uid = query.from_user.id
    markup.add(types.InlineKeyboardButton(
        localized_text(uid, "Присоединиться", "Join", "Приєднатися"), callback_data=f"pong_{gid}_join"))
    results = [types.InlineQueryResultArticle(
        id=f"pong_preview_{gid}",
        title=f"🏓 {get_game_title(uid, 'pong')} " + localized_text(uid, "(2 игрока)", "(2 players)", "(2 гравці)"),
        description=localized_text(
            uid,
            "Нажмите 'Присоединиться' чтобы стать игроком",
            "Press 'Join' to take a seat",
            "Натисніть 'Приєднатися', щоб стати гравцем",
        ),
        input_message_content=types.InputTextMessageContent(localized_text(
            uid,
            "🏓 Пинг-понг\nНажмите 'Присоединиться', дождитесь второго игрока и начните матч.",
            "🏓 Ping-Pong\nPress 'Join', wait for the second player and start the match.",
            "🏓 Пінг-понг\nНатисніть 'Приєднатися', дочекайтеся другого гравця та почніть матч.",
        )),
        reply_markup=markup
    )]
    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("pong_"))
def pong_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_", 2)
        gid = parts[1]
        action = parts[2] if len(parts) > 2 else ""
        state = games_pong.get(gid)
        if action == "join":
            if state is None:
                state = _new_pong_state()
                games_pong[gid] = state
            state["inline_id"] = call.inline_message_id
            uid = call.from_user.id
            if uid in state["players"]:
                bot.answer_callback_query(call.id, "Вы уже в игре")
                return
            if state["players"][0] is None:
                state["players"][0] = uid
                msg = "Вы — Игрок 1 (слева)"
            elif state["players"][1] is None:
                state["players"][1] = uid
                msg = "Вы — Игрок 2 (справа)"
            else:
                bot.answer_callback_query(call.id, "Пати заполнен.")
                return
            if state["players"][0] and state["players"][1]:
                safe_edit_message(call, _render_pong_text(state), reply_markup=_pong_controls_markup(gid, started=False))
            else:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"pong_{gid}_join"))
                safe_edit_message(call, f"{msg}\nОжидаем второго игрока...", reply_markup=markup)
            bot.answer_callback_query(call.id, msg)
            return

        if state is None:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return
        state["inline_id"] = call.inline_message_id or state.get("inline_id")
        uid = call.from_user.id
        if uid not in state["players"]:
            bot.answer_callback_query(call.id, "Вы не участник игры")
            return

        if action == "restart":
            games_pong[gid] = _new_pong_state()
            games_pong[gid]["inline_id"] = state.get("inline_id")
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"pong_{gid}_join"))
            safe_edit_message(call, "🏓 Пинг-понг\nНажмите 'Присоединиться' чтобы игра началась.", reply_markup=markup)
            bot.answer_callback_query(call.id, "Игра сброшена")
            return

        if action in ("U", "D"):
            pidx = 0 if uid == state["players"][0] else 1
            if action == "U":
                state["paddles"][pidx] = max(0, state["paddles"][pidx] - 1)
            else:
                state["paddles"][pidx] = min(6, state["paddles"][pidx] + 1)
            safe_edit_message(call, _render_pong_text(state), reply_markup=_pong_controls_markup(gid, started=state.get("started", False)))
            bot.answer_callback_query(call.id, "Платформа сдвинута")
            return

        if action == "start":
            if state["started"]:
                bot.answer_callback_query(call.id, "Игра уже запущена")
                return
            if not all(state["players"]):
                bot.answer_callback_query(call.id, "Нужны 2 игрока")
                return
            state["started"] = True
            _pong_reset_ball(state)
            safe_edit_message(call, _render_pong_text(state), reply_markup=_pong_controls_markup(gid, started=True))
            if not state.get("loop_running") and state.get("inline_id"):
                state["loop_running"] = True
                Thread(target=pong_game_loop, args=(gid, state["inline_id"]), daemon=True).start()
            bot.answer_callback_query(call.id, "Старт!")
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("pong", e)
        bot.answer_callback_query(call.id, "Ошибка Pong")

@bot.callback_query_handler(func=lambda c: c.data.startswith("millionaire_"))
def millionaire_callback(call):
    _track_callback_game_play(call)
    try:
        _, game_id, index = call.data.split("_")
        index = int(index)
        game = millionaire_games.get(game_id)
        if not game:
            bot.answer_callback_query(call.id, "Игра завершена!")
            return
        question = game["question"]
        answer = question["options"][index]
        if answer == question["answer"]:
            bot.edit_message_text(f"🎉 Правильно! Ответ: {answer}", inline_message_id=call.inline_message_id)
            millionaire_games.pop(game_id, None)
            return
        game["attempts"] -= 1
        if game["attempts"] == 0:
            bot.edit_message_text(f"💀 Вы проиграли!\nПравильный ответ: {question['answer']}", inline_message_id=call.inline_message_id)
            millionaire_games.pop(game_id, None)
            return
        markup = types.InlineKeyboardMarkup()
        for i, option in enumerate(question["options"]):
            markup.add(types.InlineKeyboardButton(option, callback_data=f"millionaire_{game_id}_{i}"))
        bot.edit_message_text(f"💰 {question['question']}\nОсталось попыток: {game['attempts']}", inline_message_id=call.inline_message_id, reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("mill", e)
        bot.answer_callback_query(call.id, "Ошибка Миллионера")

minesweeper_games = {}

def generate_minesweeper_board(size=5, mines=5):
    board = [[0 for _ in range(size)] for _ in range(size)]
    mine_positions = random.sample([(i, j) for i in range(size) for j in range(size)], mines)
    for x, y in mine_positions:
        board[x][y] = -1
        for dx in [-1,0,1]:
            for dy in [-1,0,1]:
                nx, ny = x+dx, y+dy
                if 0 <= nx < size and 0 <= ny < size and board[nx][ny] != -1:
                    board[nx][ny] += 1
    return board, mine_positions

HANGMAN_STAGES = [
    "┌─────┐\n│     |\n│\n│\n│\n│\n└─────",
    "┌─────┐\n│     |\n│     O\n│\n│\n│\n└─────",
    "┌─────┐\n│     |\n│     O\n│     |\n│\n│\n└─────",
    "┌─────┐\n│     |\n│     O\n│    \\|\n│\n│\n└─────",
    "┌─────┐\n│     |\n│     O\n│    \\|/\n│\n│\n└─────",
    "┌─────┐\n│     |\n│     O\n│    \\|/\n│     |\n│\n└─────",
    "┌─────┐\n│     |\n│     O\n│    \\|/\n│     |\n│    / \\\n└─────",
]
HANGMAN_ALPHABET = "абвгдежзийклмнопрстуфхцчшщъыьэюя"


def _hangman_new_game(word=None):
    word = word or random.choice(list(HANGMAN_WORDS))
    return {
        "word": word,
        "hint": HANGMAN_WORDS[word],
        "guessed": set(),
        "wrong": set(),
        "attempts": 6,
        "hint_used": False,
    }


def _hangman_word_guessed(game):
    return all(letter.lower() in game["guessed"] for letter in game["word"])


def render_hangman_state(game):
    wrong = game["wrong"]
    attempts = game["attempts"]
    display = "".join(
        (letter.upper() if letter.lower() in game["guessed"] else "_") + " "
        for letter in game["word"]
    )

    text = "```\n" + HANGMAN_STAGES[min(len(wrong), len(HANGMAN_STAGES) - 1)] + "\n```\n\n"
    text += f"Слово: `{display}`\n"
    text += f"Ошибки: {', '.join(sorted(c.upper() for c in wrong)) if wrong else '-'}\n"
    text += f"Попыток: {attempts - len(wrong)}/{attempts}\n"

    if game.get("hint_used"):
        text += f"\n💡 Подсказка: {game.get('hint', '')}"

    return text

def render_hangman_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup()
    guessed = game["guessed"]
    wrong = game["wrong"]

    if len(wrong) >= game["attempts"] or _hangman_word_guessed(game):
        kb.add(types.InlineKeyboardButton("🔄 Новая игра", callback_data="hangman_new"))
        return kb

    if game.get("hint_used"):
        kb.add(types.InlineKeyboardButton("✓ Подсказка использована", callback_data="none"))
    else:
        kb.add(types.InlineKeyboardButton("💡 Подсказка", callback_data=f"hangman_hint_{gid}"))

    row = []
    for letter in HANGMAN_ALPHABET:
        used = letter in guessed or letter in wrong
        row.append(types.InlineKeyboardButton(
            "✓" if used else letter.upper(),
            callback_data="none" if used else f"hangman_{gid}_{letter}",
        ))
        if len(row) == 5:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)

    kb.add(types.InlineKeyboardButton("🔄 Новая игра", callback_data="hangman_new"))
    return kb

@bot.inline_handler(lambda q: q.query.lower() in ("hangman", "виселица"))
def inline_hangman(query):
    if not _inline_guard(query):
        return
    gid = short_id()
    game = hangman_games[gid] = _hangman_new_game()
    uid = query.from_user.id

    results = [types.InlineQueryResultArticle(
        id=f"hangman_{gid}",
        title=f"🔤 {get_game_title(uid, 'hangman')}",
        description=get_game_description(uid, "hangman"),
        input_message_content=types.InputTextMessageContent(render_hangman_state(game)),
        reply_markup=render_hangman_keyboard(gid, game)
    )]
    
    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("hangman_"))
def hangman_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        action = parts[1]

        if action == "new":
            gid = short_id()
            game = hangman_games[gid] = _hangman_new_game()
            bot.edit_message_text(
                render_hangman_state(game),
                inline_message_id=call.inline_message_id,
                reply_markup=render_hangman_keyboard(gid, game)
            )
            bot.answer_callback_query(call.id, "Новая игра!")
            return

        if action == "hint":
            gid = parts[2]
            game = hangman_games.get(gid)
            if not game:
                bot.answer_callback_query(call.id, "Игра завершена!")
                return
            if game.get("hint_used"):
                bot.answer_callback_query(call.id, "Подсказка уже использована!")
                return

            game["hint_used"] = True
            bot.edit_message_text(
                render_hangman_state(game),
                inline_message_id=call.inline_message_id,
                reply_markup=render_hangman_keyboard(gid, game)
            )
            bot.answer_callback_query(call.id, f"💡 {game.get('hint', '')}")
            return

        gid, letter = parts[1], parts[2]
        game = hangman_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра завершена!")
            return

        word = game["word"]
        guessed = game["guessed"]
        wrong = game["wrong"]
        attempts = game["attempts"]

        if len(wrong) >= attempts:
            bot.answer_callback_query(call.id, f"Игра окончена! Слово: {word.upper()}")
            return
        if _hangman_word_guessed(game):
            bot.answer_callback_query(call.id, "Вы уже выиграли!")
            return
        if letter in guessed or letter in wrong:
            bot.answer_callback_query(call.id, "Вы уже выбрали эту букву!")
            return

        if letter.lower() in word.lower():
            guessed.add(letter)
            bot.answer_callback_query(call.id, "✅ Верно!")
        else:
            wrong.add(letter)
            bot.answer_callback_query(call.id, "❌ Неверно!")

        text = render_hangman_state(game)
        if _hangman_word_guessed(game):
            text += f"\n\n🎉 Вы выиграли! Слово: {word.upper()}"
        elif len(wrong) >= attempts:
            text += f"\n\n💀 Вы проиграли! Слово: {word.upper()}"

        bot.edit_message_text(
            text,
            inline_message_id=call.inline_message_id,
            reply_markup=render_hangman_keyboard(gid, game)
        )

    except Exception as e:
        log_exception("hangman", e)
        bot.answer_callback_query(call.id, "Ошибка Виселицы")

def render_minesweeper_board(board, revealed):
    def cell(i, j):
        if (i, j) not in revealed:
            return "⬛ "
        if board[i][j] == -1:
            return "💣 "
        return "⬜ " if board[i][j] == 0 else f"{board[i][j]}️⃣ "

    size = len(board)
    return "".join("".join(cell(i, j) for j in range(size)) + "\n" for i in range(size))

def _minesweeper_build_markup(gid, board, revealed):
    markup = types.InlineKeyboardMarkup()
    for i in range(len(board)):
        markup.row(*[
            types.InlineKeyboardButton("⬜", callback_data="none")
            if (i, j) in revealed
            else types.InlineKeyboardButton("⬛", callback_data=f"minesweeper_{gid}_{i}_{j}")
            for j in range(len(board))
        ])
    return markup

def start_minesweeper_in_chat(chat_id):
    board, mine_positions = generate_minesweeper_board()
    gid = short_id()
    revealed = set()
    minesweeper_games[gid] = {"board": board, "revealed": revealed, "mine_positions": mine_positions}
    bot.send_message(
        chat_id,
        f"💣 Сапёр\n{render_minesweeper_board(board, revealed)}",
        reply_markup=_minesweeper_build_markup(gid, board, revealed),
    )

@bot.inline_handler(lambda q: q.query.lower() in ("слова", "word_duel"))
def inline_word_duel(query):
    if not _inline_guard(query):
        return

    gid = short_id()
    uid = query.from_user.id
    first_word = random.choice(WORD_LIST)
    word_games[gid] = {
        "word": first_word,
        "player1": uid,
        "p1_name": query.from_user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"),
        "player2": None,
        "scores": {}
    }

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        localized_text(uid, "Присоединиться", "Join", "Приєднатися"), callback_data=f"wordgame_join_{gid}"))

    results = [types.InlineQueryResultArticle(
        id=f"wordgame_{gid}",
        title=f"📝 {get_game_title(uid, 'wordgame')}",
        description=get_game_description(uid, "wordgame"),
        input_message_content=types.InputTextMessageContent(
            localized_text(
                uid,
                f"📝 *Словесная дуэль*\n\nПервое слово: `{first_word.upper()}`\n\n"
                f"Следующий игрок должен написать слово, начинающееся на '{first_word[-1].upper()}'\n\n"
                "Давайте играть!",
                f"📝 *Word Duel*\n\nFirst word: `{first_word.upper()}`\n\n"
                f"The next player must write a word starting with '{first_word[-1].upper()}'\n\n"
                "Let's play!",
                f"📝 *Словесна дуель*\n\nПерше слово: `{first_word.upper()}`\n\n"
                f"Наступний гравець має написати слово, що починається на '{first_word[-1].upper()}'\n\n"
                "Граймо!",
            ),
            parse_mode="Markdown"
        ),
        reply_markup=kb
    )]

    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.inline_handler(lambda q: q.query.lower() in ("викторина", "quiz"))
def inline_quiz_game(query):
    if not _inline_guard(query):
        return

    gid = short_id()
    qdata = random.choice(QUIZ_QUESTIONS)
    uid = query.from_user.id
    quiz_games[gid] = _quiz_new_game(
        qdata, uid, query.from_user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"))

    results = [types.InlineQueryResultArticle(
        id=f"quizgame_{gid}",
        title=f"🧠 {get_game_title(uid, 'quizgame')} " + localized_text(uid, "- кто быстрее", "- who is faster", "- хто швидше"),
        description=get_game_description(uid, "quizgame"),
        input_message_content=types.InputTextMessageContent(_quiz_intro_text(qdata["q"]), parse_mode="Markdown"),
        reply_markup=_quiz_join_kb(gid)
    )]

    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.inline_handler(lambda q: q.query.lower() in ("комбо", "combo"))
def inline_combo_battle(query):
    if not _inline_guard(query):
        return

    gid = short_id()
    uid = query.from_user.id
    combo_games[gid] = _combo_new_game(
        uid, query.from_user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"))

    results = [types.InlineQueryResultArticle(
        id=f"combogame_{gid}",
        title=f"⚡ {get_game_title(uid, 'combogame')}",
        description=get_game_description(uid, "combogame"),
        input_message_content=types.InputTextMessageContent(COMBO_INTRO_TEXT, parse_mode="Markdown"),
        reply_markup=_combo_join_kb(gid)
    )]

    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.inline_handler(lambda q: q.query.lower() in ("мафия", "mafia"))
def inline_mafia_game(query):
    if not _inline_guard(query):
        return

    gid = short_id()
    uid = query.from_user.id
    mafia_games[gid] = mafia_new_game(
        uid, query.from_user.first_name or localized_text(uid, "Игрок 1", "Player 1", "Гравець 1"))

    results = [types.InlineQueryResultArticle(
        id=f"mafia_{gid}",
        title=f"🎭 {get_game_title(uid, 'mafia')}",
        description=localized_text(uid, "Нужно 4-10 игроков", "Needs 4-10 players", "Потрібно 4-10 гравців"),
        input_message_content=types.InputTextMessageContent(localized_text(
            uid,
            "🎭 Мафия\n\nСоздано лобби. Нажмите «Присоединиться», затем «Старт».",
            "🎭 Mafia\n\nLobby created. Press «Join», then «Start».",
            "🎭 Мафія\n\nСтворено лобі. Натисніть «Приєднатися», потім «Старт».",
        )),
        reply_markup=mafia_build_lobby_kb(gid)
    )]
    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

def _mafia_keyboard(gid, game):
    phase = game.get("phase")
    if phase == "night":
        return mafia_build_night_kb(gid, game)
    if phase == "day":
        return mafia_build_day_kb(gid, game)
    if phase == "ended":
        return None
    return mafia_build_lobby_kb(gid)


def _mafia_refresh(call, gid, game):
    safe_edit_message(call, mafia_render_text(game), reply_markup=_mafia_keyboard(gid, game))


def _mafia_finish_if_over(game):
    winner = mafia_check_winner(game)
    if not winner:
        return False
    game["phase"] = "ended"
    game["last_event"] = "\U0001f3c6 Победили мирные жители!" if winner == "citizens" else "\U0001f480 Победила мафия!"
    return True


@bot.callback_query_handler(func=lambda c: c.data.startswith("mafia_"))
def mafia_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        action = parts[1] if len(parts) > 1 else ""
        gid = parts[2] if len(parts) > 2 else ""
        game = mafia_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        uid = call.from_user.id
        target = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else None

        if action == "join":
            if game["phase"] != "lobby":
                bot.answer_callback_query(call.id, "Игра уже началась", show_alert=True)
                return
            if uid in game["players"]:
                bot.answer_callback_query(call.id, "Вы уже в лобби")
                return
            if len(game["players"]) >= 10:
                bot.answer_callback_query(call.id, "Мест больше нет (максимум 10)", show_alert=True)
                return
            game["players"].append(uid)
            game["alive"].append(uid)
            game["names"][uid] = call.from_user.first_name or call.from_user.username or str(uid)
            game["last_event"] = f"\u2795 {game['names'][uid]} присоединился."
            _mafia_refresh(call, gid, game)
            bot.answer_callback_query(call.id, "Вы в игре!")
            return

        if action == "role":
            role = game["roles"].get(uid)
            if not role:
                bot.answer_callback_query(call.id, "Роли ещё не розданы", show_alert=True)
                return
            bot.answer_callback_query(call.id, f"Ваша роль: {MAFIA_ROLE_NAMES.get(role, role)}", show_alert=True)
            return

        if action == "start":
            if uid != game.get("owner"):
                bot.answer_callback_query(call.id, "Начать может только создатель", show_alert=True)
                return
            if game["phase"] != "lobby":
                bot.answer_callback_query(call.id, "Игра уже началась")
                return
            if len(game["players"]) < 4:
                bot.answer_callback_query(call.id, "Нужно минимум 4 игрока", show_alert=True)
                return
            game["roles"] = mafia_assign_roles(game["players"])
            game["phase"] = "night"
            game["last_event"] = "\U0001f319 Роли розданы. Наступает ночь."
            _mafia_refresh(call, gid, game)
            for player_id in game["players"]:
                _record_game_play_once(player_id, "mafia", f"mafia_{gid}", display_name=game["names"].get(player_id))
            bot.answer_callback_query(call.id, "Игра началась!")
            return

        if uid not in game["alive"]:
            bot.answer_callback_query(call.id, "Вы не в игре или уже выбыли", show_alert=True)
            return

        if action in ("nkill", "heal", "check"):
            if game["phase"] != "night":
                bot.answer_callback_query(call.id, "Сейчас не ночь")
                return
            required_role = {"nkill": "mafia", "heal": "doctor", "check": "detective"}[action]
            if game["roles"].get(uid) != required_role:
                bot.answer_callback_query(call.id, "Это действие не для вашей роли", show_alert=True)
                return
            if target not in game["alive"]:
                bot.answer_callback_query(call.id, "Игрок уже выбыл")
                return

            if action == "check":
                game["night"]["check"] = target
                is_mafia = game["roles"].get(target) == "mafia"
                bot.answer_callback_query(
                    call.id,
                    f"{game['names'].get(target, 'Игрок')}: {'мафия' if is_mafia else 'не мафия'}",
                    show_alert=True,
                )
                return

            game["night"]["kill" if action == "nkill" else "heal"] = target
            bot.answer_callback_query(call.id, "Действие принято")
            if game["night"]["kill"] is not None:
                mafia_resolve_night(game)
                _mafia_finish_if_over(game)
                _mafia_refresh(call, gid, game)
            return

        if action == "vote":
            if game["phase"] != "day":
                bot.answer_callback_query(call.id, "Голосование ещё не началось")
                return
            if target not in game["alive"]:
                bot.answer_callback_query(call.id, "Игрок уже выбыл")
                return
            game["votes"][uid] = target
            bot.answer_callback_query(call.id, "Голос учтён")
            if len(game["votes"]) >= len(game["alive"]):
                mafia_resolve_day(game)
                _mafia_finish_if_over(game)
                _mafia_refresh(call, gid, game)
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("mafia_callback", e, user_id=getattr(call.from_user, "id", None))
        try:
            bot.answer_callback_query(call.id, "Ошибка Мафии")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("wordgame_join_"))
def wordgame_join(call):
    _track_callback_game_play(call)
    try:
        gid = call.data.split("_")[2]
        game = word_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return
        
        if game["player2"] is None:
            game["player2"] = call.from_user.id
            game["p2_name"] = call.from_user.first_name or "Игрок 2"
            game["scores"][call.from_user.id] = 0
            game["scores"][game["player1"]] = 0
            
            text = f"📝 *Словесная дуэль*\n\n"
            text += f"Слово: `{game['word'].upper()}`\n"
            text += f"{game.get('p1_name', 'Игрок 1')}\n"
            text += f"{game.get('p2_name', 'Игрок 2')}\n\n"
            text += f"⏳ Ожидание начала игры...\n"
            text += f"Следующее слово должно начинаться на '{game['word'][-1].upper()}'\n\n"
            text += f"Оба игрока готовы! Поиграем!"
            
            kb = types.InlineKeyboardMarkup()
            row = []
            for i, letter in enumerate("абвгдежзийклмнопрстуфхцчшщъyэюя".replace('y','й')):
                if i % 5 == 0 and i > 0:
                    kb.row(*row)
                    row = []
                row.append(types.InlineKeyboardButton(letter.upper(), callback_data=f"word_{gid}_{letter}"))
            if row:
                kb.row(*row)
            kb.add(types.InlineKeyboardButton("✅ Отправить слово", callback_data=f"word_{gid}_submit"))
            
            bot.edit_message_text(text, inline_message_id=call.inline_message_id, parse_mode="Markdown", reply_markup=kb)
            bot.answer_callback_query(call.id, "✅ Вы присоединились!")
        else:
            bot.answer_callback_query(call.id, "Игрок уже присоединился", show_alert=True)
    except Exception as e:
        log_exception("wordgame_join", e)
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("quizgame_join_"))
def quizgame_join(call):
    _track_callback_game_play(call)
    try:
        gid = call.data.split("_")[2]
        game = quiz_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        _quiz_normalize_game(game)
        players = game["players"]
        uid = call.from_user.id

        if uid in players and not game.get("started"):
            safe_edit_message(
                call,
                _quiz_status_text(game, "Нажмите «Присоединиться», чтобы начать игру."),
                reply_markup=_quiz_join_kb(gid, owner_can_start=game.get("owner") == uid),
                parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id, "Ожидаем игроков")
            return

        if game.get("locked"):
            bot.answer_callback_query(call.id, "Игра уже началась", show_alert=True)
            return

        if len(players) >= game.get("max_players", 4):
            bot.answer_callback_query(call.id, "Игра заполнена (максимум 4)")
            return

        if uid not in players:
            players.append(uid)
            game["names"][uid] = call.from_user.first_name or f"Игрок {len(players)}"
            game["inputs"].setdefault(uid, "")
            game["answered"].setdefault(uid, False)
            game["correct"].setdefault(uid, False)

        if len(players) >= 2:
            game["started"] = True

        if game["started"]:
            text = _quiz_status_text(game, "\nНабирайте ответ на клавиатуре ниже.")
            kb = _quiz_input_kb(gid)
        else:
            text = _quiz_status_text(game, "\nЖдём ещё игроков...")
            kb = _quiz_join_kb(gid, owner_can_start=game.get("owner") == uid)

        safe_edit_message(call, text, reply_markup=kb, parse_mode="Markdown")
        bot.answer_callback_query(call.id, "✅ Вы присоединились!")
    except Exception as e:
        log_exception("quizgame_join", e, user_id=getattr(call.from_user, "id", None))
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("quizgame_start_"))
def quizgame_start(call):
    _track_callback_game_play(call)
    try:
        gid = call.data.split("_")[2]
        game = quiz_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return
        _quiz_normalize_game(game)
        if call.from_user.id != game.get("owner"):
            bot.answer_callback_query(call.id, "Только создатель может начать", show_alert=True)
            return
        if len(game["players"]) < 2:
            bot.answer_callback_query(call.id, "Нужно минимум 2 игрока")
            return

        game["started"] = True
        safe_edit_message(
            call,
            _quiz_status_text(game, "\nНабирайте ответ на клавиатуре ниже."),
            reply_markup=_quiz_input_kb(gid),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id, "Игра началась")
    except Exception as e:
        log_exception("quizgame_start", e, user_id=getattr(call.from_user, "id", None))
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("quiz_"))
def quiz_input(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные")
            return
        gid, token = parts[1], parts[2]
        game = quiz_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        _quiz_normalize_game(game)
        uid = call.from_user.id
        if uid not in game["players"]:
            bot.answer_callback_query(call.id, "Вы не участник этой игры", show_alert=True)
            return
        if not game.get("started"):
            bot.answer_callback_query(call.id, "Ждём игроков...")
            return
        if game["answered"].get(uid):
            bot.answer_callback_query(call.id, "Вы уже ответили")
            return

        if token == "submit":
            answer = (game["inputs"].get(uid, "") or "").strip().lower()
            if not answer:
                bot.answer_callback_query(call.id, "Введите ответ")
                return

            game["locked"] = True
            game["answered"][uid] = True
            game["correct"][uid] = answer == game.get("answer", "").lower()

            if game["correct"][uid]:
                text = f"🎉 {game['names'].get(uid, 'Игрок')} выиграл!\n\n"
            elif all(game["answered"].get(p, False) for p in game["players"]):
                text = "🤷 Никто не угадал.\n\n"
            else:
                bot.answer_callback_query(call.id, "Неверно. Ждём ответы остальных.")
                return

            text += f"❓ {game['question']}\n\n"
            text += f"✅ Ответ: {game['answer']}"
            safe_edit_message(call, text, parse_mode="Markdown")
            quiz_games.pop(gid, None)
            return

        current = game["inputs"].get(uid, "")
        if token == "back":
            game["inputs"][uid] = current[:-1]
        elif len(current) >= 32:
            bot.answer_callback_query(call.id, "Слишком длинный ответ")
            return
        else:
            game["inputs"][uid] = current + token

        safe_edit_message(
            call,
            _quiz_status_text(game, "\nНажмите «Готово», когда закончите."),
            reply_markup=_quiz_input_kb(gid),
            parse_mode="Markdown",
        )
        bot.answer_callback_query(call.id, f"Ваш ответ: {game['inputs'][uid]}")
    except Exception as e:
        log_exception("quiz_input", e, user_id=getattr(call.from_user, "id", None))
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("combogame_join_"))
def combogame_join(call):
    _track_callback_game_play(call)
    try:
        gid = call.data.split("_")[2]
        game = combo_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        p1_name = game.get("p1_name", "Игрок 1")
        if call.from_user.id == game.get("p1"):
            safe_edit_message(
                call,
                f"⚡ *Комбо-битва*\n\n⏳ Ожидание второго игрока...\n\n{p1_name}\n\nНажмите «Присоединиться», чтобы начать игру.",
                reply_markup=_combo_join_kb(gid),
                parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id, "Ожидаем второго игрока")
            return

        if game["p2"] is not None:
            bot.answer_callback_query(call.id, "Игрок уже присоединился")
            return

        game["p2"] = call.from_user.id
        game["p2_name"] = call.from_user.first_name or "Игрок 2"
        game["scores"][call.from_user.id] = 0

        text = (
            f"⚡ *Комбо-битва*\n\n"
            f"✅ Оба игрока готовы!\n\n"
            f"{p1_name}\n{game['p2_name']}\n\n"
            f"Раунд 1 из 3\n\n"
            f"Правила:\n⚡ > 🪨\n🪨 > 🛡️\n🛡️ > ⚡\n\n"
            f"Выбирайте приём:"
        )
        bot.edit_message_text(text, inline_message_id=call.inline_message_id, parse_mode="Markdown", reply_markup=_combo_move_kb(gid))
        bot.answer_callback_query(call.id, "✅ Вы присоединились!")
    except Exception as e:
        log_exception("combogame_join", e, user_id=getattr(call.from_user, "id", None))
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("combo_"))
def combo_choice(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        gid, choice = parts[1], parts[2]
        game = combo_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return
        if choice not in COMBO_CHOICES:
            bot.answer_callback_query(call.id, "Неверный приём")
            return

        uid = call.from_user.id
        p1, p2 = game.get("p1"), game.get("p2")
        if uid == p1:
            my_key, other_key = "p1_choice", "p2_choice"
        elif uid == p2:
            my_key, other_key = "p2_choice", "p1_choice"
        else:
            bot.answer_callback_query(call.id, "Вы не участник этой партии")
            return

        if p2 is None:
            bot.answer_callback_query(call.id, "Ждём второго игрока")
            return
        if game[my_key] is not None:
            bot.answer_callback_query(call.id, "Вы уже выбрали!")
            return

        game[my_key] = choice
        bot.answer_callback_query(call.id, f"✅ Вы выбрали: {COMBO_CHOICES[choice]}")

        p1_name = game.get("p1_name", "Игрок 1")
        p2_name = game.get("p2_name", "Игрок 2")

        if game[other_key] is None:
            waiting_line = f"{p1_name}: {'✅ выбрал' if game['p1_choice'] else '⏳ ждём выбор'}"
            waiting_line += f"\n{p2_name}: {'✅ выбрал' if game['p2_choice'] else '⏳ ждём выбор'}"
            text = f"⚡ *Комбо-битва*\n\nРаунд {game['round']} из 3\n\n{waiting_line}\n\nВыбирайте приём:"
            bot.edit_message_text(text, inline_message_id=call.inline_message_id, parse_mode="Markdown", reply_markup=_combo_move_kb(gid))
            return

        c1, c2 = game["p1_choice"], game["p2_choice"]
        if c1 == c2:
            result = "🤝 Ничья!"
        elif COMBO_BEATS[c1] == c2:
            result = f"🎉 {p1_name} выигрывает раунд!"
            game["scores"][p1] = game["scores"].get(p1, 0) + 1
        else:
            result = f"🎉 {p2_name} выигрывает раунд!"
            game["scores"][p2] = game["scores"].get(p2, 0) + 1

        text = (
            f"⚡ *Результат раунда {game['round']} из 3*\n\n"
            f"{p1_name}: {COMBO_CHOICES[c1]}\n"
            f"{p2_name}: {COMBO_CHOICES[c2]}\n\n"
            f"{result}\n\n"
            f"Счёт: {p1_name}: {game['scores'].get(p1, 0)} - {p2_name}: {game['scores'].get(p2, 0)}"
        )

        if game["round"] < 3:
            game["round"] += 1
            game["p1_choice"] = None
            game["p2_choice"] = None
            text += f"\n\nРаунд {game['round']} - Выбирайте:"
            bot.edit_message_text(text, inline_message_id=call.inline_message_id, parse_mode="Markdown", reply_markup=_combo_move_kb(gid))
            return

        s1, s2 = game["scores"].get(p1, 0), game["scores"].get(p2, 0)
        if s1 > s2:
            text += f"\n\n🏆 {p1_name} победил!"
        elif s2 > s1:
            text += f"\n\n🏆 {p2_name} победил!"
        else:
            text += "\n\n🤝 Ничья!"
        bot.edit_message_text(text, inline_message_id=call.inline_message_id, parse_mode="Markdown")
        combo_games.pop(gid, None)
    except Exception as e:
        log_exception("combo_choice", e, user_id=getattr(call.from_user, "id", None))
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("wrdl_"))
def wordle_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_", 3)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные")
            return

        action = parts[1]
        gid = parts[2]
        game = wordle_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        if call.from_user.id != game.get("owner"):
            bot.answer_callback_query(call.id, "Это не ваша игра", show_alert=True)
            return

        if action == "new":
            game = _wordle_new_game(call.from_user.id)
            wordle_games[gid] = game
            safe_edit_message(call, _wordle_render_text(game), reply_markup=_wordle_keyboard(gid, game))
            bot.answer_callback_query(call.id, "Новая игра")
            return

        if game.get("status") != "playing":
            bot.answer_callback_query(call.id, "Игра завершена")
            return

        if action == "l":
            if len(parts) < 4:
                bot.answer_callback_query(call.id, "Неверная буква")
                return
            ch = (parts[3] or "").lower()
            if len(ch) != 1:
                bot.answer_callback_query(call.id, "Неверная буква")
                return
            cur = game.get("current", "")
            if len(cur) < 5:
                game["current"] = cur + ch
            safe_edit_message(call, _wordle_render_text(game), reply_markup=_wordle_keyboard(gid, game))
            bot.answer_callback_query(call.id, game["current"].upper())
            return

        if action == "back":
            game["current"] = (game.get("current", "") or "")[:-1]
            safe_edit_message(call, _wordle_render_text(game), reply_markup=_wordle_keyboard(gid, game))
            bot.answer_callback_query(call.id)
            return

        if action == "submit":
            guess = (game.get("current", "") or "").lower()
            if len(guess) != 5:
                bot.answer_callback_query(call.id, "Введите 5 букв")
                return
            if guess not in WORDLE_WORDS:
                bot.answer_callback_query(call.id, "Слова нет в словаре")
                return
            marks = _wordle_eval_guess(guess, game["target"])
            game["attempts"].append({"guess": guess, "marks": marks})
            game["current"] = ""
            if guess == game["target"]:
                game["status"] = "won"
            elif len(game["attempts"]) >= 6:
                game["status"] = "lost"
            safe_edit_message(call, _wordle_render_text(game), reply_markup=_wordle_keyboard(gid, game))
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("wordle_callback", e)
        bot.answer_callback_query(call.id, "Ошибка Wordle")

def _bship_random_ships(size=5, ships_count=5):
    max_cells = max(1, size * size)
    ships_count = max(1, min(ships_count, max_cells))
    ships = set()
    while len(ships) < ships_count:
        ships.add((random.randint(0, size - 1), random.randint(0, size - 1)))
    return ships


def _bship_norm_cells(value):
    """Восстанавливает множество клеток из JSON (списки пар вместо кортежей)."""
    if not isinstance(value, (set, list, tuple)):
        return set()
    out = set()
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                out.add((int(item[0]), int(item[1])))
            except Exception:
                pass
    return out


def _bship_new_game(owner_id, owner_name, size=5, ships_count=5):
    size = max(3, min(8, int(size)))
    ships_count = max(1, min(int(ships_count), size * size))
    return {
        "p1": owner_id,
        "p2": None,
        "p1_name": owner_name,
        "p2_name": "",
        "size": size,
        "ships_count": ships_count,
        "ships": {owner_id: _bship_random_ships(size, ships_count)},
        "shots": {owner_id: set()},
        "turn": owner_id,
        "status": "waiting",
        "winner": None,
    }


def _bship_ensure_game_shape(game):
    if not isinstance(game, dict):
        return False
    if not isinstance(game.get("ships"), dict):
        game["ships"] = {}
    if not isinstance(game.get("shots"), dict):
        game["shots"] = {}
    game["size"] = max(3, min(8, int(game.get("size", 5) or 5)))
    game["ships_count"] = max(1, min(int(game.get("ships_count", 5) or 5), game["size"] * game["size"]))
    game["status"] = game.get("status") if game.get("status") in ("waiting", "playing", "ended") else "waiting"

    p1 = game.get("p1")
    p2 = game.get("p2")
    if p1 is None:
        return False

    if not game.get("p1_name"):
        game["p1_name"] = str(p1)
    if p2 is not None and not game.get("p2_name"):
        game["p2_name"] = str(p2)

    for player in (p1, p2):
        if player is None:
            continue
        game["ships"][player] = _bship_norm_cells(game["ships"].get(player))
        game["shots"][player] = _bship_norm_cells(game["shots"].get(player))
        if not game["ships"][player]:
            game["ships"][player] = _bship_random_ships(game["size"], game["ships_count"])

    if game.get("turn") not in (p1, p2):
        game["turn"] = p1

    if game["status"] == "playing" and p2 is None:
        game["status"] = "waiting"

    if game["status"] == "waiting":
        game["winner"] = None
    return True


def _bship_cell_view(is_own, has_ship, was_shot_by_self, was_shot_by_enemy):
    if is_own:
        if has_ship and was_shot_by_enemy:
            return "💥"
        if has_ship:
            return "🚢"
        if was_shot_by_enemy:
            return "•"
        return "▫️"
    if was_shot_by_self:
        return "💥" if has_ship else "•"
    return "❔"


def _bship_header(game, p2_placeholder):
    """Общая шапка: размер поля, имена и текущий статус партии."""
    p1_name = game.get("p1_name", "Игрок 1")
    p2_name = game.get("p2_name", "Игрок 2") if game.get("p2") else p2_placeholder
    text = f"🚢 Морской бой ({game['size']}x{game['size']})\nКораблей: {game['ships_count']} у каждого\n\n"
    text += f"{p1_name} vs {p2_name}\n"

    status = game.get("status")
    if status == "waiting":
        return text + "\nНажмите «Присоединиться», чтобы начать."
    if status == "ended":
        return text + f"\nПобедитель: {p1_name if game.get('winner') == game.get('p1') else p2_name}"
    return text + f"\nХод: {p1_name if game.get('turn') == game.get('p1') else p2_name}"


def _bship_public_text(game):
    _bship_ensure_game_shape(game)
    text = _bship_header(game, "Ожидание второго игрока")
    return text + "\n\nПоля скрыты. Играйте через личные сообщения с ботом."


def _bship_public_keyboard(gid, game):
    _bship_ensure_game_shape(game)
    kb = types.InlineKeyboardMarkup()
    if game.get("status") == "waiting":
        kb.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"bship_join_{gid}"))
    kb.add(types.InlineKeyboardButton("Открыть ЛС", callback_data=f"bship_dm_{gid}"))
    if game.get("status") == "ended":
        kb.add(types.InlineKeyboardButton("Новая партия", callback_data=f"bship_new_{gid}"))
    return kb


def _bship_render_text(game, viewer_id):
    _bship_ensure_game_shape(game)
    p1, p2 = game.get("p1"), game.get("p2")
    text = _bship_header(game, "Игрок 2") + "\n"

    if game.get("status") == "waiting":
        return text
    if viewer_id not in (p1, p2):
        return text + "\n(Вы не участник этой партии)"

    size = game["size"]
    enemy = p2 if viewer_id == p1 else p1
    my_ships = game["ships"].get(viewer_id, set())
    enemy_ships = game["ships"].get(enemy, set())
    my_shots = game["shots"].get(viewer_id, set())
    enemy_shots = game["shots"].get(enemy, set())

    def grid(is_own):
        rows = []
        for r in range(size):
            rows.append("".join(
                _bship_cell_view(is_own, (r, c) in (my_ships if is_own else enemy_ships),
                                 (r, c) in my_shots, (r, c) in enemy_shots)
                for c in range(size)
            ))
        return "\n".join(rows) + "\n"

    text += "\nЛегенда: 🚢 корабль, 💥 попадание, • мимо, ▫️ пусто, ❔ неизвестно\n"
    text += "\nВаше поле\n" + grid(True)
    text += "\nПоле соперника\n" + grid(False)
    return text


def _bship_keyboard(gid, game, viewer_id):
    _bship_ensure_game_shape(game)
    kb = types.InlineKeyboardMarkup()
    status = game.get("status")

    if status == "waiting":
        kb.add(types.InlineKeyboardButton("Присоединиться", callback_data=f"bship_join_{gid}"))
        return kb

    if status == "ended":
        kb.add(types.InlineKeyboardButton("Новая партия", callback_data=f"bship_new_{gid}"))
        return kb

    p1 = game.get("p1")
    p2 = game.get("p2")
    if viewer_id not in (p1, p2):
        return kb

    if viewer_id != game.get("turn"):
        kb.add(types.InlineKeyboardButton("Ход соперника", callback_data="none"))
        return kb

    size = game.get("size", 5)
    shots = game.get("shots", {}).get(viewer_id, set())
    for r in range(size):
        kb.row(*[
            types.InlineKeyboardButton("•", callback_data="none")
            if (r, c) in shots
            else types.InlineKeyboardButton("▫️", callback_data=f"bship_shot_{gid}_{r}_{c}")
            for c in range(size)
        ])
    return kb


def _bship_store_public_anchor(game, call):
    if getattr(call, "inline_message_id", None):
        game["public_inline_id"] = call.inline_message_id
    elif getattr(call, "message", None) and getattr(call.message.chat, "type", None) != "private":
        game["public_chat_id"] = call.message.chat.id
        game["public_message_id"] = call.message.message_id


def _bship_edit_public_view(gid, game, call=None):
    text = _bship_public_text(game)
    kb = _bship_public_keyboard(gid, game)
    try:
        inline_id = game.get("public_inline_id")
        if inline_id:
            bot.edit_message_text(text, inline_message_id=inline_id, reply_markup=kb)
            return
        chat_id = game.get("public_chat_id")
        message_id = game.get("public_message_id")
        if chat_id and message_id:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb)
            return
        if call is not None and (getattr(call, "inline_message_id", None) or (getattr(call, "message", None) and getattr(call.message.chat, "type", None) != "private")):
            safe_edit_message(call, text, reply_markup=kb)
    except Exception as e:
        if not _is_unchanged_message_error(e):
            log_exception("battleship_public_view", e)


def _bship_send_or_edit_private(gid, game, uid):
    if uid not in (game.get("p1"), game.get("p2")):
        return False
    return _send_or_edit_tracked(
        game.setdefault("pm", {}),
        uid,
        _bship_render_text(game, uid),
        _bship_keyboard(gid, game, uid),
        context="battleship_private_view",
    )


def _bship_sync_views(gid, game, call=None):
    _bship_edit_public_view(gid, game, call=call)
    return _sync_two_player_views(
        game,
        lambda uid: _bship_render_text(game, uid),
        lambda uid: _bship_keyboard(gid, game, uid),
        (game.get("p1"), game.get("p2")),
        "battleship_private_view",
    )


@bot.callback_query_handler(func=lambda c: c.data == "none")
def noop_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "find_cancel")
def find_cancel_callback(call):
    uid = call.from_user.id
    if find_queue.pop(uid, None) is not None:
        safe_edit_message(call, "🔎 Поиск отменен.")
        try:
            bot.answer_callback_query(call.id, "Поиск остановлен")
        except Exception:
            pass
        return
    try:
        bot.answer_callback_query(call.id, "Вы уже не в поиске")
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("find_vote_"))
def find_vote_callback(call):
    try:
        parts = call.data.split("_")
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Неверные данные")
            return
        match_id = parts[2]
        game_key = parts[3]
        match = find_matches.get(match_id)
        if not match or match.get("status") != "voting":
            bot.answer_callback_query(call.id, "Голосование уже завершено")
            return
        uid = call.from_user.id
        if uid not in match.get("players", []):
            bot.answer_callback_query(call.id, "Это не ваше голосование", show_alert=True)
            return
        if game_key not in match.get("options", []):
            bot.answer_callback_query(call.id, "Такой игры нет")
            return
        match.setdefault("votes", {})[uid] = game_key
        _find_refresh_vote_messages(match_id)
        bot.answer_callback_query(call.id, f"Ваш голос: {GAME_TITLES.get(game_key, game_key)}")
        if len(match.get("votes", {})) >= len(match.get("players", [])):
            _find_finalize_vote(match_id)
    except Exception as e:
        log_exception("find_vote", e)
        try:
            bot.answer_callback_query(call.id, "Ошибка голосования")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("pmttt_"))
def private_ttt_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные")
            return
        action = parts[1]
        gid = parts[2]
        game = pm_ttt_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return
        uid = call.from_user.id
        if uid not in game.get("players", []):
            bot.answer_callback_query(call.id, "Это не ваша партия", show_alert=True)
            return

        if action == "new":
            p1, p2 = game["players"]
            new_game = _pm_ttt_new_game(p1, p2, game["names"].get(p1, "Игрок 1"), game["names"].get(p2, "Игрок 2"))
            new_game["scores"] = dict(game.get("scores", {}))
            new_game["pm"] = game.get("pm", {})
            pm_ttt_games[gid] = new_game
            _pm_ttt_record_session(gid, new_game)
            _pm_ttt_sync_views(gid, new_game)
            bot.answer_callback_query(call.id, "Новая партия")
            return

        if action != "move" or len(parts) < 4:
            bot.answer_callback_query(call.id, "Неверный ход")
            return
        if game.get("status") != "playing":
            bot.answer_callback_query(call.id, "Партия завершена")
            return
        if uid != game.get("turn"):
            bot.answer_callback_query(call.id, "Сейчас не ваш ход")
            return

        cell = int(parts[3])
        if cell < 0 or cell > 8:
            bot.answer_callback_query(call.id, "Неверная клетка")
            return
        if game["board"][cell] != " ":
            bot.answer_callback_query(call.id, "Клетка занята")
            return

        p1, p2 = game["players"]
        symbol = TTT_X if uid == p1 else "⭕"
        game["board"][cell] = symbol
        if _ttt_wins(game["board"], symbol):
            game["status"] = "ended"
            game["winner"] = uid
            game["scores"][uid] = game["scores"].get(uid, 0) + 1
            _pm_ttt_record_results(game)
        elif " " not in game["board"]:
            game["status"] = "ended"
            game["winner"] = "draw"
            _pm_ttt_record_results(game)
        else:
            game["turn"] = p2 if uid == p1 else p1

        _pm_ttt_sync_views(gid, game)
        bot.answer_callback_query(call.id, "Ход принят")
    except Exception as e:
        log_exception("private_ttt", e)
        try:
            bot.answer_callback_query(call.id, "Ошибка игры")
        except Exception:
            pass


def _safe_answer_callback(call, text=None, show_alert=False, context="callback_ack"):
    try:
        bot.answer_callback_query(call.id, text, show_alert=show_alert)
    except Exception as e:
        msg = str(e)
        if "query is too old" not in msg and "query ID is invalid" not in msg:
            log_exception(context, e)


@bot.callback_query_handler(func=lambda c: c.data.startswith("bship_"))
def battleship_callback(call):
    _track_callback_game_play(call)

    def _safe_ack(text=None, show_alert=False):
        _safe_answer_callback(call, text, show_alert, context="battleship_ack")

    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            _safe_ack("Неверные данные")
            return

        action = parts[1]
        gid = parts[2]
        game = battleship_games.get(gid)
        if not game:
            _safe_ack("Игра не найдена")
            return
        if not _bship_ensure_game_shape(game):
            _safe_ack("Игра повреждена")
            return
        _bship_store_public_anchor(game, call)

        uid = call.from_user.id

        if action == "dm":
            if uid not in (game.get("p1"), game.get("p2")):
                _safe_ack("Вы не участник этой партии", show_alert=True)
                return
            if _bship_send_or_edit_private(gid, game, uid):
                _safe_ack("Отправил поле в ЛС")
            else:
                _safe_ack("Не могу написать в ЛС. Откройте чат с ботом и нажмите Start", show_alert=True)
            return

        if action == "join":
            if game.get("status") != "waiting":
                _safe_ack("Игра уже началась")
                return
            if uid == game.get("p1"):
                _safe_ack("Нужен второй игрок")
                return

            game["p2"] = uid
            game["p2_name"] = call.from_user.first_name or call.from_user.username or str(uid)
            size = game.get("size", 5)
            ships_count = game.get("ships_count", 5)
            game.setdefault("ships", {})[uid] = _bship_random_ships(size, ships_count)
            game.setdefault("shots", {})[uid] = set()
            game["status"] = "playing"
            game["turn"] = game.get("p1")

            ok1, ok2 = _bship_sync_views(gid, game, call=call)
            if not ok1 or not ok2:
                _safe_ack("Партия началась. Если нет поля в ЛС — откройте чат с ботом и нажмите Start", show_alert=True)
            else:
                _safe_ack("Партия началась. Поля отправлены в ЛС")
            return

        if action == "new":
            if uid not in (game.get("p1"), game.get("p2")):
                _safe_ack("\u042d\u0442\u043e \u043d\u0435 \u0432\u0430\u0448\u0430 \u043f\u0430\u0440\u0442\u0438\u044f")
                return

            size = game.get("size", 5)
            ships_count = game.get("ships_count", 5)
            p1 = game.get("p1")
            p2 = game.get("p2")

            if not p2:
                game.update(_bship_new_game(p1, game.get("p1_name", "\u0418\u0433\u0440\u043e\u043a 1"), size=size, ships_count=ships_count))
                game["turn"] = uid
                game["shots"] = {p1: set()}
            else:
                game["status"] = "playing"
                game["ships"] = {
                    p1: _bship_random_ships(size, ships_count),
                    p2: _bship_random_ships(size, ships_count),
                }
                game["shots"] = {p1: set(), p2: set()}
            game["turn"] = p1
            game["winner"] = None

            _bship_sync_views(gid, game, call=call)
            _safe_ack("Новая партия. Обновил поля в ЛС")
            return

        if action == "shot":
            if len(parts) < 5:
                _safe_ack("Неверный ход")
                return
            if game.get("status") != "playing":
                _safe_ack("Партия не начата")
                return
            if uid != game.get("turn"):
                _safe_ack("Сейчас не ваш ход")
                return
            if uid not in (game.get("p1"), game.get("p2")):
                _safe_ack("Вы не участник этой партии")
                return

            r = int(parts[3])
            c = int(parts[4])
            size = game.get("size", 5)
            if r < 0 or c < 0 or r >= size or c >= size:
                _safe_ack("Некорректная клетка")
                return

            enemy = game.get("p2") if uid == game.get("p1") else game.get("p1")
            if not enemy:
                _safe_ack("Ожидаем второго игрока")
                return

            my_shots = game.setdefault("shots", {}).setdefault(uid, set())
            if (r, c) in my_shots:
                _safe_ack("Вы уже стреляли сюда")
                return

            my_shots.add((r, c))
            enemy_ships = game.setdefault("ships", {}).setdefault(enemy, set())

            if (r, c) in enemy_ships:
                if enemy_ships.issubset(my_shots):
                    game["status"] = "ended"
                    game["winner"] = uid
                    _bship_sync_views(gid, game, call=call)
                    _safe_ack("Попадание! Вы победили")
                    return
                _safe_ack("Попадание! Ходите еще")
            else:
                game["turn"] = enemy
                _safe_ack("Мимо")

            _bship_sync_views(gid, game, call=call)
            return

        _safe_ack()
    except Exception as e:
        log_exception("battleship_callback", e)
        _safe_ack("\u041e\u0448\u0438\u0431\u043a\u0430 \u041c\u043e\u0440\u0441\u043a\u043e\u0433\u043e \u0431\u043e\u044f")


@bot.callback_query_handler(func=lambda c: c.data.startswith("chess_"))
def chess_callback(call):
    _track_callback_game_play(call)
    try:
        parts = call.data.split("_")
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Неверные данные")
            return

        action = parts[1]
        gid = parts[2]
        game = chess_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра не найдена")
            return

        uid = call.from_user.id

        if action == "join":
            if game.get("status") != "waiting":
                bot.answer_callback_query(call.id, "Игра уже началась")
                return
            if uid == game.get("p1"):
                bot.answer_callback_query(call.id, "Нужен второй игрок")
                return
            game["p2"] = uid
            game["p2_name"] = call.from_user.first_name or call.from_user.username or str(uid)
            game["status"] = "playing"
            _chess_refresh_views(gid, game, call=call)
            bot.answer_callback_query(call.id, "Партия началась")
            return

        if action == "new":
            if uid not in (game.get("p1"), game.get("p2")):
                bot.answer_callback_query(call.id, "Это не ваша партия")
                return
            if game.get("private_mode") and game.get("p1") and game.get("p2"):
                new_game = _private_chess_new_game(
                    game.get("p1"),
                    game.get("p2"),
                    game.get("p1_name") or str(game.get("p1")),
                    game.get("p2_name") or str(game.get("p2")),
                )
                new_game["pm"] = game.get("pm", {})
            else:
                new_game = _chess_new_game(uid, call.from_user.first_name or call.from_user.username or str(uid))
            chess_games[gid] = new_game
            _chess_refresh_views(gid, new_game, call=call)
            bot.answer_callback_query(call.id, "Новая партия")
            return

        if action == "reset":
            if game.get("status") != "playing":
                bot.answer_callback_query(call.id)
                return
            if uid not in (game.get("p1"), game.get("p2")):
                bot.answer_callback_query(call.id, "Это не ваша партия")
                return
            game["selected"] = None
            _chess_refresh_views(gid, game, call=call)
            bot.answer_callback_query(call.id, "Сброшено")
            return

        if action == "c":
            if len(parts) < 5:
                bot.answer_callback_query(call.id, "Неверный ход")
                return
            if game.get("status") != "playing":
                bot.answer_callback_query(call.id, "Партия не начата")
                return

            player_color = _chess_get_player_color(game, uid)
            if player_color is None:
                bot.answer_callback_query(call.id, "Вы не участник этой партии")
                return
            if player_color != game.get("turn"):
                bot.answer_callback_query(call.id, "Сейчас не ваш ход")
                return

            r = int(parts[3])
            c = int(parts[4])
            if not _chess_in_bounds(r, c):
                bot.answer_callback_query(call.id, "Некорректная клетка")
                return

            board = game["board"]
            selected = game.get("selected")

            if selected:
                sr, sc = selected
                legal = set(_chess_legal_moves(board, sr, sc))
                if (r, c) in legal:
                    _chess_apply_move(game, sr, sc, r, c)
                    _chess_refresh_views(gid, game, call=call)
                    bot.answer_callback_query(call.id, "Ход выполнен")
                    return
                target = board[r][c]
                if target and target[0] == player_color:
                    game["selected"] = (r, c)
                    _chess_refresh_views(gid, game, call=call)
                    bot.answer_callback_query(call.id, "Фигура выбрана")
                    return
                bot.answer_callback_query(call.id, "Сюда ходить нельзя")
                return

            piece = board[r][c]
            if not piece:
                bot.answer_callback_query(call.id, "Выберите свою фигуру")
                return
            if piece[0] != player_color:
                bot.answer_callback_query(call.id, "Это фигура соперника")
                return
            game["selected"] = (r, c)
            _chess_refresh_views(gid, game, call=call)
            bot.answer_callback_query(call.id, "Фигура выбрана")
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("chess_callback", e)
        bot.answer_callback_query(call.id, "Ошибка шахмат")

@bot.inline_handler(lambda q: q.query.lower() == "minesweeper")
def inline_minesweeper(query):
    if not _inline_guard(query):
        return
    board, mine_positions = generate_minesweeper_board()
    gid = short_id()
    minesweeper_games[gid] = {"board": board, "revealed": set(), "mine_positions": mine_positions}
    uid = query.from_user.id
    results = [types.InlineQueryResultArticle(
        id=f"minesweeper_{gid}",
        title=f"💣 {get_game_title(uid, 'minesweeper')}",
        description=get_game_description(uid, "minesweeper"),
        input_message_content=types.InputTextMessageContent(
            f"💣 {get_game_title(uid, 'minesweeper')}\n{render_minesweeper_board(board, set())}"),
        reply_markup=_minesweeper_build_markup(gid, board, set())
    )]
    bot.answer_inline_query(query.id, results, cache_time=1, is_personal=True)

@bot.callback_query_handler(func=lambda c: c.data.startswith("minesweeper_"))
def minesweeper_callback(call):
    _track_callback_game_play(call)
    try:
        _, gid, x, y = call.data.split("_")
        x, y = int(x), int(y)
        game = minesweeper_games.get(gid)
        if not game:
            bot.answer_callback_query(call.id, "Игра завершена!")
            return
        board = game["board"]; revealed = game["revealed"]; mine_positions = game["mine_positions"]
        if (x, y) in mine_positions:
            safe_edit_message(call, f"💥 Вы наткнулись на мину!\n\n{render_minesweeper_board(board, revealed.union(mine_positions))}")
            minesweeper_games.pop(gid, None)
            bot.answer_callback_query(call.id)
            return
        revealed.add((x, y))
        if len(revealed) == len(board)*len(board) - len(mine_positions):
            safe_edit_message(call, f"🎉 Вы выиграли!\n\n{render_minesweeper_board(board, revealed.union(mine_positions))}")
            minesweeper_games.pop(gid, None)
            bot.answer_callback_query(call.id)
            return
        markup = _minesweeper_build_markup(gid, board, revealed)
        safe_edit_message(call, f"💣 Сапёр\n{render_minesweeper_board(board, revealed)}", reply_markup=markup)
        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("mine", e)
        bot.answer_callback_query(call.id, "Ошибка сапёра")

@bot.callback_query_handler(func=lambda c: c.data.startswith("os_"))
def telos_callbacks(call):
    try:
        data = call.data
        uid = call.from_user.id
        st = _telos_get_state(uid)

        if data == "os_back":
            safe_edit_message(call, _telos_home_text(uid), reply_markup=telos_main_menu(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_boot":
            st["booted"] = True
            _telos_save_state(uid, st)
            safe_edit_message(call, _telos_home_text(uid), reply_markup=telos_main_menu(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if not st.get("booted", True):
            boot_kb = types.InlineKeyboardMarkup()
            boot_kb.add(types.InlineKeyboardButton("▶️ Запустить", callback_data="os_boot"))
            safe_edit_message(call, "⏻ *TELOS выключен*\nНажмите Запустить.", reply_markup=boot_kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_files":
            files = st.get("files", [])
            body = "\n".join([f"{i+1}. `{x.get('name', 'file.txt')}`" for i, x in enumerate(files[:10])]) if files else "(пусто)"
            safe_edit_message(call, "*Файлы*\n\n" + body, reply_markup=_telos_files_kb(st), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_files_new":
            telos_input_wait[uid] = {"action": "new_file"}
            bot.answer_callback_query(call.id)
            bot.send_message(uid, "Отправьте файл в формате: `имя.txt | содержимое`", parse_mode="Markdown")
            return

        if data == "os_files_clear":
            st["files"] = []
            _telos_save_state(uid, st)
            safe_edit_message(call, "*Файлы*\n\n(пусто)", reply_markup=_telos_files_kb(st), parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Файлы очищены")
            return

        if data.startswith("os_file_"):
            idx = int(data.split("_")[2])
            files = st.get("files", [])
            if idx < 0 or idx >= len(files):
                bot.answer_callback_query(call.id, "Файл не найден", show_alert=True)
                return
            fobj = files[idx]
            safe_edit_message(call, f"*{fobj.get('name', 'file.txt')}*\n\n{fobj.get('content', '(пусто)')[:1500]}", reply_markup=_telos_files_kb(st), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_notes":
            notes = st.get("notes", [])
            body = "\n".join([f"{i+1}. {str(x)[:80]}" for i, x in enumerate(notes[:10])]) if notes else "(нет заметок)"
            safe_edit_message(call, "*Заметки*\n\n" + body, reply_markup=_telos_notes_kb(st), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_notes_add":
            telos_input_wait[uid] = {"action": "new_note"}
            bot.answer_callback_query(call.id)
            bot.send_message(uid, "Введите текст заметки:")
            return

        if data == "os_notes_clear":
            st["notes"] = []
            _telos_save_state(uid, st)
            safe_edit_message(call, "*Заметки*\n\n(нет заметок)", reply_markup=_telos_notes_kb(st), parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Заметки очищены")
            return

        if data.startswith("os_note_"):
            idx = int(data.split("_")[2])
            notes = st.get("notes", [])
            if idx < 0 or idx >= len(notes):
                bot.answer_callback_query(call.id, "Заметка не найдена", show_alert=True)
                return
            safe_edit_message(call, f"*Заметка #{idx+1}*\n\n{str(notes[idx])[:1500]}", reply_markup=_telos_notes_kb(st), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_games":
            safe_edit_message(call, "*Игры внутри TELOS*\nВыберите игру:", reply_markup=_telos_games_kb(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_game_coin":
            bot.answer_callback_query(call.id, random.choice(["🪙 Орёл", "🪙 Решка"]), show_alert=True)
            return

        if data == "os_game_slot":
            symbols = ["🍒", "🍋", "🍉", "⭐", "💎", "7️⃣"]
            roll = " | ".join([random.choice(symbols) for _ in range(3)])
            picks = roll.split(" | ")
            if picks[0] == picks[1] == picks[2]:
                result = "🎉 Джекпот!"
            elif len(set(picks)) == 2:
                result = "✨ Почти!"
            else:
                result = "🚪 "
            bot.answer_callback_query(call.id, f"{roll}\n{result}", show_alert=True)
            return

        if data == "os_game_rps":
            safe_edit_message(call, "*Камень-ножницы-бумага*\nВыберите ход:", reply_markup=_telos_rps_kb(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data.startswith("os_game_rps_"):
            user_move = data.split("_")[3]
            bot_move = random.choice(["rock", "paper", "scissors"])
            icon = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
            if user_move == bot_move:
                res = "🤝 Ничья"
            elif (user_move == "rock" and bot_move == "scissors") or (user_move == "paper" and bot_move == "rock") or (user_move == "scissors" and bot_move == "paper"):
                res = "🎉 Победа"
            else:
                res = "😢 Поражение"
            bot.answer_callback_query(call.id, f"Вы: {icon[user_move]} | Бот: {icon[bot_move]}\n{res}", show_alert=True)
            return

        if data == "os_game_guess":
            st.setdefault("mini_games", {})["guess_target"] = random.randint(1, 10)
            _telos_save_state(uid, st)
            safe_edit_message(call, "*Угадай число*\nВыберите число от 1 до 10:", reply_markup=_telos_guess_kb(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_game_dice":
            value = random.randint(1, 6)
            faces = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
            bot.answer_callback_query(call.id, f"🚪 Выпало: {faces[value]} ({value})", show_alert=True)
            return

        if data.startswith("os_game_guess_pick_"):
            try:
                pick = int(data.split("_")[4])
            except Exception:
                bot.answer_callback_query(call.id, "Ошибка выбора", show_alert=True)
                return
            target = st.setdefault("mini_games", {}).get("guess_target")
            if not isinstance(target, int):
                bot.answer_callback_query(call.id, "Сначала запустите игру «Угадай число»", show_alert=True)
                return
            if pick == target:
                st["mini_games"]["guess_target"] = None
                _telos_save_state(uid, st)
                bot.answer_callback_query(call.id, f"🎉 Верно! Это {target}", show_alert=True)
            else:
                hint = "меньше" if pick > target else "больше"
                bot.answer_callback_query(call.id, f"❌ Неверно. Загаданное число {hint}.", show_alert=True)
            return

        if data == "os_terminal":
            hist = st.get("terminal_history", [])
            body = "\n".join(hist[-8:]) if hist else "(пусто)"
            safe_edit_message(call, "*Терминал*\n\n`" + body + "`", reply_markup=_telos_terminal_kb(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data.startswith("os_term_"):
            cmd = data.replace("os_term_", "")
            if cmd == "input":
                telos_input_wait[uid] = {"action": "term_input"}
                bot.answer_callback_query(call.id)
                bot.send_message(uid, "Введите команду терминала:")
                return
            out = _telos_run_command(st, cmd)
            st.setdefault("terminal_history", []).append(f"$ {cmd}")
            st["terminal_history"].append(out)
            st["terminal_history"] = st["terminal_history"][-20:]
            _telos_save_state(uid, st)
            safe_edit_message(call, "*Терминал*\n\n`" + "\n".join(st["terminal_history"][-8:]) + "`", reply_markup=_telos_terminal_kb(), parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        if data == "os_settings":
            s = st.get("settings", {})
            safe_edit_message(
                call,
                "*Настройки*\n\n"
                f"Имя ОС: *{s.get('os_name', 'TELOS')}*\n"
                f"Тема: *{s.get('theme', 'classic')}*",
                reply_markup=_telos_settings_kb(),
                parse_mode="Markdown",
            )
            bot.answer_callback_query(call.id)
            return

        if data == "os_set_name":
            telos_input_wait[uid] = {"action": "set_os_name"}
            bot.answer_callback_query(call.id)
            bot.send_message(uid, "Введите новое имя ОС (до 24 символов):")
            return

        if data == "os_set_theme":
            theme = st.get("settings", {}).get("theme", "classic")
            st["settings"]["theme"] = "neon" if theme == "classic" else "classic"
            _telos_save_state(uid, st)
            bot.send_message(uid, f"Тема: {st['settings']['theme']}")
            safe_edit_message(call, _telos_home_text(uid), reply_markup=telos_main_menu(), parse_mode="Markdown")
            return

        if data == "os_set_reset":
            st = _telos_default_state()
            _telos_save_state(uid, st)
            bot.send_message(uid, "TELOS сброшен")
            safe_edit_message(call, _telos_home_text(uid), reply_markup=telos_main_menu(), parse_mode="Markdown")
            return

        if data == "os_shutdown":
            st["booted"] = False
            _telos_save_state(uid, st)
            boot_kb = types.InlineKeyboardMarkup()
            boot_kb.add(types.InlineKeyboardButton("▶️ Запустить", callback_data="os_boot"))
            safe_edit_message(call, "⏻ *TELOS выключен*", reply_markup=boot_kb, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
            return

        bot.answer_callback_query(call.id)
    except Exception as e:
        log_exception("telos_callback", e)
        bot.answer_callback_query(call.id, "Ошибка")

@bot.callback_query_handler(func=lambda c: c.data == "easter_egg")
def easter_inline(call):
    bot.answer_callback_query(call.id, "Пасхалка!")
    Thread(target=play_inline_easter_egg, args=(call.inline_message_id,)).start()

@bot.callback_query_handler(func=lambda c: c.data.startswith("sysopen_"))
def sys_open(call):
    try:
        parts = call.data.split("_", 2)  # sysopen_{owner_uid}_{sid}
        owner_uid = int(parts[1])
        if owner_uid not in user_sys_settings:
            bot.answer_callback_query(call.id, "Данные не найдены.")
            return

        gui_text = user_sys_settings[owner_uid].get("gui", "Пусто")
        alert_text = gui_text[:190] if len(gui_text) > 190 else gui_text
        bot.answer_callback_query(call.id, alert_text or "Пусто", show_alert=True)
    except Exception as e:
        log_exception("sys_open", e)
        bot.answer_callback_query(call.id, "Ошибка")


@bot.callback_query_handler(func=lambda c: c.data == "coin_flip")
def coin_flip(call):
    _track_callback_game_play(call)
    res = random.choice(["🪙 Орёл","🪙 Решка"])
    bot.edit_message_text(f"Результат: {res}", inline_message_id=call.inline_message_id)
    bot.answer_callback_query(call.id, res)

@bot.callback_query_handler(func=lambda c: c.data == "slot_spin")
def slot_spin(call):
    _track_callback_game_play(call)
    symbols = ["🍒", "🍋", "🍉", "⭐", "💎", "7️⃣"]
    roll = [random.choice(symbols) for _ in range(3)]
    text = f"| {' | '.join(roll)} |"
    if roll.count("7️⃣") == 3:
        text += "\n💥💥💥"
    elif len(set(roll)) == 1:
        text += "\n✨✨✨"
    elif len(set(roll)) == 2:
        text += "\n✨✨"
    else:
        text += "\n🚪 "
    bot.edit_message_text(f"🎰 результат\n {text}\n", inline_message_id=call.inline_message_id,
                          reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🎰 Ещё раз", callback_data="slot_spin")))
    bot.answer_callback_query(call.id, "Крутим 🚪 ")

def play_inline_easter_egg(inline_id):
    frames = [
    "8=✊===D 🤨",
    "8==✊==D 🤨",
    "8===✊=D 🤨",
    "8====✊D 🤨",
    "8===✊=D 🤨",
    "8==✊==D 🤨",
    "8=✊===D 🤨",
    "8==✊==D 🥲",
    "8===✊=D 🥲",
    "8====✊D💦 🥲",
    "8===✊=D 🥲",
    "8====✊D💦 ☺️",
    "8===✊=D 😊",
    "8====✊D💦 😊",
    "8===✊=D 😊",
    "8====✊D💦 😊",
    "8=====D ☺️",
    "конец "
    ]
    for frame in frames:
        try:
            bot.edit_message_text(frame, inline_message_id=inline_id)
            time.sleep(0.5)
        except Exception:
            break

@bot.message_handler(func=lambda m: m.from_user.id in telos_input_wait)
def telos_save_input(message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    wait = telos_input_wait.pop(uid, None)
    if not wait:
        return

    st = _telos_get_state(uid)
    action = wait.get("action")

    if action == "new_note":
        if text:
            st.setdefault("notes", []).append(text[:500])
            st["notes"] = st["notes"][-100:]
            _telos_save_state(uid, st)
            bot.send_message(uid, "✅ Заметка добавлена")
        else:
            bot.send_message(uid, "❌ Пустая заметка не сохранена")
        return

    if action == "new_file":
        if "|" in text:
            name, content = text.split("|", 1)
            name = name.strip()[:40] or f"file_{len(st.get('files', []))+1}.txt"
            content = content.strip()[:1500]
        else:
            name = f"file_{len(st.get('files', []))+1}.txt"
            content = text[:1500]
        st.setdefault("files", []).append({"name": name, "content": content})
        st["files"] = st["files"][-100:]
        _telos_save_state(uid, st)
        bot.send_message(uid, f"✅ Файл `{name}` сохранён", parse_mode="Markdown")
        return

    if action == "set_os_name":
        st.setdefault("settings", {})["os_name"] = (text[:24] if text else "TELOS")
        _telos_save_state(uid, st)
        bot.send_message(uid, f"✅ Имя ОС: *{st['settings']['os_name']}*", parse_mode="Markdown")
        return

    if action == "term_input":
        out = _telos_run_command(st, text)
        st.setdefault("terminal_history", []).append(f"$ {text}")
        st["terminal_history"].append(out)
        st["terminal_history"] = st["terminal_history"][-20:]
        _telos_save_state(uid, st)
        bot.send_message(uid, f"`$ {text}`\n`{out}`", parse_mode="Markdown")
        return

    bot.send_message(uid, "❌ Неизвестное действие TELOS")

@bot.message_handler(func=lambda m: m.from_user.id in support_chat_wait, content_types=["text", "photo", "video"])
def support_user_message(message):
    uid = message.from_user.id
    mode = support_chat_wait.get(uid)
    text = (message.text or "").strip()
    caption = (message.caption or "").strip()
    if message.content_type == "text" and (not text or text.startswith("/")):
        return

    if mode == "moderator" and message.content_type != "text":
        bot.send_message(message.chat.id, "В режиме модератора отправьте текстовое сообщение.")
        return

    support_chat_wait.pop(uid, None)
    user_link = f"@{message.from_user.username}" if message.from_user.username else "без username"
    user_name = message.from_user.first_name or "Пользователь"
    mode_label = "Модератору" if mode == "moderator" else "Проблема"
    payload = (
        f"📩 <b>Новое обращение в поддержку</b>\n"
        f"Тип: <b>{mode_label}</b>\n"
        f"ID: <code>{uid}</code>\n"
        f"Имя: {html.escape(user_name)}\n"
        f"Username: {html.escape(user_link)}\n"
        f"Контент: <b>{message.content_type}</b>\n\n"
        f"<i>Ответить:</i> <code>/reply {uid} ваш_ответ</code>"
    )

    sent = 0
    for admin_id in SUPPORT_ADMIN_IDS:
        try:
            bot.send_message(admin_id, payload, parse_mode="HTML")
            bot.forward_message(admin_id, message.chat.id, message.message_id)
            if caption and message.content_type in ("photo", "video"):
                bot.send_message(admin_id, f"Подпись:\n{html.escape(caption)}", parse_mode="HTML")
            sent += 1
        except Exception:
            pass

    if sent:
        bot.send_message(message.chat.id, "✅ Сообщение отправлено в поддержку. Ожидайте ответ здесь в боте.")
    else:
        bot.send_message(message.chat.id, "❌ Сейчас нет доступных операторов поддержки.")

@bot.message_handler(func=lambda m: m.from_user.id in system_notify_wait)
def sys_save_value(message):
    uid = message.from_user.id
    field = system_notify_wait.pop(uid)

    if uid not in user_sys_settings:
        user_sys_settings[uid] = {"msg": "", "btn": "", "title": "", "gui": ""}

    if field.startswith("broadcast_"):
        if field == "broadcast_msg":
            BROADCAST_SETTINGS["msg"] = message.text
        elif field == "broadcast_btn":
            BROADCAST_SETTINGS["btn_text"] = message.text
        elif field == "broadcast_btn_link":
            BROADCAST_SETTINGS["btn_link"] = message.text
            BROADCAST_SETTINGS["btn_type"] = "link"
        elif field == "broadcast_btn_callback":
            BROADCAST_SETTINGS["btn_text"] = message.text
            BROADCAST_SETTINGS["btn_type"] = "callback"

        d = load_data()
        d["broadcast"] = BROADCAST_SETTINGS
        save_data(d)
        bot.send_message(uid, "✅ Broadcast сохранён!")
        return

    user_sys_settings[uid][field] = message.text
    bot.send_message(uid, "✅ Сохранено!")

@bot.message_handler(func=lambda m: m.from_user.id in admin_wait)
def admin_wait_input(message):
    uid = message.from_user.id
    wait = admin_wait.pop(uid, None)
    if not wait:
        return
    action = wait.get("action")
    if action == "close_room":
        code = (message.text or "").strip().upper()
        if not code:
            bot.send_message(uid, "Код пуст.")
            return
        ok = _room_close(code, reason="закрыто админом")
        if ok:
            bot.send_message(uid, f"✅ Пати {code} закрыто.")
        else:
            bot.send_message(uid, f"❌ Пати {code} не найдено.")
        return
    if action == "ban_user":
        parts = (message.text or "").split(maxsplit=1)
        if not parts or not parts[0].isdigit():
            bot.send_message(uid, "Нужен user_id.")
            return
        target_uid = int(parts[0])
        reason = parts[1] if len(parts) > 1 else "нарушение правил"
        _set_user_ban(uid, target_uid, True, reason)
        bot.send_message(uid, f"⛔ Пользователь {target_uid} забанен.")
        return
    if action == "unban_user":
        target = (message.text or "").strip()
        if not target.isdigit():
            bot.send_message(uid, "Нужен user_id.")
            return
        _set_user_ban(uid, int(target), False)
        bot.send_message(uid, f"✅ Пользователь {target} разбанен.")
        return

@bot.message_handler(func=lambda m: m.chat and m.chat.type in ("group", "supergroup"))
def room_track_messages(message):
    try:
        d, rooms = _rooms_get_data()
        code, room = _room_find_by_chat(rooms, message.chat.id)
        if not room:
            return
        chat_id = message.chat.id
        msg_list = room_messages.setdefault(chat_id, [])
        msg_list.append(message.message_id)
        if ROOM_MESSAGE_BUFFER and len(msg_list) > ROOM_MESSAGE_BUFFER:
            room_messages[chat_id] = msg_list[-ROOM_MESSAGE_BUFFER:]
        room_participants.setdefault(chat_id, set()).add(message.from_user.id)
        participants = room.get("participants", [])
        if isinstance(participants, list) and message.from_user.id not in participants:
            participants.append(message.from_user.id)
            room["participants"] = participants
        room["last_activity_at"] = time.time()
        rooms["active"][code] = room
        save_data(d)
    except Exception:
        pass

def _webapp_title(uid):
    return localized_text(uid, "🎮 Играть (Mini App)", "🎮 Play (Mini App)", "🎮 Грати (Mini App)")


def _webapp_keyboard_button(uid):
    """Кнопка запуска Mini App; Telegram принимает только https-адрес."""
    if not WEBAPP_URL.startswith("https://"):
        return None
    return types.KeyboardButton(_webapp_title(uid), web_app=types.WebAppInfo(WEBAPP_URL))


def _webapp_inline_button(uid, game_key=None):
    if not WEBAPP_URL.startswith("https://"):
        return None
    url = f"{WEBAPP_URL}/?game={game_key}" if game_key else WEBAPP_URL
    return types.InlineKeyboardButton(_webapp_title(uid), web_app=types.WebAppInfo(url))


def _setup_webapp_menu_button():
    if not WEBAPP_URL.startswith("https://"):
        LOGGER.warning("WEBAPP_URL=%s не https — кнопки Mini App отключены", WEBAPP_URL)
        return
    try:
        bot.set_chat_menu_button(
            menu_button=types.MenuButtonWebApp(
                type="web_app", text="🎮 Играть", web_app=types.WebAppInfo(WEBAPP_URL)
            )
        )
        LOGGER.info("Кнопка меню Mini App установлена: %s", WEBAPP_URL)
    except Exception as e:
        log_exception("setup_webapp_menu_button", e)


@bot.message_handler(commands=["app", "play"])
def webapp_command(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="webapp"):
        return
    button = _webapp_inline_button(uid)
    if not button:
        bot.send_message(
            message.chat.id,
            "⚠️ Mini App пока не настроен: нужен https-адрес в WEBAPP_URL.",
        )
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(button)
    bot.send_message(
        message.chat.id,
        localized_text(
            uid,
            f"🎮 Все игры в одном приложении: {len(WEBAPP_GAMES)} мини-игр, прогресс общий с ботом.",
            f"🎮 All games in one app: {len(WEBAPP_GAMES)} mini-games, progress shared with the bot.",
            f"🎮 Усі ігри в одному застосунку: {len(WEBAPP_GAMES)} міні-ігор, прогрес спільний із ботом.",
        ),
        reply_markup=kb,
    )


def _webapp_datasets():
    """Данные для Mini App берутся из тех же констант, что и чат-версии игр."""
    return {
        "games": WEBAPP_GAMES,
        "chat_only": WEBAPP_CHAT_ONLY_GAMES,
        "bot_username": INLINE_BOT_USERNAME,
        "wordle_words": WORDLE_WORDS,
        "hangman_words": HANGMAN_WORDS,
        "hangman_alphabet": HANGMAN_ALPHABET,
        "hangman_stages": HANGMAN_STAGES,
        "quiz_questions": QUIZ_QUESTIONS,
        "millionaire_questions": questions,
        "combo_choices": COMBO_CHOICES,
        "combo_beats": COMBO_BEATS,
        "poker_ranks": POKER_RANKS,
        "poker_suits": POKER_SUITS,
        "poker_hand_names": _POKER_HAND_NAMES,
    }


def _webapp_profile(user_id, tg_user=None):
    if tg_user and tg_user.get("first_name"):
        _remember_display_name(user_id, tg_user)
    rec, unlocked = _load_profile(user_id)
    completed_quests, total_quests = _quest_completion_summary(user_id)
    return {
        "user_id": user_id,
        "name": rec.get("display_name") or (tg_user or {}).get("first_name") or f"user_{user_id}",
        "coins": int(rec.get("coins", 0) or 0),
        "games_total": int(rec.get("games_total", 0) or 0),
        "streak": int(rec.get("streak_current", 0) or 0),
        "achievements": len(unlocked),
        "achievements_total": len(ACHIEVEMENTS),
        "quests_done": completed_quests,
        "quests_total": total_quests,
        "game_stats": _game_stats(rec),
        "avatar": rec.get("avatar_emoji", "🙂"),
    }


def _remember_display_name(user_id, tg_user):
    name = (tg_user.get("first_name") or tg_user.get("username") or str(user_id))[:64]
    d = load_data()
    rec = d.setdefault("users", {}).setdefault(str(user_id), {})
    if rec.get("display_name") != name:
        rec["display_name"] = name
        save_data(d)


def _webapp_apply_result(user_id, tg_user, payload):
    """Записывает партию из Mini App теми же функциями, что и чат-версия."""
    game_key = str(payload.get("game") or "").strip()
    if game_key not in WEBAPP_GAME_KEYS:
        raise ValueError("unknown game")

    display_name = (tg_user or {}).get("first_name") or str(user_id)
    session_id = str(payload.get("session") or short_id())[:64]
    _record_game_play_once(user_id, game_key, f"webapp_{session_id}", display_name=display_name)

    result = payload.get("result")
    if result in ("wins", "losses", "draws"):
        extra = {}
        for key in ("score", "rounds", "opponent"):
            if payload.get(key) is not None:
                extra[key] = str(payload[key])[:64]

        bet = payload.get("bet")
        if game_key in WEBAPP_BET_GAMES and bet is not None:
            try:
                bet = max(1, min(500, int(bet)))
            except (TypeError, ValueError):
                bet = None
            if bet:
                extra["bet"] = bet
                _webapp_settle_bet(user_id, bet, result)

        _record_game_result(user_id, game_key, result, extra=extra or None)

    update_user_streak(user_id, display_name)
    return _webapp_profile(user_id, tg_user)


def _webapp_settle_bet(user_id, bet, result):
    if result == "draws":
        return
    d = load_data()
    rec = _ensure_profile_fields(d.setdefault("users", {}).setdefault(str(user_id), {}))
    coins = int(rec.get("coins", 0) or 0)
    rec["coins"] = coins + bet if result == "wins" else max(0, coins - bet)
    save_data(d)


webapp_server = create_webapp(
    bot_token=TOKEN,
    dataset_provider=_webapp_datasets,
    profile_provider=_webapp_profile,
    result_handler=_webapp_apply_result,
    allow_unsigned=WEBAPP_ALLOW_UNSIGNED,
)


@webapp_server.route("/status")
def webapp_status():
    return "✅ если вы это видите — бот работает"


def run_webapp_server():
    try:
        run_webapp(webapp_server, "0.0.0.0", WEBAPP_PORT, WEBAPP_SSL_CERT, WEBAPP_SSL_KEY)
    except Exception as e:
        log_exception("webapp_server", e, notify_admin=True)


def keep_alive():
    import requests
    url = os.getenv("KEEP_ALIVE_URL", "").strip()
    if not url:
        return
    while not _shutdown_event.is_set():
        try:
            requests.get(url, timeout=10)
        except Exception:
            pass
        _shutdown_event.wait(300)


@bot.message_handler(commands=['menu'])
def send_welcome(message):
    if not _guard_user(message.from_user.id, chat_id=message.chat.id, action="menu", require_subscription=False):
        return
    _send_home_menu(message)


@bot.message_handler(commands=["help"])
def help_command(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, _render_help_text(uid), parse_mode="HTML")


@bot.message_handler(commands=["news", "whatsnew"])
def whats_new_command(message):
    uid = message.from_user.id
    bot.send_message(message.chat.id, _render_whats_new_text(uid))

@bot.message_handler(commands=['language', 'lang'])
def change_language_command(message):
    show_language_selection(message.chat.id)


@bot.message_handler(commands=['find'])
def find_player_command(message):
    uid = message.from_user.id
    if message.chat.type != "private":
        bot.send_message(message.chat.id, "Чтобы матчи запускались в ЛС, используйте /find в личном чате с ботом.")
        return

    active_match_id, active_match = _find_active_match_for_user(uid)
    if active_match:
        if active_match.get("status") == "voting":
            _find_refresh_vote_messages(active_match_id)
            bot.send_message(uid, "Вы уже нашли соперника. Голосование за игру уже идет.")
        else:
            bot.send_message(uid, "У вас уже есть активный матч. Доиграйте его или начните новый поиск позже.")
        return

    _find_prune_queue()
    find_queue[uid] = {
        "chat_id": message.chat.id,
        "name": _find_player_name(user=message.from_user),
        "started_at": time.time(),
    }
    bot.send_message(uid, _find_waiting_text(uid), reply_markup=_find_waiting_kb())
    _find_try_match_players()

def show_language_selection(chat_id):
    """Show language selection menu"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    for lang_code, lang_name in LANGUAGES.items():
        kb.add(types.InlineKeyboardButton(lang_name, callback_data=f"set_lang_{lang_code}"))
    
    welcome_msg = "🌍 Welcome! Choose your language:\n\n🇺🇦 Ласкаво просимо! Оберіть мову:\n\n🇷🇺 Добро пожаловать! Выберите язык:"
    bot.send_message(chat_id, welcome_msg, reply_markup=kb)

def show_main_menu(chat_id, uid):
    """Show main menu in user's language"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)

    play_button = _webapp_keyboard_button(uid)
    if play_button:
        kb.add(play_button)

    kb.add(
        types.KeyboardButton(t(uid, "games")),
        types.KeyboardButton(t(uid, "profile"))
    )

    kb.add(
        types.KeyboardButton(t(uid, "ai")),
        types.KeyboardButton(t(uid, "shop"))
    )
    
    kb.add(
        types.KeyboardButton(t(uid, "achievements")),
        types.KeyboardButton(t(uid, "leaderboard"))
    )
    
    kb.add(types.KeyboardButton(t(uid, "quests")))
    
    kb.add(types.KeyboardButton(t(uid, "create_room")))

    kb.add(
        types.KeyboardButton(t(uid, "support")),
        types.KeyboardButton(t(uid, "settings"))
    )

    inline_kb = types.InlineKeyboardMarkup(row_width=2)
    webapp_button = _webapp_inline_button(uid)
    if webapp_button:
        inline_kb.add(webapp_button)
    inline_kb.add(
        types.InlineKeyboardButton("📖 /help", callback_data="menu_help"),
        types.InlineKeyboardButton("🆕", callback_data="menu_whats_new")
    )
    if _last_game_instruction(uid):
        inline_kb.add(types.InlineKeyboardButton("▶️", callback_data="menu_continue"))

    bot.send_message(
        chat_id,
        f"{t(uid, 'main_menu')}\n\n{t(uid, 'choose_option')}\n\n{_render_main_menu_status(uid)}",
        reply_markup=kb
    )
    bot.send_message(
        chat_id,
        localized_text(uid, "Быстрые действия:", "Quick actions:", "Швидкі дії:"),
        reply_markup=inline_kb
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_lang_"))
def set_language_callback(call):
    if not _guard_user(call.from_user.id, chat_id=call.message.chat.id if call.message else call.from_user.id, call_id=call.id, action="language", require_subscription=False):
        return
    lang_code = call.data.replace("set_lang_", "")
    uid = call.from_user.id
    
    if lang_code in LANGUAGES:
        set_user_language(uid, lang_code)
        bot.answer_callback_query(call.id, t(uid, "language_changed"))
        
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        
        show_main_menu(call.message.chat.id, uid)
    else:
        bot.answer_callback_query(call.id, t(uid, "invalid_language"))

@bot.message_handler(func=lambda m: any(text_matches_key(m.text, key) for key in (
    "games", "profile", "ai", "shop", "achievements",
    "leaderboard", "support", "settings", "create_room", "quests"
)))
def handle_menu_buttons(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="menu_button"):
        return
    text = message.text
    
    if text_matches_key(text, "games"):
        show_games_menu(message.chat.id, uid)
    
    elif text_matches_key(text, "profile"):
        show_profile(message, uid)
    
    elif text_matches_key(text, "ai"):
        show_ai_menu(message, uid)
    
    elif text_matches_key(text, "shop"):
        show_shop(message, uid)
    
    elif text_matches_key(text, "achievements"):
        show_achievements(message, uid)
    
    elif text_matches_key(text, "leaderboard"):
        show_leaderboard(message, uid)
    
    elif text_matches_key(text, "quests"):
        quests_cmd(message)
    
    elif text_matches_key(text, "support"):
        show_support_menu(message, uid)
    
    elif text_matches_key(text, "settings"):
        show_settings_menu(message, uid)
    
    elif text_matches_key(text, "create_room"):
        create_room_handler(message, uid)

def show_games_menu(chat_id, uid):
    """Show games menu with categories"""
    kb = types.InlineKeyboardMarkup(row_width=1)

    kb.add(types.InlineKeyboardButton(t(uid, "games_solo"), callback_data="games_solo"))
    kb.add(types.InlineKeyboardButton(t(uid, "games_vs_bot"), callback_data="games_vs_bot"))
    kb.add(types.InlineKeyboardButton(t(uid, "games_multi"), callback_data="games_multi"))
    kb.add(types.InlineKeyboardButton(t(uid, "games_room"), callback_data="games_room"))
    kb.add(types.InlineKeyboardButton(t(uid, "back_to_menu"), callback_data="main_menu"))

    bot.send_message(
        chat_id,
        f"{t(uid, 'choose_game_category')}\n\n"
        f"• {t(uid, 'games_solo')} — {localized_text(uid, 'соло-прохождение и рекорды', 'solo runs and high scores', 'соло-проходження та рекорди')}\n"
        f"• {t(uid, 'games_vs_bot')} — {localized_text(uid, 'партии один на один', 'one-on-one matches', 'партії один на один')}\n"
        f"• {t(uid, 'games_multi')} — {localized_text(uid, 'игры с другими игроками', 'matches with other players', 'ігри з іншими гравцями')}\n"
        f"• {t(uid, 'games_room')} — {localized_text(uid, 'режим для комнаты и компании', 'room mode for groups', 'режим для кімнати та компанії')}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("games_"))
def games_category_handler(call):
    uid = call.from_user.id
    if not _guard_user(uid, chat_id=call.message.chat.id if call.message else uid, call_id=call.id, action="games"):
        return
    category = call.data.replace("games_", "")
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    
    if category == "solo":
        games = ["snake", "tetris", "flappy", "g2048", "slot", "wordle", "hangman", "minesweeper", "guess"]
    elif category == "vs_bot":
        games = ["rps", "ttt", "blackjack", "chess"]
    elif category == "multi":
        games = ["ttt", "chess", "bship", "wordgame", "combogame", "quizgame"]
    elif category == "room":
        games = ["room_rps", "room_duel", "room_bship", "room_quiz", "room_combo", "room_mafia"]
    else:
        bot.answer_callback_query(call.id, t(uid, "unknown_category"))
        return
    
    descriptions = []
    for game in games:
        title = get_game_title(uid, game)
        kb.add(types.InlineKeyboardButton(title, callback_data=f"play_{game}"))
        descriptions.append(f"• {title} — {get_game_description(uid, game)}")
    kb.add(types.InlineKeyboardButton(t(uid, "back_to_menu"), callback_data="main_menu"))
    
    try:
        bot.edit_message_text(
            "\n".join(descriptions),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=kb,
        )
    except Exception:
        pass
    
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data in {"menu_help", "menu_whats_new", "menu_continue", "contact_support", "show_faq", "toggle_notifications"})
def menu_action_callbacks(call):
    uid = call.from_user.id
    action = call.data
    if action == "menu_help":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, _render_help_text(uid), parse_mode="HTML")
        return
    if action == "menu_whats_new":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, _render_whats_new_text(uid))
        return
    if action == "menu_continue":
        text = _last_game_instruction(uid) or localized_text(
            uid,
            "Пока нечего продолжать: сначала сыграйте хотя бы одну игру.",
            "Nothing to continue yet: play at least one game first.",
            "Поки нічого продовжувати: спочатку зіграйте хоча б в одну гру.",
        )
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, text)
        return
    if action == "contact_support":
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, _support_text(), reply_markup=_support_menu_kb())
        return
    if action == "show_faq":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            localized_text(
                uid,
                "FAQ\n\n• Где запускать игры? Через меню или inline-режим.\n"
                "• Как вернуть прогресс? Используйте главное меню и профиль.\n"
                "• Куда писать о проблеме? В поддержку через /support.",
                "FAQ\n\n• Where do I launch games? Use the menu or inline mode.\n"
                "• How do I track progress? Open the main menu and profile.\n"
                "• Where do I report problems? Use /support.",
                "FAQ\n\n• Де запускати ігри? Через меню або inline-режим.\n"
                "• Як повернутись до прогресу? Відкрийте головне меню і профіль.\n"
                "• Куди писати про проблему? Через /support.",
            )
        )
        return
    if action == "toggle_notifications":
        d = load_data()
        rec = d.setdefault("users", {}).setdefault(str(uid), {})
        rec = _ensure_profile_fields(rec)
        rec["notifications_enabled"] = not rec.get("notifications_enabled", True)
        d["users"][str(uid)] = rec
        save_data(d)
        bot.answer_callback_query(
            call.id,
            localized_text(
                uid,
                "Уведомления включены" if rec["notifications_enabled"] else "Уведомления выключены",
                "Notifications enabled" if rec["notifications_enabled"] else "Notifications disabled",
                "Сповіщення увімкнено" if rec["notifications_enabled"] else "Сповіщення вимкнено",
            ),
        )
        return


@bot.callback_query_handler(func=lambda call: call.data.startswith("play_"))
def play_game_info_callback(call):
    uid = call.from_user.id
    if not _guard_user(uid, chat_id=call.message.chat.id if call.message else uid, call_id=call.id, action="play_info"):
        return
    game_key = call.data.replace("play_", "", 1)
    title = get_game_title(uid, game_key)
    description = get_game_description(uid, game_key)
    launch_hint = _last_game_instruction(uid) if game_key == load_data().get("users", {}).get(str(uid), {}).get("last_game") else None
    lines = [f"🎮 {title}", "", description]
    if launch_hint and title in launch_hint:
        lines.append("")
        lines.append(launch_hint)
    else:
        lines.append("")
        lines.append(
            localized_text(
                uid,
                f"Подсказка: используйте /help или inline-режим @{INLINE_BOT_USERNAME} для запуска игры.",
                f"Tip: use /help or inline mode @{INLINE_BOT_USERNAME} to launch the game.",
                f"Підказка: використовуйте /help або inline-режим @{INLINE_BOT_USERNAME} для запуску гри.",
            )
        )
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "\n".join(lines))


def _open_user_shop(chat_id, uid):
    bot.send_message(chat_id, _shop_render_text(uid), reply_markup=_shop_items_kb(uid))


def _render_leaderboard_text(uid):
    d = load_data()
    users = d.get("users", {})
    rows = []
    for uid_str, rec in users.items():
        if not isinstance(rec, dict):
            continue
        total = int(rec.get("games_total", 0) or 0)
        name = rec.get("display_name") or f"user_{uid_str}"
        rows.append((total, str(name)))
    rows.sort(key=lambda item: (-item[0], item[1].lower()))
    lines = [localized_text(uid, "🏆 Рейтинг по сыгранным играм", "🏆 Leaderboard by games played", "🏆 Рейтинг за кількістю ігор")]
    for index, (total, name) in enumerate(rows[:10], 1):
        lines.append(f"{index}. {name} — {total}")
    if len(lines) == 1:
        lines.append(localized_text(uid, "Пока нет данных.", "No data yet.", "Поки немає даних."))
    return "\n".join(lines)

def show_profile(message, uid):
    """Show user profile in their language"""
    profile_text = _render_profile_text(uid)
    bot.send_message(message.chat.id, profile_text, parse_mode="HTML")

def show_ai_menu(message, uid):
    bot.send_message(
        message.chat.id,
        localized_text(
            uid,
            "🤖 AI-ассистент\n\nИспользуйте inline-режим: "
            f"<code>@{INLINE_BOT_USERNAME} ваш вопрос</code>\n"
            "После отправки нажмите «Получить ответ».",
            "🤖 AI assistant\n\nUse inline mode: "
            f"<code>@{INLINE_BOT_USERNAME} your question</code>\n"
            "Then press “Get answer”.",
            "🤖 AI-асистент\n\nВикористовуйте inline-режим: "
            f"<code>@{INLINE_BOT_USERNAME} ваше запитання</code>\n"
            "Після відправки натисніть «Отримати відповідь».",
        ),
        parse_mode="HTML"
    )

def show_shop(message, uid):
    _open_user_shop(message.chat.id, uid)

def show_achievements(message, uid):
    """Show achievements - keep original functionality"""
    text = _render_achievements_text(uid)
    bot.send_message(message.chat.id, text)

def show_leaderboard(message, uid):
    bot.send_message(message.chat.id, _render_leaderboard_text(uid))

def show_support_menu(message, uid):
    """Show support menu in user's language"""
    kb = types.InlineKeyboardMarkup(row_width=1)

    kb.add(types.InlineKeyboardButton(t(uid, "contact_support"), callback_data="contact_support"))
    kb.add(types.InlineKeyboardButton(t(uid, "faq"), callback_data="show_faq"))
    kb.add(types.InlineKeyboardButton("📖 /help", callback_data="menu_help"))

    bot.send_message(message.chat.id, t(uid, "support_menu_title"), reply_markup=kb)

def show_settings_menu(message, uid):
    """Show settings menu in user's language"""
    kb = types.InlineKeyboardMarkup(row_width=1)
    
    lang = get_user_language(uid)
    lang_name = LANGUAGES.get(lang, "🇷🇺 Русский")

    kb.add(types.InlineKeyboardButton(f"{t(uid, 'language_label')}: {lang_name}", callback_data="change_language"))
    kb.add(types.InlineKeyboardButton(t(uid, "notifications"), callback_data="toggle_notifications"))
    kb.add(types.InlineKeyboardButton("🆕", callback_data="menu_whats_new"))

    bot.send_message(message.chat.id, t(uid, "settings_title"), reply_markup=kb)

def quests_cmd(message):
    uid = message.from_user.id
    if not _guard_user(uid, chat_id=message.chat.id, action="quests"):
        return
    reset_daily_quests(uid)
    reset_weekly_quests(uid)
    reset_seasonal_quests(uid)
    progress = get_user_quests_progress(uid)

    def progress_bar(value, target, width=8):
        target = max(int(target or 1), 1)
        value = max(int(value or 0), 0)
        filled = min(width, int(width * min(value, target) / target))
        return "▓" * filled + "░" * (width - filled)

    titles = {
        "daily": "🎯 Дневные квесты",
        "weekly": "📅 Недельные квесты",
        "seasonal": "💎 Сезонные квесты",
    }
    text = f"{t(uid, 'quests')}:\n\n"
    claimable = []
    for quest_type in ("daily", "weekly", "seasonal"):
        quests = QUESTS.get(quest_type, [])
        if not quests:
            continue
        text += f"{titles[quest_type]}\n"
        for q in quests:
            p = int(progress.get(quest_type, {}).get(q["id"], 0) or 0)
            target = int(q.get("target", 1) or 1)
            done = p >= target
            claimed = q["id"] in progress.get("claimed", [])
            status = "✅ получено" if claimed else ("✅ готово" if done else f"{p}/{target}")
            reward = ", ".join(f"{v} {k}" for k, v in (q.get("reward") or {}).items())
            text += f"• {q['title']}: {status}\n{progress_bar(p, target)}\n{q['description']}\nНаграда: {reward}\n\n"
            if done and not claimed:
                claimable.append((quest_type, q))
    kb = types.InlineKeyboardMarkup()
    if claimable:
        kb.add(types.InlineKeyboardButton("✅ Забрать все награды", callback_data="claim_all"))
    for quest_type, q in claimable:
        kb.add(types.InlineKeyboardButton(f"✅ Забрать {q['title']}", callback_data=f"claim_{quest_type}_{q['id']}"))
    kb.add(types.InlineKeyboardButton(t(uid, "back_to_menu"), callback_data="main_menu"))
    bot.send_message(message.chat.id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("claim_"))
def claim_quest(call):
    if not _guard_user(call.from_user.id, chat_id=call.message.chat.id if call.message else call.from_user.id, call_id=call.id, action="quest_claim"):
        return
    if call.data == "claim_all":
        uid = call.from_user.id
        reset_daily_quests(uid)
        reset_weekly_quests(uid)
        reset_seasonal_quests(uid)
        progress = get_user_quests_progress(uid)
        claimed = 0
        for quest_type in ("daily", "weekly", "seasonal"):
            for q in QUESTS.get(quest_type, []):
                p = int(progress.get(quest_type, {}).get(q["id"], 0) or 0)
                if p >= int(q.get("target", 1) or 1) and q["id"] not in progress.get("claimed", []):
                    if claim_quest_reward(uid, quest_type, q["id"]):
                        claimed += 1
        bot.answer_callback_query(call.id, f"✅ Получено наград: {claimed}")
        try:
            safe_edit_message(call, _render_main_menu_status(uid))
        except Exception:
            pass
        return
    parts = call.data.split("_", 2)
    if len(parts) < 3:
        return
    quest_type, quest_id = parts[1], parts[2]
    user_id = call.from_user.id
    if claim_quest_reward(user_id, quest_type, quest_id):
        bot.answer_callback_query(call.id, "Награда получена!")
    else:
        bot.answer_callback_query(call.id, "Квест не выполнен")

@bot.callback_query_handler(func=lambda call: call.data == "change_language")
def change_language_callback(call):
    show_language_selection(call.message.chat.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

@bot.callback_query_handler(func=lambda call: call.data == "main_menu")
def back_to_main_menu(call):
    show_main_menu(call.message.chat.id, call.from_user.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

def create_room_handler(message, uid):
    bot.send_message(
        message.chat.id,
        localized_text(
            uid,
            "🏠 Комнаты\n\nСоздание и подключение:\n• /party\n• /party_join <КОД>\n• /party_status",
            "🏠 Rooms\n\nCreate and join with:\n• /party\n• /party_join <CODE>\n• /party_status",
            "🏠 Кімнати\n\nСтворення та підключення:\n• /party\n• /party_join <КОД>\n• /party_status",
        )
    )


_shutdown_event = threading.Event()


def _graceful_shutdown(signum, _frame):
    """Stop polling and flush a final backup on SIGTERM/SIGINT.

    Live in-memory game state is best-effort, but persistent user/room data is
    checkpointed and backed up so a deploy or host restart can't lose the last
    backup window.
    """
    if _shutdown_event.is_set():
        return
    _shutdown_event.set()
    LOGGER.info("Получен сигнал %s — корректное завершение", signum)
    try:
        bot.stop_polling()
    except Exception:
        LOGGER.exception("stop_polling failed during shutdown")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    start_premium_watcher(bot)
    Thread(target=run_webapp_server, daemon=True).start()
    Thread(target=keep_alive, daemon=True).start()
    Thread(target=_rooms_watchdog, daemon=True).start()
    _setup_webapp_menu_button()
    if _DB_RECOVERY_NOTE:
        _send_admin_alert(f"⚠️ <b>База данных восстановлена при старте</b>\n{html.escape(_DB_RECOVERY_NOTE)}")
    LOGGER.info("Бот запущен, Mini App слушает порт %s (публичный адрес %s)", WEBAPP_PORT, WEBAPP_URL)
    while not _shutdown_event.is_set():
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=30)
        except Exception as e:
            if _shutdown_event.is_set():
                break
            log_exception("infinity_polling", e, notify_admin=True)
            time.sleep(5)

    LOGGER.info("Финальный бэкап перед остановкой")
    try:
        backup_json_files()
    except Exception:
        LOGGER.exception("не удалось сделать финальный бэкап")
    try:
        checkpoint_database(DB_FILE)
    except Exception:
        LOGGER.exception("не удалось свести WAL при остановке")
    if not database_is_healthy(DB_FILE):
        LOGGER.error("После остановки БД не проходит проверку целостности — будет восстановлена при следующем запуске")
    LOGGER.info("Бот остановлен корректно")
