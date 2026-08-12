"""
Database migration system for version control of schema changes.
Supports incremental updates and rollback tracking.
"""

import sqlite3
from datetime import datetime


class Migration:
    """Base class for database migrations."""
    
    version: int = 0
    description: str = "Migration"
    
    def up(self, conn: sqlite3.Connection) -> None:
        """Apply the migration."""
        raise NotImplementedError("Subclasses must implement up()")
    
    def down(self, conn: sqlite3.Connection) -> None:
        """Rollback the migration."""
        raise NotImplementedError("Subclasses must implement down()")


class Migration_001_InitialSchema(Migration):
    """Initial database schema creation."""
    
    version = 1
    description = "Create initial schema with all tables"
    
    def up(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            data_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            result TEXT,
            played_at TEXT,
            session_id TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_games_history_user_id
        ON games_history(user_id);

        CREATE TABLE IF NOT EXISTS ratings (
            user_id INTEGER NOT NULL,
            game_key TEXT NOT NULL,
            mmr INTEGER NOT NULL DEFAULT 1000,
            PRIMARY KEY (user_id, game_key)
        );

        CREATE TABLE IF NOT EXISTS quests_progress (
            user_id INTEGER NOT NULL,
            quest_type TEXT NOT NULL,
            quest_id TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            claimed INTEGER NOT NULL DEFAULT 0,
            season_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, quest_type, quest_id, season_id)
        );

        CREATE TABLE IF NOT EXISTS friends (
            user_id INTEGER NOT NULL,
            friend_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (user_id, friend_id)
        );

        CREATE TABLE IF NOT EXISTS friend_requests (
            request_id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL,
            to_user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            responded_at TEXT
        );

        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            chat_id INTEGER,
            game_key TEXT,
            room_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
    
    def down(self, conn: sqlite3.Connection) -> None:
        """Rollback by dropping all tables."""
        conn.executescript("""
        DROP TABLE IF EXISTS rooms;
        DROP TABLE IF EXISTS friend_requests;
        DROP TABLE IF EXISTS friends;
        DROP TABLE IF EXISTS quests_progress;
        DROP TABLE IF EXISTS ratings;
        DROP INDEX IF EXISTS idx_games_history_user_id;
        DROP TABLE IF EXISTS games_history;
        DROP TABLE IF EXISTS state;
        DROP TABLE IF EXISTS users;
        DROP TABLE IF EXISTS meta;
        """)


class Migration_002_StructuredRuntimeTables(Migration):
    """Structured tables for frequently updated bot state."""

    version = 2
    description = "Add structured user, quest, room, shop, and admin runtime tables"

    def up(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            display_name TEXT,
            language TEXT NOT NULL DEFAULT 'ru',
            coins INTEGER NOT NULL DEFAULT 0,
            xp INTEGER NOT NULL DEFAULT 0,
            premium_until INTEGER NOT NULL DEFAULT 0,
            is_banned INTEGER NOT NULL DEFAULT 0,
            ban_reason TEXT,
            notifications_enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_inventory (
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            purchased_at TEXT NOT NULL,
            PRIMARY KEY (user_id, item_id)
        );

        CREATE TABLE IF NOT EXISTS active_cosmetics (
            user_id INTEGER PRIMARY KEY,
            avatar_item_id TEXT,
            frame_item_id TEXT,
            theme_item_id TEXT,
            victory_item_id TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shop_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            price INTEGER NOT NULL,
            purchased_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_user_id INTEGER,
            details_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_user_profiles_banned
        ON user_profiles(is_banned);

        CREATE INDEX IF NOT EXISTS idx_admin_actions_admin_id
        ON admin_actions(admin_id);
        """)

    def down(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
        DROP INDEX IF EXISTS idx_admin_actions_admin_id;
        DROP INDEX IF EXISTS idx_user_profiles_banned;
        DROP TABLE IF EXISTS admin_actions;
        DROP TABLE IF EXISTS shop_purchases;
        DROP TABLE IF EXISTS active_cosmetics;
        DROP TABLE IF EXISTS user_inventory;
        DROP TABLE IF EXISTS user_profiles;
        """)


# List of all migrations in order
MIGRATIONS = [
    Migration_001_InitialSchema,
    Migration_002_StructuredRuntimeTables,
]


class MigrationManager:
    """Manages database migrations and schema versioning."""
    
    MIGRATIONS_TABLE = "_schema_migrations"
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    def _ensure_migrations_table(self, conn: sqlite3.Connection) -> None:
        """Create migrations tracking table if it doesn't exist."""
        conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {self.MIGRATIONS_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER UNIQUE NOT NULL,
            description TEXT,
            applied_at TEXT NOT NULL
        )
        """)
        conn.commit()
    
    def get_applied_migrations(self, conn: sqlite3.Connection) -> set:
        """Get set of applied migration versions."""
        cursor = conn.execute(f"SELECT version FROM {self.MIGRATIONS_TABLE}")
        return {row[0] for row in cursor.fetchall()}
    
    def get_current_version(self, conn: sqlite3.Connection) -> int:
        """Get the current database schema version."""
        cursor = conn.execute(
            f"SELECT MAX(version) FROM {self.MIGRATIONS_TABLE}"
        )
        result = cursor.fetchone()
        return result[0] if result[0] is not None else 0
    
    def apply_migration(self, conn: sqlite3.Connection, migration_class) -> None:
        """Apply a single migration."""
        migration = migration_class()
        
        try:
            migration.up(conn)
            
            conn.execute(
                f"""
                INSERT INTO {self.MIGRATIONS_TABLE} (version, description, applied_at)
                VALUES (?, ?, ?)
                """,
                (
                    migration.version,
                    migration.description,
                    datetime.utcnow().isoformat(),
                ),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise RuntimeError(
                f"Failed to apply migration {migration.version}: {e}"
            ) from e
    
    def migrate(self, conn: sqlite3.Connection) -> None:
        """Apply all pending migrations."""
        self._ensure_migrations_table(conn)
        
        applied = self.get_applied_migrations(conn)
        
        for migration_class in MIGRATIONS:
            migration = migration_class()
            if migration.version not in applied:
                self.apply_migration(conn, migration_class)
    
    def rollback(self, conn: sqlite3.Connection, steps: int = 1) -> None:
        """Rollback the last N migrations."""
        self._ensure_migrations_table(conn)
        
        for _ in range(steps):
            current_version = self.get_current_version(conn)
            if current_version == 0:
                break
            
            # Find and rollback the migration
            for migration_class in reversed(MIGRATIONS):
                migration = migration_class()
                if migration.version == current_version:
                    try:
                        migration.down(conn)
                        conn.execute(
                            f"DELETE FROM {self.MIGRATIONS_TABLE} WHERE version = ?",
                            (current_version,),
                        )
                        conn.commit()
                    except Exception as e:
                        conn.rollback()
                        raise RuntimeError(
                            f"Failed to rollback migration {current_version}: {e}"
                        ) from e
                    break
