"""Import the Google Sheets masterlist CSV export."""
import csv
import re
import json
import io
from datetime import datetime

from . import grades
from .db import (db, now, norm_title, sort_title, GRADES,
                 get_setting_conn, FALLBACK_DATE, backfill)

HEADER_MAP = {
    "title": "title",
    "date added": "date_added",
    "last updated": "last_updated",
    "notes": "notes",
    "is verified": "verified",
    "grade": "grade",
    "ea": "legacy_ea",
    "own": "legacy_own",
    "portable": "legacy_portable",
    "completed": "legacy_completed",
    "badge eligiblity": "legacy_badge",
    "badge eligibility": "legacy_badge",
    "emulator": "legacy_emulator",
    "playtime (min)": "playtime_minutes",
    "keep/remove": "keep_flag",
    "steam appid": "steam_appid",
    "broken": "broken",
    "repack": "repack",
}


def _bool(value: str) -> int:
    return 1 if (value or "").strip().upper() == "TRUE" else 0


def _int(value: str):
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _date(value: str):
    """Returns an ISO date, or None if the cell can't be read as one.

    The sheet has a handful of doubled-slash typos ("9/21//25"), which would
    otherwise be stored verbatim and break every sort that touches dates.
    """
    raw = re.sub(r"/{2,}", "/", (value or "").strip())
    if not raw:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_csv(text: str) -> dict:
    rows = list(csv.reader(io.StringIO(text)))

    header_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip().lower() == "title":
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Could not find a header row starting with 'Title'.")

    header = [c.strip().lower() for c in rows[header_idx]]
    cols = {}
    # "Grade: alice" restores alice's own grade; the plain Grade column is the
    # rolled-up one and is recomputed rather than trusted.
    grader_cols = {}
    for idx, name in enumerate(header):
        if name in HEADER_MAP:
            cols[HEADER_MAP[name]] = idx
        elif name.startswith("grade:"):
            grader_cols[name.split(":", 1)[1].strip()] = idx

    if "title" not in cols:
        raise ValueError("No Title column found.")

    games, sections, warnings = [], [], []
    current_section = None
    no_date = 0

    for i, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or not any(c.strip() for c in row):
            continue
        first = row[0].strip()
        rest = "".join(c.strip() for c in row[1:])
        if first and not rest:
            current_section = first
            sections.append(first)
            continue
        if not first:
            continue

        def cell(key, default=""):
            idx = cols.get(key)
            if idx is None or idx >= len(row):
                return default
            return row[idx].strip()

        grade = cell("grade").upper()
        notes = cell("notes")
        if grade and grade not in GRADES:
            warnings.append('row %d: "%s" in the Grade column for %s' % (i, cell("grade"), first))
            notes = (notes + " | " if notes else "") + cell("grade")
            grade = ""

        date_added = _date(cell("date_added"))
        if not date_added:
            date_added = FALLBACK_DATE
            no_date += 1

        by_grader = {}
        for who, idx in grader_cols.items():
            value = (row[idx].strip().upper() if idx < len(row) else "")
            if value in GRADES:
                by_grader[who] = value

        games.append({
            "title": first,
            "title_norm": norm_title(first),
            "title_sort": sort_title(first),
            "graders": by_grader,
            "section": current_section,
            "date_added": date_added,
            "last_updated": _date(cell("last_updated")),
            "notes": notes or None,
            "verified": _bool(cell("verified")),
            "broken": _bool(cell("broken")),
            "repack": cell("repack").strip().lower() or None,
            "grade": grade or None,
            "playtime_minutes": _int(cell("playtime_minutes")),
            "keep_flag": cell("keep_flag").lower() or None,
            "steam_appid": _int(cell("steam_appid")),
            "legacy_ea": _bool(cell("legacy_ea")),
            "legacy_own": _bool(cell("legacy_own")),
            "legacy_portable": _bool(cell("legacy_portable")),
            "legacy_completed": _bool(cell("legacy_completed")),
            "legacy_badge": cell("legacy_badge") or None,
            "legacy_emulator": cell("legacy_emulator") or None,
        })

    seen, dupes = set(), []
    for g in games:
        if g["title_norm"] in seen:
            dupes.append(g["title"])
        seen.add(g["title_norm"])

    return {
        "games": games, "sections": sections, "warnings": warnings,
        "duplicates": dupes, "no_date": no_date,
    }


