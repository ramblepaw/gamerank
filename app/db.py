"""SQLite storage. One file, mounted volume, no server dependency."""
import os
import re
import sqlite3
import hashlib
import secrets
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone

DATA_DIR = os.environ.get("GRT_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "gamerank.db")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

GRADES = ["S", "A", "B", "C", "D"]
BROKEN_STATUSES = ["unaddressed", "investigating", "fixed_recheck", "unfixable"]
GAME_STATUSES = ["active", "removed"]

# Date the first game went on the server. Used when a row has no date.
FALLBACK_DATE = "2025-02-04"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS games (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,
    title_norm        TEXT NOT NULL,
    section           TEXT,
    date_added        TEXT,
    last_updated      TEXT,
    notes             TEXT,

    verified          INTEGER NOT NULL DEFAULT 0,
    verified_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    verified_at       TEXT,
    pre_tested        INTEGER NOT NULL DEFAULT 0,

    broken            INTEGER NOT NULL DEFAULT 0,
    broken_status     TEXT,

    grade             TEXT,
    graded_by         INTEGER REFERENCES users(id) ON DELETE SET NULL,
    graded_at         TEXT,
    playtime_minutes  INTEGER,
    keep_flag         TEXT,

    status            TEXT NOT NULL DEFAULT 'active',
    removed_at        TEXT,

    version           TEXT,

    steam_appid       INTEGER,
    igdb_id           INTEGER,
    cover_url         TEXT,
    store_url         TEXT,
    release_date      TEXT,
    meta_source       TEXT,
    meta_fetched_at   TEXT,

    legacy_ea         INTEGER DEFAULT 0,
    legacy_own        INTEGER DEFAULT 0,
    legacy_portable   INTEGER DEFAULT 0,
    legacy_completed  INTEGER DEFAULT 0,
    legacy_badge      TEXT,
    legacy_emulator   TEXT,
    legacy_controller TEXT,

    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_games_verified ON games(verified, status);
CREATE INDEX IF NOT EXISTS idx_games_grade ON games(grade, status);
CREATE INDEX IF NOT EXISTS idx_games_titlenorm ON games(title_norm);
CREATE INDEX IF NOT EXISTS idx_games_added ON games(date_added);

CREATE TABLE IF NOT EXISTS slot_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    balance      INTEGER NOT NULL,
    check_credit INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS slot_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    delta         INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    game_id       INTEGER REFERENCES games(id) ON DELETE SET NULL,
    user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    balance_after INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_slots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    queue      TEXT NOT NULL,
    position   INTEGER NOT NULL,
    game_id    INTEGER REFERENCES games(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, queue, position)
);

CREATE TABLE IF NOT EXISTS wishlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    title_norm  TEXT NOT NULL,
    steam_appid INTEGER,
    store_url   TEXT,
    cover_url   TEXT,
    notes       TEXT,
    added_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_themes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    slug       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    based_on   TEXT,
    tokens     TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id    INTEGER REFERENCES games(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit(created_at);
"""

DEFAULT_SETTINGS = {
    "slot_limit": "50",
    "checks_per_slot": "2",
    "queue_size": os.environ.get("GRT_QUEUE_SIZE", "5"),
    "library_target": "2000",
    "imported": "0",
}

# Columns added after the first release. Applied to existing databases on boot.
DEFAULT_THEME = "dusk"

THEMES = [
    ("dusk",    "Dusk",    "aubergine and rose"),
    ("archive", "Archive", "walnut and amber"),
    ("ember",   "Ember",   "charcoal and hot orange"),
    ("moss",    "Moss",    "deep green and brass"),
    ("paper",   "Paper",   "light, warm, ink on card"),
]

# Tokens a custom theme may set. Everything else is derived in CSS.
THEME_TOKENS = [
    ("bg",        "Page background"),
    ("bg-2",      "Background, upper"),
    ("panel",     "Panel"),
    ("panel-2",   "Panel, raised"),
    ("sunk",      "Inputs and wells"),
    ("line",      "Hairline"),
    ("line-2",    "Border"),
    ("text",      "Text"),
    ("dim",       "Muted text"),
    ("accent",    "Accent"),
    ("accent-2",  "Accent, bright"),
    ("on-accent", "Text on accent"),
    ("ok",        "Good"),
    ("bad",       "Bad"),
    ("gS",        "Grade S"),
    ("gA",        "Grade A"),
    ("gB",        "Grade B"),
    ("gC",        "Grade C"),
    ("gD",        "Grade D"),
]
ACCENTS = ["c9852f", "e2703a", "c94f4f", "b55ea8", "5f9e6e", "3f8fa8", "d4a017", "9a8c7c"]
DENSITIES = ["comfortable", "compact"]
TILE_SIZES = {"small": 118, "medium": 150, "large": 196}

MIGRATIONS = [
    ("games", "steam_appid", "INTEGER"),
    ("games", "igdb_id", "INTEGER"),
    ("games", "cover_url", "TEXT"),
    ("games", "store_url", "TEXT"),
    ("games", "release_date", "TEXT"),
    ("games", "meta_source", "TEXT"),
    ("games", "meta_fetched_at", "TEXT"),
    ("users", "theme", "TEXT"),
    ("users", "accent", "TEXT"),
    ("users", "density", "TEXT"),
    ("users", "tile_size", "TEXT"),
    ("users", "motion", "TEXT"),
    # Slated = still on the server, queued for the next batch deletion.
    ("games", "slated_at", "TEXT"),
    # Last masterlist filter set, so returning to it resumes where you were.
    ("users", "library_filters", "TEXT"),
    # A shared, read-only account for people who just want to look.
    ("users", "is_guest", "INTEGER"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def norm_title(title: str) -> str:
    """Loose key for matching titles across sources.

    Accents are folded rather than stripped: without this "Pokemon" and
    "Pokemon" (with the accented e IGDB uses) normalise to different keys and
    can never match.
    """
    folded = unicodedata.normalize("NFKD", title or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).lower()
    # "Bits & Bops" and "Bits and Bops" are the same game; dropping the
    # ampersand instead of reading it leaves them permanently unequal.
    folded = folded.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", folded)


def hash_password(password: str) -> str:
    if not password:
        return ""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return True
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), bytes.fromhex(salt), 120_000).hex()
    return secrets.compare_digest(calc, digest)


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn, table: str):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)

        for table, column, decl in MIGRATIONS:
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        # Older builds split notes three ways. Fold them back into one field.
        cols = _columns(conn, "games")
        for legacy in ("grade_notes", "broken_notes", "removed_reason"):
            if legacy in cols:
                conn.execute(
                    "UPDATE games SET notes = TRIM(COALESCE(notes, '') ||"
                    f" CASE WHEN {legacy} IS NOT NULL AND {legacy} != ''"
                    f" THEN CASE WHEN COALESCE(notes,'') = '' THEN '' ELSE ' | ' END || {legacy}"
                    " ELSE '' END)"
                    f" WHERE {legacy} IS NOT NULL AND {legacy} != ''"
                )
                conn.execute(f"UPDATE games SET {legacy} = NULL")

        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        if conn.execute("SELECT COUNT(*) AS n FROM slot_state").fetchone()["n"] == 0:
            limit = int(get_setting_conn(conn, "slot_limit", "50"))
            conn.execute("INSERT INTO slot_state (id, balance, check_credit) VALUES (1, ?, 0)", (limit,))
        if conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin, created_at)"
                " VALUES (?, ?, ?, 1, ?)",
                ("admin", "Admin", "", now()),
            )


def get_setting_conn(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_setting(key: str, default: str = "") -> str:
    with db() as conn:
        return get_setting_conn(conn, key, default)


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def log_audit(conn, game_id, user_id, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit (game_id, user_id, action, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (game_id, user_id, action, detail, now()),
    )
