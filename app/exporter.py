"""CSV hardcopy plus the plain-text list for Discord."""
import csv
import json
import os
from datetime import datetime

from .db import db, EXPORT_DIR, now, get_setting_conn

BASE_HEADER = [
    "Title", "EA", "own", "Portable", "Date Added", "Last Updated",
    "Is Verified", "Grade", "Completed", "Badge Eligiblity", "emulator", "Notes",
]
NEW_HEADER = [
    "Playtime (min)", "Keep/Remove", "Graded By", "Verified By",
    "Broken", "Repack", "Steam AppID", "Status", "Date Removed",
]
HEADER = BASE_HEADER + NEW_HEADER


def grade_columns(conn) -> list:
    """One column per grade seat, named after its account.

    The plain Grade column keeps carrying the harsher of the two, so the sheet
    reads the same way it always did and the removal rule can be checked off it.
    """
    return [(r["id"], "Grade: %s" % r["username"]) for r in conn.execute(
        "SELECT id, username FROM users WHERE grade_seat IN (1, 2) ORDER BY grade_seat")]


def _tf(value) -> str:
    return "TRUE" if value else "FALSE"


def _date_us(value) -> str:
    if not value:
        return ""
    try:
        d = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return str(value)
    return "%d/%d/%s" % (d.month, d.day, d.strftime("%y"))


def _ordered_sections(conn):
    seen = [r["section"] for r in conn.execute(
        "SELECT section, MIN(id) AS first_id FROM games GROUP BY section ORDER BY first_id")]
    try:
        order = [s for s in json.loads(get_setting_conn(conn, "section_order", "") or "[]") if s]
    except ValueError:
        order = []
    for section in seen:
        if section not in order:
            order.append(section)
    return order


def build_rows() -> list:
    with db() as conn:
        users = {r["id"]: r["username"] for r in conn.execute("SELECT id, username FROM users")}
        games = conn.execute("SELECT * FROM games ORDER BY COALESCE(section, ''), id").fetchall()
        order = _ordered_sections(conn)
        seats = grade_columns(conn)
        held = {}
        for r in conn.execute("SELECT game_id, user_id, grade FROM game_grades"):
            held.setdefault(r["game_id"], {})[r["user_id"]] = r["grade"] or ""

    header = HEADER + [name for _, name in seats]

    by_section = {}
    for g in games:
        by_section.setdefault(g["section"], []).append(g)

    rows = [header]
    for section in order:
        if section:
            rows.append([section] + [""] * (len(header) - 1))
        for g in by_section.get(section, []):
            rows.append([
                g["title"], _tf(g["legacy_ea"]), _tf(g["legacy_own"]), _tf(g["legacy_portable"]),
                _date_us(g["date_added"]), _date_us(g["last_updated"]), _tf(g["verified"]),
                g["grade"] or "", _tf(g["legacy_completed"]), g["legacy_badge"] or "",
                g["legacy_emulator"] or "", g["notes"] or "",
                g["playtime_minutes"] if g["playtime_minutes"] is not None else "",
                g["keep_flag"] or "", users.get(g["graded_by"], ""), users.get(g["verified_by"], ""),
                _tf(g["broken"]), g["repack"] or "", g["steam_appid"] or "", g["status"],
                _date_us((g["removed_at"] or "")[:10]),
            ] + [held.get(g["id"], {}).get(uid, "") for uid, _ in seats])
    return rows


def export(tag: str = "auto") -> dict:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    rows = build_rows()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot = os.path.join(EXPORT_DIR, "masterlist-%s-%s.csv" % (stamp, tag))
    current = os.path.join(EXPORT_DIR, "masterlist-current.csv")
    for path in (snapshot, current):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    prune(keep=60)
    return {"snapshot": snapshot, "current": current, "rows": len(rows) - 1, "at": now()}


def prune(keep: int = 60) -> None:
    try:
        snaps = sorted(f for f in os.listdir(EXPORT_DIR)
                       if f.startswith("masterlist-") and f != "masterlist-current.csv")
    except FileNotFoundError:
        return
    for stale in (snaps[:-keep] if len(snaps) > keep else []):
        try:
            os.remove(os.path.join(EXPORT_DIR, stale))
        except OSError:
            pass


# ------------------------------------------------------------------- plain text

FORMATS = {
    "markdown": "* [{title}]({url})",
    "title_appid": "{title}\t{appid}",
    "titles": "{title}",
    "urls": "{url}",
}


def plain_text(rows, fmt: str = "markdown") -> str:
    """One line per game. Games with no store link fall back to the bare title."""
    template = FORMATS.get(fmt, FORMATS["markdown"])
    out = []
    for g in rows:
        url = g["store_url"] or ""
        appid = g["steam_appid"] or ""
        if fmt == "urls" and not url:
            continue
        if fmt == "markdown" and not url:
            out.append("* %s" % g["title"])
            continue
        out.append(template.format(title=g["title"], url=url, appid=appid))
    return "\n".join(out)