def import_csv(text: str, user_id=None, replace: bool = True) -> dict:
    parsed = parse_csv(text)
    games = parsed["games"]

    with db() as conn:
        if replace:
            # Clear the rows that point at games first, or the delete trips a
            # foreign key on any database that already has history in it.
            conn.execute("DELETE FROM queue_slots")
            conn.execute("DELETE FROM audit")
            conn.execute("UPDATE slot_events SET game_id = NULL")
            conn.execute("DELETE FROM games")

        restored = set()
        for g in games:
            cur = conn.execute(
                "INSERT INTO games (title, title_norm, title_sort, section, date_added,"
                " last_updated, notes,"
                " verified, broken, repack, grade, playtime_minutes, keep_flag,"
                " steam_appid, store_url,"
                " legacy_ea, legacy_own, legacy_portable, legacy_completed, legacy_badge,"
                " legacy_emulator, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    g["title"], g["title_norm"], g["title_sort"], g["section"], g["date_added"],
                    g["last_updated"],
                    g["notes"], g["verified"], g["broken"], g["repack"], g["grade"],
                    g["playtime_minutes"], g["keep_flag"],
                    g["steam_appid"],
                    "https://store.steampowered.com/app/%d/" % g["steam_appid"] if g["steam_appid"] else None,
                    g["legacy_ea"], g["legacy_own"], g["legacy_portable"], g["legacy_completed"],
                    g["legacy_badge"], g["legacy_emulator"], now(), now(),
                ),
            )
            for who, letter in g["graders"].items():
                owner = conn.execute("SELECT id FROM users WHERE username = ?", (who,)).fetchone()
                if owner:
                    conn.execute(
                        "INSERT OR REPLACE INTO game_grades (game_id, user_id, grade,"
                        " playtime_minutes, keep_flag, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (cur.lastrowid, owner["id"], letter, g["playtime_minutes"],
                         g["keep_flag"], now(), now()))
                    restored.add(cur.lastrowid)

        # Categories seen in the sheet become rows, and anything else the new
        # columns need gets filled in.
        for pos, name in enumerate(dict.fromkeys(parsed["sections"])):
            conn.execute("INSERT OR IGNORE INTO sections (name, position, created_at)"
                         " VALUES (?, ?, ?)", (name, pos, now()))
        backfill(conn)
        # The plain Grade column is derived, so rebuild it from what was restored
        # rather than trusting whatever the sheet happened to hold.
        for game_id in restored:
            grades.recompute(conn, game_id)

        limit = int(get_setting_conn(conn, "slot_limit", "50"))
        intake_unverified = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE verified = 0 AND status = 'active'"
            " AND section LIKE '%Recently Added%'"
        ).fetchone()["n"]
        seeded = max(0, limit - intake_unverified)
        conn.execute("UPDATE slot_state SET balance = ?, check_credit = 0 WHERE id = 1", (seeded,))
        conn.execute(
            "INSERT INTO slot_events (delta, reason, game_id, user_id, balance_after, created_at)"
            " VALUES (0, ?, NULL, ?, ?, ?)",
            ("import", user_id, seeded, now()),
        )
        for key, value in (("imported", "1"), ("section_order", json.dumps(parsed["sections"]))):
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    return {
        "inserted": len(games),
        "sections": parsed["sections"],
        "warnings": parsed["warnings"],
        "duplicates": parsed["duplicates"],
        "no_date": parsed["no_date"],
        "seeded_balance": seeded,
        "intake_unverified": intake_unverified,
    }
