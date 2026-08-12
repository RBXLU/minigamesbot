import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from .migrations import MigrationManager


DEFAULT_DB_PATH = "bot_data.sqlite3"
STATE_TABLE_KEYS = {
    "premium",
    "ai_cache",
    "stats",
    "global_game_stats",
    "rooms",
    "broadcast",
}

LOGGER = logging.getLogger("telegram_games_bot")

_DB_LOCK = threading.RLock()

# Пути БД, для которых миграции в этом процессе уже проверены: не гоняем их на каждом вызове
_SCHEMA_READY = set()

BUSY_RETRIES = 5
BUSY_BACKOFF = 0.15


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    # WAL даёт чтение параллельно с записью и переживает падение процесса;
    # synchronous=FULL добавляет fsync на коммит — защита и от потери питания.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA wal_autocheckpoint=512")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _is_busy_error(exc):
    text = str(exc).lower()
    return "locked" in text or "busy" in text


def _with_retries(operation, what):
    """Повторяет операцию, если база временно занята другим потоком."""
    last = None
    for attempt in range(BUSY_RETRIES):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc):
                raise
            last = exc
            time.sleep(BUSY_BACKOFF * (attempt + 1))
    LOGGER.error("%s: база занята после %s попыток", what, BUSY_RETRIES)
    raise last


