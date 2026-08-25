"""Queue slates.

Each user holds a fixed slate of N games per queue. Random draw, because size
and setup effort vary so much that any manual pick becomes a biased pick.
Resolve one and the empty position refills. A game held in one user's slate is
not offered to anyone else.
"""
from .db import db, now, get_setting_conn

VERIFY = "verify"
GRADE = "grade"

POOL_SQL = {
    VERIFY: "SELECT id FROM games WHERE verified = 0 AND status = 'active'",
    GRADE: "SELECT id FROM games WHERE verified = 1 AND grade IS NULL AND status = 'active'",
}


def queue_size(conn) -> int:
    return max(1, int(get_setting_conn(conn, "queue_size", "5")))


def _held_elsewhere(conn, user_id: int, queue: str):
    rows = conn.execute(
        "SELECT game_id FROM queue_slots WHERE queue = ? AND user_id != ? AND game_id IS NOT NULL",
        (queue, user_id),
    ).fetchall()
    return {r["game_id"] for r in rows}


def pool_count(conn, queue: str) -> int:
    return conn.execute(
        f"SELECT COUNT(*) AS n FROM ({POOL_SQL[queue]})"
    ).fetchone()["n"]


def refill(conn, user_id: int, queue: str) -> None:
    """Top the slate back up to queue_size with random eligible games."""
    size = queue_size(conn)

    conn.execute(
        "DELETE FROM queue_slots WHERE user_id = ? AND queue = ? AND position >= ?",
        (user_id, queue, size),
    )

    existing = {
        r["position"]: r["game_id"]
        for r in conn.execute(
            "SELECT position, game_id FROM queue_slots WHERE user_id = ? AND queue = ?",
            (user_id, queue),
        )
    }

    # Drop anything that no longer belongs in this queue (resolved elsewhere).
    valid = {r["id"] for r in conn.execute(POOL_SQL[queue])}
    for pos, gid in list(existing.items()):
        if gid is not None and gid not in valid:
            conn.execute(
                "UPDATE queue_slots SET game_id = NULL WHERE user_id = ? AND queue = ? AND position = ?",
                (user_id, queue, pos),
            )
            existing[pos] = None

    taken = {g for g in existing.values() if g is not None} | _held_elsewhere(conn, user_id, queue)
    need = [p for p in range(size) if existing.get(p) is None]
    if not need:
        return

    placeholders = ",".join("?" for _ in taken) if taken else ""
    sql = POOL_SQL[queue]
    if taken:
        sql += f" AND id NOT IN ({placeholders})"
    sql += " ORDER BY RANDOM() LIMIT ?"
    picks = [r["id"] for r in conn.execute(sql, (*taken, len(need)))]

    for pos in need:
        gid = picks.pop() if picks else None
        conn.execute(
            "INSERT INTO queue_slots (user_id, queue, position, game_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id, queue, position) DO UPDATE SET game_id = excluded.game_id",
            (user_id, queue, pos, gid, now()),
        )


def slate(conn, user_id: int, queue: str):
    refill(conn, user_id, queue)
    return conn.execute(
        "SELECT q.position, g.* FROM queue_slots q"
        " LEFT JOIN games g ON g.id = q.game_id"
        " WHERE q.user_id = ? AND q.queue = ? AND q.game_id IS NOT NULL"
        " ORDER BY q.position",
        (user_id, queue),
    ).fetchall()


def clear_slot(conn, user_id: int, queue: str, game_id: int) -> None:
    conn.execute(
        "UPDATE queue_slots SET game_id = NULL WHERE user_id = ? AND queue = ? AND game_id = ?",
        (user_id, queue, game_id),
    )

