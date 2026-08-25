"""Long-running background work.

The art fetch walks the whole library in one go rather than in batches - at
roughly three games a second it's minutes, not something worth clicking through
by hand. State lives in memory; a restart mid-run just means starting it again,
and anything already written stays written.
"""
import threading
import time

from .db import db, now, log_audit
from . import metadata

_lock = threading.Lock()
_state = {
    "name": None,
    "running": False,
    "cancel": False,
    "total": 0,
    "done": 0,
    "found": 0,
    "renamed": 0,
    "current": "",
    "started_at": None,
    "finished_at": None,
    "misses": [],
    "error": "",
}


def status() -> dict:
    with _lock:
        snap = dict(_state)
    snap["misses"] = snap["misses"][-50:]
    done, total = snap["done"], snap["total"]
    snap["pct"] = round(done / total * 100, 1) if total else 0.0
    if snap["running"] and done and snap["started_at"]:
        rate = done / max(0.001, time.time() - snap["started_at"])
        snap["eta_seconds"] = int((total - done) / rate) if rate else None
    else:
        snap["eta_seconds"] = None
    return snap


def cancel() -> None:
    with _lock:
        _state["cancel"] = True


def _reset(name: str, total: int) -> None:
    with _lock:
        _state.update({
            "name": name, "running": True, "cancel": False,
            "total": total, "done": 0, "found": 0, "renamed": 0, "current": "",
            "started_at": time.time(), "finished_at": None,
            "misses": [], "error": "",
        })


def _worker(rows, user_id, overwrite):
    try:
        for row in rows:
            with _lock:
                if _state["cancel"]:
                    break
                _state["current"] = row["title"]

            try:
                meta = metadata.lookup(row["title"], row["steam_appid"])
            except Exception as exc:                      # keep the run alive
                meta = {}
                with _lock:
                    _state["error"] = "%s: %s" % (row["title"][:40], exc)

            if meta and meta.get("cover_url"):
                # Only correct actual misspellings. A subtitle or prefix match
                # is not a typo - renaming "Shapez 2" to "Shapez 2: Factory"
                # would be rewriting a title that was already right.
                # Correct misspellings, but never swap a plain title for a
                # subtitled one: "Snalland" should not become "Smalland:
                # Survive the Wilds VR". The art is right either way; the
                # title is the part that's risky to rewrite.
                new_title = meta.get("title") or ""
                gains_subtitle = (":" in new_title and ":" not in row["title"])
                renamed = (overwrite and new_title
                           and meta.get("match_kind") in ("typo", "similar")
                           and not gains_subtitle
                           and new_title != row["title"])
                with db() as conn:
                    metadata.apply(conn, row["id"], meta, overwrite_title=renamed)
                    if renamed:
                        # Keep the old spelling so a bad rename can be undone.
                        log_audit(conn, row["id"], user_id, "renamed",
                                  '"%s" -> "%s"' % (row["title"], meta["title"]))
                with _lock:
                    _state["found"] += 1
                    if renamed:
                        _state["renamed"] += 1
            else:
                with _lock:
                    _state["misses"].append({
                        "id": row["id"], "title": row["title"],
                        "ambiguous": len(meta.get("ambiguous", [])) if meta else 0,
                    })

            with _lock:
                _state["done"] += 1
    finally:
        with _lock:
            _state["running"] = False
            _state["current"] = ""
            _state["finished_at"] = time.time()
            found, total, ren = _state["found"], _state["done"], _state["renamed"]
        try:
            with db() as conn:
                log_audit(conn, None, user_id, "art fetch",
                          "%d of %d matched, %d renamed" % (found, total, ren))
        except Exception:
            pass


def start_art_fetch(user_id=None, only_missing: bool = True,
                    overwrite_title: bool = False) -> dict:
    with _lock:
        if _state["running"]:
            return {"ok": False, "detail": "A fetch is already running."}
    if not metadata.igdb_available():
        return {"ok": False, "detail": "IGDB credentials are not set."}

    where = "status = 'active'"
    if only_missing:
        where += " AND (cover_url IS NULL OR cover_url = '')"
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, steam_appid FROM games WHERE " + where +
            " ORDER BY MAX(COALESCE(last_updated, ''), COALESCE(date_added, '')) DESC, id DESC")]

    if not rows:
        return {"ok": False, "detail": "Nothing to fetch - every game already has art."}

    _reset("art", len(rows))
    threading.Thread(target=_worker, args=(rows, user_id, overwrite_title),
                     daemon=True).start()
    return {"ok": True, "detail": "Fetching art for %d games." % len(rows)}