def database_is_healthy(db_path):
    """Быстрая проверка целостности файла БД."""
    if not Path(db_path).exists():
        return True
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def _latest_backup(backup_dir, stem):
    candidates = sorted(
        Path(backup_dir).glob(f"{stem}_*.sqlite3"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return next((p for p in candidates if database_is_healthy(p)), None)


def recover_if_corrupted(db_path=DEFAULT_DB_PATH, backup_dir="backups"):
    """Если файл БД повреждён — отводит его в сторону и поднимает свежий бэкап.

    Возвращает описание того, что произошло, или None если всё в порядке.
    """
    source = Path(db_path)
    if database_is_healthy(source):
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    broken = source.with_name(f"{source.stem}_broken_{stamp}{source.suffix}")
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(source) + suffix)
        if candidate.exists():
            candidate.rename(str(broken) + suffix)

    backup = _latest_backup(backup_dir, source.stem)
    if backup is None:
        LOGGER.error("БД повреждена (%s), рабочих бэкапов нет — стартуем с чистой базы", broken.name)
        return f"БД повреждена, перенесена в {broken.name}; бэкапов нет, создана новая база"

    shutil.copy2(backup, source)
    LOGGER.error("БД повреждена (%s), восстановлена из бэкапа %s", broken.name, backup.name)
    return f"БД повреждена, перенесена в {broken.name}; восстановлена из {backup.name}"


def initialize_storage(db_path=DEFAULT_DB_PATH, legacy_json_path="bot_data.json", backup_dir="backups"):
    """Готовит БД к работе: чинит повреждения, применяет миграции, переносит старый JSON."""
    recovery = recover_if_corrupted(db_path, backup_dir)
    with _DB_LOCK:
        conn = _connect(db_path)
        try:
            MigrationManager(db_path).migrate(conn)
            _migrate_from_json_if_needed(conn, legacy_json_path)
            conn.commit()
        finally:
            conn.close()
    return recovery


def checkpoint(db_path=DEFAULT_DB_PATH):
    """Merge the WAL sidecar back into the main database file.

    In WAL mode the ``-wal`` file accumulates committed pages and is only
    folded into the main file at a checkpoint. Forcing a TRUNCATE checkpoint
    keeps that file bounded under sustained writes and guarantees that a plain
    copy of the ``.sqlite3`` file (e.g. on shutdown) is complete.
    """
    source = Path(db_path)
    if not source.exists():
        return
    with _DB_LOCK:
        conn = _connect(db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()


def backup_database(db_path=DEFAULT_DB_PATH, backup_dir="backups"):
    """Делает согласованную копию БД и проверяет, что она читается."""
    source = Path(db_path)
    if not source.exists():
        return None

    target_dir = Path(backup_dir)
    target_dir.mkdir(exist_ok=True)
    backup_path = target_dir / f"{source.stem}_{datetime.now():%Y%m%d_%H%M%S}{source.suffix}"

    with _DB_LOCK:
        src = _connect(str(source))
        try:
            dst = sqlite3.connect(str(backup_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

    # Битую копию лучше удалить сразу, чем однажды восстановиться из неё
    if not database_is_healthy(backup_path):
        LOGGER.error("Бэкап %s не прошёл проверку целостности и удалён", backup_path.name)
        backup_path.unlink(missing_ok=True)
        return None

    return str(backup_path)


def _meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key, value):
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES(?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def _ensure_schema(conn, db_path=DEFAULT_DB_PATH):
    """Ensure database schema is up to date by running migrations.

    Migrations are idempotent but still issue several queries + commits, so we
    only run them once per (process, db_path). ``initialize_storage`` performs
    the authoritative run at startup; later load/save calls hit the fast path.
    """
    if db_path in _SCHEMA_READY:
        return
    migration_manager = MigrationManager(db_path)
    migration_manager.migrate(conn)
    _SCHEMA_READY.add(db_path)


def _migrate_from_json_if_needed(conn, legacy_json_path):
    already_migrated = _meta_get(conn, "json_migrated", "")
    has_users = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
    if already_migrated == "1" or has_users:
        return

    if not os.path.exists(legacy_json_path):
        _meta_set(conn, "json_migrated", "1")
        return

    with open(legacy_json_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        payload = {}

    now = _utcnow()
    users = payload.get("users", {})
    for user_id, user_data in users.items():
        if not str(user_id).isdigit():
            continue
        conn.execute(
            "INSERT OR REPLACE INTO users(user_id, data_json, updated_at) VALUES (?, ?, ?)",
            (int(user_id), json.dumps(user_data, ensure_ascii=False), now),
        )
        _upsert_user_profile(conn, int(user_id), user_data, now)
        _migrate_user_side_tables(conn, int(user_id), user_data)

    for key, value in payload.items():
        if key == "users":
            continue
        if key == "rooms" and isinstance(value, dict):
            for code, room in (value.get("active", {}) or {}).items():
                if not isinstance(room, dict):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO rooms(code, chat_id, game_key, room_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        str(code),
                        room.get("chat_id"),
                        room.get("game_key"),
                        json.dumps(room, ensure_ascii=False),
                        now,
                    ),
                )
        if key in STATE_TABLE_KEYS:
            conn.execute(
                "INSERT OR REPLACE INTO state(key, value_json, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now),
            )

    _meta_set(conn, "json_migrated", "1")
    _meta_set(conn, "legacy_json_path", legacy_json_path)


def _migrate_user_side_tables(conn, user_id, user_data):
    history = user_data.get("match_history", [])
    if isinstance(history, list):
        for item in history:
            if not isinstance(item, dict):
                continue
            conn.execute(
                """
                INSERT INTO games_history(user_id, game_key, result, played_at, session_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    str(item.get("game") or ""),
                    str(item.get("result") or ""),
                    str(item.get("at") or ""),
                    str(item.get("session") or ""),
                ),
            )

    progress = user_data.get("quests_progress", {})
    if isinstance(progress, dict):
        claimed = set(progress.get("claimed", []) if isinstance(progress.get("claimed", []), list) else [])
        for quest_type in ("daily", "weekly"):
            rows = progress.get(quest_type, {})
            if not isinstance(rows, dict):
                continue
            for quest_id, value in rows.items():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO quests_progress(user_id, quest_type, quest_id, progress, claimed, season_id)
                    VALUES (?, ?, ?, ?, ?, '')
                    """,
                    (user_id, quest_type, str(quest_id), int(value or 0), 1 if quest_id in claimed else 0),
                )


def _upsert_user_profile(conn, user_id, user_data, now):
    if not isinstance(user_data, dict):
        user_data = {}

    conn.execute(
        """
        INSERT INTO user_profiles(
            user_id, display_name, language, coins, xp, premium_until,
            is_banned, ban_reason, notifications_enabled, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name = excluded.display_name,
            language = excluded.language,
            coins = excluded.coins,
            xp = excluded.xp,
            premium_until = excluded.premium_until,
            is_banned = excluded.is_banned,
            ban_reason = excluded.ban_reason,
            notifications_enabled = excluded.notifications_enabled,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            user_data.get("display_name"),
            str(user_data.get("lang") or user_data.get("language") or "ru"),
            int(user_data.get("coins", 0) or 0),
            int(user_data.get("xp", 0) or 0),
            int(user_data.get("premium_until", 0) or 0),
            1 if user_data.get("is_banned") else 0,
            user_data.get("ban_reason"),
            1 if user_data.get("notifications_enabled", True) else 0,
            now,
        ),
    )

    conn.execute("DELETE FROM user_inventory WHERE user_id = ?", (user_id,))
    inventory = user_data.get("inventory", [])
    if isinstance(inventory, list):
        for item_id in inventory:
            conn.execute(
                """
                INSERT OR IGNORE INTO user_inventory(user_id, item_id, purchased_at)
                VALUES (?, ?, ?)
                """,
                (user_id, str(item_id), now),
            )

    conn.execute(
        """
        INSERT INTO active_cosmetics(
            user_id, avatar_item_id, frame_item_id, theme_item_id, victory_item_id, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            avatar_item_id = excluded.avatar_item_id,
            frame_item_id = excluded.frame_item_id,
            theme_item_id = excluded.theme_item_id,
            victory_item_id = excluded.victory_item_id,
            updated_at = excluded.updated_at
        """,
        (
            user_id,
            user_data.get("avatar_item_id"),
            user_data.get("frame_item_id"),
            user_data.get("theme_item_id"),
            user_data.get("victory_item_id"),
            now,
        ),
    )


def load_state(db_path=DEFAULT_DB_PATH):
    return _with_retries(lambda: _load_state_once(db_path), "load_state")


def _load_state_once(db_path):
    with _DB_LOCK:
        conn = _connect(db_path)
        try:
            _ensure_schema(conn, db_path)
            state = {"users": {}}
            for row in conn.execute("SELECT user_id, data_json FROM users"):
                try:
                    state["users"][str(row["user_id"])] = json.loads(row["data_json"])
                except Exception:
                    state["users"][str(row["user_id"])] = {}

            for row in conn.execute("SELECT key, value_json FROM state"):
                try:
                    state[row["key"]] = json.loads(row["value_json"])
                except Exception:
                    state[row["key"]] = {}

            state.setdefault("global_game_stats", {})
            state.setdefault("premium", {})
            state.setdefault("rooms", {"pool": [], "active": {}, "free_title": "Свободно"})
            return state
        finally:
            conn.close()


def save_state(data, db_path=DEFAULT_DB_PATH):
    return _with_retries(lambda: _save_state_once(data, db_path), "save_state")


def _save_state_once(data, db_path):
    payload = data if isinstance(data, dict) else {}
    users = payload.get("users", {})
    now = _utcnow()

    with _DB_LOCK:
        conn = _connect(db_path)
        try:
            _ensure_schema(conn, db_path)

            # Вызывающий код передаёт всё состояние целиком, хотя меняется обычно
            # один пользователь: сравниваем с сохранённым и пишем только отличия.
            existing_users = {
                row["user_id"]: row["data_json"]
                for row in conn.execute("SELECT user_id, data_json FROM users")
            }
            existing_state = {
                row["key"]: row["value_json"]
                for row in conn.execute("SELECT key, value_json FROM state")
            }

            # IMMEDIATE берёт блокировку записи сразу — иначе два потока могут
            # начать транзакции и один упадёт при апгрейде до записи.
            conn.execute("BEGIN IMMEDIATE")

            for user_id, user_data in users.items():
                if not str(user_id).isdigit():
                    continue
                numeric_user_id = int(user_id)
                data_json = json.dumps(user_data, ensure_ascii=False)
                if existing_users.get(numeric_user_id) == data_json:
                    # Unchanged user: skip the main row, profile and side tables.
                    continue
                conn.execute(
                    """
                    INSERT INTO users(user_id, data_json, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        data_json = excluded.data_json,
                        updated_at = excluded.updated_at
                    """,
                    (numeric_user_id, data_json, now),
                )
                _upsert_user_profile(conn, numeric_user_id, user_data, now)

                if isinstance(user_data, dict):
                    conn.execute("DELETE FROM games_history WHERE user_id = ?", (numeric_user_id,))
                    conn.execute("DELETE FROM quests_progress WHERE user_id = ?", (numeric_user_id,))
                    _migrate_user_side_tables(conn, numeric_user_id, user_data)

            for key in STATE_TABLE_KEYS:
                value = payload.get(key)
                if value is None:
                    continue
                value_json = json.dumps(value, ensure_ascii=False)
                if existing_state.get(key) == value_json:
                    continue
                conn.execute(
                    """
                    INSERT INTO state(key, value_json, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                    """,
                    (key, value_json, now),
                )

            # Rooms are a small replace-all set; only rewrite when they differ.
            rooms = payload.get("rooms", {})
            new_rooms = {
                str(code): room
                for code, room in (rooms.get("active", {}) or {}).items()
                if isinstance(room, dict)
            }
            existing_rooms = {
                row["code"]: row["room_json"]
                for row in conn.execute("SELECT code, room_json FROM rooms")
            }
            new_rooms_json = {
                code: json.dumps(room, ensure_ascii=False)
                for code, room in new_rooms.items()
            }
            if new_rooms_json != existing_rooms:
                conn.execute("DELETE FROM rooms")
                for code, room in new_rooms.items():
                    conn.execute(
                        "INSERT INTO rooms(code, chat_id, game_key, room_json, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            code,
                            room.get("chat_id"),
                            room.get("game_key"),
                            new_rooms_json[code],
                            now,
                        ),
                    )

            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def export_state_to_json(json_path, db_path=DEFAULT_DB_PATH):
    """Пишет выгрузку через временный файл, чтобы падение не оставило обрезанный JSON."""
    state = load_state(db_path=db_path)
    target = Path(json_path)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


def get_user_record(user_id, db_path=DEFAULT_DB_PATH):
    state = load_state(db_path=db_path)
    return state.setdefault("users", {}).setdefault(str(user_id), {})


def save_user_record(user_id, user_data, db_path=DEFAULT_DB_PATH):
    state = load_state(db_path=db_path)
    state.setdefault("users", {})[str(user_id)] = user_data if isinstance(user_data, dict) else {}
    save_state(state, db_path=db_path)


def log_admin_action(admin_id, action, target_user_id=None, details=None, db_path=DEFAULT_DB_PATH):
    now = _utcnow()
    with _DB_LOCK:
        conn = _connect(db_path)
        try:
            _ensure_schema(conn, db_path)
            conn.execute(
                """
                INSERT INTO admin_actions(admin_id, action, target_user_id, details_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(admin_id),
                    str(action),
                    int(target_user_id) if target_user_id is not None else None,
                    json.dumps(details or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
