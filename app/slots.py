"""The slot economy.

Balance starts at the limit (default 50). Adding a game spends one slot.
Every N checks (default 2) returns one slot. The balance is capped at the
limit - surplus checks are not banked.

Only a game that actually runs earns credit. A broken one has not left the
pile of work, and paying for a new game with it would mean the broken ones
never get dealt with.

Games added pre-tested spend a slot like anything else but earn nothing back,
because they never joined the unchecked pile in the first place.
"""
from .db import db, get_setting_conn, now, log_audit


def _state(conn):
    return conn.execute("SELECT balance, check_credit FROM slot_state WHERE id = 1").fetchone()


def _limit(conn) -> int:
    return int(get_setting_conn(conn, "slot_limit", "50"))


def _checks_per_slot(conn) -> int:
    return max(1, int(get_setting_conn(conn, "checks_per_slot", "2")))


def status(conn) -> dict:
    st = _state(conn)
    limit = _limit(conn)
    per = _checks_per_slot(conn)
    unchecked = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE verified = 0 AND status = 'active'"
    ).fetchone()["n"]
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM games WHERE status = 'active'"
    ).fetchone()["n"]
    verified = total - unchecked
    return {
        "balance": st["balance"],
        "limit": limit,
        "check_credit": st["check_credit"],
        "checks_per_slot": per,
        "checks_to_next_slot": (per - st["check_credit"]) if st["balance"] < limit else None,
        "at_cap": st["balance"] >= limit,
        "unchecked": unchecked,
        "verified": verified,
        "total": total,
        "pct_verified": round((verified / total) * 100, 1) if total else 0.0,
    }


def _apply(conn, delta: int, reason: str, game_id=None, user_id=None) -> int:
    st = _state(conn)
    limit = _limit(conn)
    balance = st["balance"] + delta
    if delta > 0:
        balance = min(balance, limit)
    conn.execute("UPDATE slot_state SET balance = ? WHERE id = 1", (balance,))
    conn.execute(
        "INSERT INTO slot_events (delta, reason, game_id, user_id, balance_after, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (delta, reason, game_id, user_id, balance, now()),
    )
    return balance


def spend(conn, game_id=None, user_id=None, reason: str = "game added") -> int:
    """Consume one slot. Allowed to go negative so an admin is never hard-blocked."""
    return _apply(conn, -1, reason, game_id, user_id)


def credit_check(conn, game_id=None, user_id=None) -> bool:
    """Register one completed check. Returns True if it freed a slot."""
    st = _state(conn)
    limit = _limit(conn)
    per = _checks_per_slot(conn)

    if st["balance"] >= limit:
        # At cap: nothing to refill, and surplus is not banked.
        conn.execute("UPDATE slot_state SET check_credit = 0 WHERE id = 1")
        return False

    credit = st["check_credit"] + 1
    if credit >= per:
        credit -= per
        conn.execute("UPDATE slot_state SET check_credit = ? WHERE id = 1", (credit,))
        _apply(conn, +1, "check credit", game_id, user_id)
        return True

    conn.execute("UPDATE slot_state SET check_credit = ? WHERE id = 1", (credit,))
    return False


def refund(conn, game_id=None, user_id=None, reason: str = "add undone") -> int:
    return _apply(conn, +1, reason, game_id, user_id)


def set_balance(conn, balance: int, user_id=None, reason: str = "admin adjustment") -> int:
    st = _state(conn)
    delta = balance - st["balance"]
    conn.execute("UPDATE slot_state SET balance = ? WHERE id = 1", (balance,))
    conn.execute(
        "INSERT INTO slot_events (delta, reason, game_id, user_id, balance_after, created_at)"
        " VALUES (?, ?, NULL, ?, ?, ?)",
        (delta, reason, user_id, balance, now()),
    )
    return balance


def recent_events(conn, limit: int = 25):
    return conn.execute(
        "SELECT e.*, g.title, u.username AS display_name FROM slot_events e"
        " LEFT JOIN games g ON g.id = e.game_id"
        " LEFT JOIN users u ON u.id = e.user_id"
        " ORDER BY e.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
