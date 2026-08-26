"""Per-grader grades.

Both people grade a game independently and neither overwrites the other. The
game still carries a derived grade - the harsher of the two - because that is
what the removal rule and every filter ask for: one C from either of you makes
a game a candidate.
"""
from .db import now, GRADES

# S first, D last, so a higher position is a harsher grade.
SEVERITY = {letter: i for i, letter in enumerate(GRADES)}


def set_grade(conn, game_id: int, user_id: int, grade: str,
              minutes, keep_flag: str) -> None:
    stamp = now()
    if grade:
        conn.execute(
            "INSERT INTO game_grades (game_id, user_id, grade, playtime_minutes,"
            " keep_flag, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(game_id, user_id) DO UPDATE SET grade = excluded.grade,"
            " playtime_minutes = excluded.playtime_minutes,"
            " keep_flag = excluded.keep_flag, updated_at = excluded.updated_at",
            (game_id, user_id, grade, minutes, keep_flag or None, stamp, stamp))
    else:
        # Clearing your grade removes the row, leaving the other person's to stand.
        conn.execute("DELETE FROM game_grades WHERE game_id = ? AND user_id = ?",
                     (game_id, user_id))
    recompute(conn, game_id)


def recompute(conn, game_id: int) -> None:
    """Roll the individual grades back up onto the game.

    Deliberately does not touch last_updated: grading is not a new build of the
    game, and treating it as one is what pushed verified games to the top of
    Recently Added.
    """
    rows = conn.execute(
        "SELECT user_id, grade, playtime_minutes, keep_flag, updated_at"
        " FROM game_grades WHERE game_id = ? ORDER BY updated_at DESC, user_id",
        (game_id,)).fetchall()

    letters = [r["grade"] for r in rows if r["grade"]]
    worst = max(letters, key=lambda g: SEVERITY.get(g, -1)) if letters else None
    minutes = [r["playtime_minutes"] for r in rows if r["playtime_minutes"] is not None]
    if any(r["keep_flag"] == "remove" for r in rows):
        keep = "remove"
    elif any(r["keep_flag"] == "keep" for r in rows):
        keep = "keep"
    else:
        keep = None
    latest = rows[0] if rows else None

    conn.execute(
        "UPDATE games SET grade = ?, playtime_minutes = ?, keep_flag = ?,"
        " graded_by = ?, graded_at = ?, updated_at = ? WHERE id = ?",
        (worst, max(minutes) if minutes else None, keep,
         latest["user_id"] if latest else None,
         latest["updated_at"] if latest else None, now(), game_id))


def for_game(conn, game_id: int) -> dict:
    """Every grade on one game, keyed by grader."""
    return {r["user_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM game_grades WHERE game_id = ?", (game_id,))}


def seats(conn) -> list:
    """The accounts holding the two grade columns, left first."""
    return [dict(r) for r in conn.execute(
        "SELECT id, username, grade_seat FROM users"
        " WHERE grade_seat IN (1, 2) ORDER BY grade_seat")]


def open_book(viewer) -> bool:
    """Whether this account sees grades it did not set.

    The viewing account grades nothing, and exists so people can look a game up
    before suggesting it again - blind grading would leave it with nothing to
    read. Anyone who can grade plays blind.
    """
    return bool(viewer and viewer.get("is_guest"))


def panels(conn, game_id: int, viewer=None) -> list:
    """One entry per grade column, in display order, for the game page.

    You always see your own. The other person's stays hidden until you have
    graded the game yourself, so their letter cannot anchor yours. Nothing
    stands in for a hidden grade: a placeholder would still say "they have
    graded this", which is most of the anchor.
    """
    viewer_id = (viewer or {}).get("id")
    held = for_game(conn, game_id)
    revealed = open_book(viewer) or viewer_id in held
    out = []
    for seat in seats(conn):
        mine = seat["id"] == viewer_id
        row = held.get(seat["id"]) if (mine or revealed) else None
        out.append({
            "seat": seat["grade_seat"],
            "user_id": seat["id"],
            "username": seat["username"],
            "mine": mine,
            "hidden": not (mine or revealed),
            "grade": row["grade"] if row else None,
            "playtime_minutes": row["playtime_minutes"] if row else None,
            "keep_flag": row["keep_flag"] if row else None,
            "graded_at": row["updated_at"] if row else None,
        })
    return out


def attach(conn, rows, viewer) -> None:
    """Hang the visible per-seat grades on each row of a listing, in place.

    Same rule as panels(): your own always, the other person's only once you
    have graded that game too.
    """
    if not rows:
        return
    viewer_id = (viewer or {}).get("id")
    everything = open_book(viewer)
    seat_list = seats(conn)
    ids = [r["id"] for r in rows]

    held = {}
    for start in range(0, len(ids), 400):        # keep well inside SQLite's limit
        block = ids[start:start + 400]
        for r in conn.execute(
                "SELECT game_id, user_id, grade FROM game_grades WHERE game_id IN (%s)"
                % ",".join("?" for _ in block), block):
            held.setdefault(r["game_id"], {})[r["user_id"]] = r["grade"]

    for row in rows:
        on_game = held.get(row["id"], {})
        revealed = everything or viewer_id in on_game
        row["seats"] = [
            {"seat": s["grade_seat"], "username": s["username"],
             "mine": s["id"] == viewer_id,
             "grade": on_game.get(s["id"]) if (revealed or s["id"] == viewer_id) else None}
            for s in seat_list
        ]
