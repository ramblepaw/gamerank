"""Queue slates.

Each user holds a fixed slate of N games per queue. Random draw, because size
and setup effort vary so much that any manual pick becomes a biased pick.
Resolve one and the rest move up, with the replacement added at the bottom. A
game held in one user's slate is not offered to anyone else.
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
    """Top the slate back up to queue_size, adding new games at the bottom.

    Positions stay a compact run. Resolve one and the games under it move up,
    so its replacement arrives at the end of the slate rather than appearing in
    a row that has just been dealt with and read as still needing attention.
    """
    size = queue_size(conn)

    current = [
        (r["position"], r["game_id"])
        for r in conn.execute(
            "SELECT position, game_id FROM queue_slots WHERE user_id = ? AND queue = ?"
            " ORDER BY position",
            (user_id, queue),
        )
    ]
    held = [gid for _, gid in current if gid is not None]

    # Drop anything that no longer belongs in this queue (resolved elsewhere).
    valid = {r["id"] for r in conn.execute(POOL_SQL[queue])}
    kept, seen = [], set()
    for gid in held:
        if gid in valid and gid not in seen:
            kept.append(gid)
            seen.add(gid)
    kept = kept[:size]

    if len(kept) < size:
        taken = set(kept) | _held_elsewhere(conn, user_id, queue)
        sql = POOL_SQL[queue]
        if taken:
            sql += " AND id NOT IN (%s)" % ",".join("?" for _ in taken)
        sql += " ORDER BY RANDOM() LIMIT ?"
        kept += [r["id"] for r in conn.execute(sql, (*taken, size - len(kept)))]

    wanted = list(enumerate(kept))
    if wanted == current:                    # every page view calls this
        return

    conn.execute("DELETE FROM queue_slots WHERE user_id = ? AND queue = ?", (user_id, queue))
    stamp = now()
    for pos, gid in wanted:
        conn.execute(
            "INSERT INTO queue_slots (user_id, queue, position, game_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (user_id, queue, pos, gid, stamp),
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
    """Take a resolved game out. refill() closes the gap and appends a new one."""
    conn.execute(
        "DELETE FROM queue_slots WHERE user_id = ? AND queue = ? AND game_id = ?",
        (user_id, queue, game_id),
    )

