"""GameRank - verification, grading and slot tracking for a game library."""
import os
import re

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature

from . import slots, queues, exporter, importer, metadata, paste, jobs, themes
from .db import (
    db, init_db, now, today, norm_title, log_audit, get_setting_conn, set_setting,
    hash_password, verify_password, GRADES, BROKEN_STATUSES, EXPORT_DIR, FALLBACK_DATE, DATA_DIR,
    THEMES, ACCENTS, DENSITIES, TILE_SIZES, THEME_TOKENS, DEFAULT_THEME,
)

BASE = os.path.dirname(__file__)


# Stamped onto the stylesheet URL so a browser picks up a new build instead of
# serving the cached one after an update.
def _asset_version() -> str:
    try:
        return str(int(os.path.getmtime(os.path.join(BASE, "static", "style.css"))))
    except OSError:
        return "0"


ASSET_V = _asset_version()
signer = URLSafeSerializer(os.environ.get("GRT_SECRET", "dev-secret-change-me"), salt="session")

app = FastAPI(title="GameRank")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

# Everything the removal list treats as a candidate. One definition so the
# count on the dashboard and the rows on the page can't drift apart.
REMOVAL_SQL = (
    "status = 'active' AND (keep_flag = 'remove' OR grade IN ('C', 'D')"
    " OR broken_status = 'unfixable')"
)
REMOVAL_SQL_G = (
    "g.status = 'active' AND (g.keep_flag = 'remove' OR g.grade IN ('C', 'D')"
    " OR g.broken_status = 'unfixable')"
)


def preflight() -> None:
    """Fail with a readable reason rather than a traceback and a restart loop.

    A container that can't write its volume exits instantly, and with a restart
    policy that looks like the app flapping for no reason. Almost always it's
    the mounted directory not being writable by the uid the container runs as.
    """
    import getpass
    data_dir = os.environ.get("GRT_DATA_DIR", "")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        probe = os.path.join(DATA_DIR, ".write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        try:
            who = "uid %d, gid %d" % (os.getuid(), os.getgid())
        except AttributeError:                       # not POSIX
            who = getpass.getuser()
        raise SystemExit(
            "\n"
            "GameRank cannot write to its data directory.\n"
            "  directory : %s%s\n"
            "  running as: %s\n"
            "  error     : %s\n\n"
            "The database and CSV exports live there, so it has to be writable.\n"
            "If this is a container, the mounted path on the host needs to be\n"
            "owned by the uid above - for example:\n"
            "  chown -R %s '<host path>'\n"
            % (DATA_DIR, " (from GRT_DATA_DIR)" if data_dir else "", who, exc,
               who.replace("uid ", "").replace(", gid ", ":") if "uid" in who else "1000:1000")
        )


@app.on_event("startup")
def _startup():
    preflight()
    init_db()


# --------------------------------------------------------------------------- auth

def current_user(request: Request):
    raw = request.cookies.get("grt_session")
    if not raw:
        return None
    try:
        data = signer.loads(raw)
    except BadSignature:
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (data.get("uid"),)).fetchone()
    return dict(row) if row else None


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    return user


def require_admin(request: Request):
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin only.")
    return user


@app.exception_handler(HTTPException)
async def _redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 307 and "Location" in (exc.headers or {}):
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return HTMLResponse("<h1>%s</h1><p>%s</p><p><a href='/'>Back</a></p>"
                        % (exc.status_code, exc.detail), status_code=exc.status_code)


def prefs(user) -> dict:
    """Look and feel, per account. Falls back to the defaults for logged-out pages."""
    user = user or {}
    tile = TILE_SIZES.get(user.get("tile_size") or "medium", TILE_SIZES["medium"])
    theme = user.get("theme") or DEFAULT_THEME
    return {
        "theme": theme,
        "theme_tokens": themes.tokens_for(theme),
        "accent": user.get("accent") or "",
        "density": user.get("density") or "comfortable",
        "tile_size": user.get("tile_size") or "medium",
        "tile_px": tile,
        "motion": user.get("motion") or "on",
    }


def render(request: Request, template: str, **ctx):
    user = ctx.pop("user", None) or current_user(request)
    ctx["ui"] = prefs(user)
    ctx["asset_v"] = ASSET_V
    with db() as conn:
        ctx["slots"] = slots.status(conn)
        ctx["broken_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE broken = 1 AND status = 'active'"
            " AND COALESCE(broken_status, '') != 'unfixable'").fetchone()["n"]
        ctx["no_art_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'"
            " AND (cover_url IS NULL OR cover_url = '')").fetchone()["n"]
        ctx["target"] = int(get_setting_conn(conn, "library_target", "2000"))
    return templates.TemplateResponse(template, {"request": request, "user": user, **ctx})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    with db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, username FROM users ORDER BY username")]
    return templates.TemplateResponse(
        "login.html", {"request": request, "users": users, "user": None,
                       "error": None, "asset_v": ASSET_V})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form("")):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        users = [dict(r) for r in conn.execute(
            "SELECT id, username FROM users ORDER BY username")]
    if not row or not verify_password(password, row["password_hash"] or ""):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "users": users, "user": None,
             "error": "Wrong password.", "asset_v": ASSET_V},
            status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("grt_session", signer.dumps({"uid": row["id"]}),
                    httponly=True, max_age=60 * 60 * 24 * 90, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("grt_session")
    return resp


# ---------------------------------------------------------------------- dashboard

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(require_user)):
    with db() as conn:
        grade_dist = {r["grade"]: r["n"] for r in conn.execute(
            "SELECT grade, COUNT(*) AS n FROM games WHERE grade IS NOT NULL"
            " AND status = 'active' GROUP BY grade")}
        ungraded = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE verified = 1 AND grade IS NULL"
            " AND status = 'active'").fetchone()["n"]
        removal_ready = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE " + REMOVAL_SQL).fetchone()["n"]
        removed_total = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'removed'").fetchone()["n"]
        wishlist = conn.execute("SELECT COUNT(*) AS n FROM wishlist").fetchone()["n"]
        recent = [dict(r) for r in conn.execute(
            "SELECT *, MAX(COALESCE(last_updated, ''), COALESCE(date_added, '')) AS touched"
            " FROM games WHERE status = 'active' ORDER BY touched DESC, id DESC LIMIT 12")]
    return render(request, "dashboard.html", user=user, grade_dist=grade_dist,
                  ungraded=ungraded, removal_ready=removal_ready, removed_total=removed_total,
                  wishlist=wishlist, recent=recent, grades=GRADES)


# ------------------------------------------------------------------------- verify

@app.get("/verify", response_class=HTMLResponse)
def verify_queue(request: Request, user=Depends(require_user)):
    with db() as conn:
        games = [dict(r) for r in queues.slate(conn, user["id"], queues.VERIFY)]
        remaining = queues.pool_count(conn, queues.VERIFY)
    return render(request, "verify.html", user=user, games=games, remaining=remaining)


@app.post("/verify/{game_id}")
def verify_submit(request: Request, game_id: int, works: str = Form(...),
                  notes: str = Form(""), user=Depends(require_user)):
    ok = works == "yes"
    with db() as conn:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game.")
        if game["verified"]:
            queues.clear_slot(conn, user["id"], queues.VERIFY, game_id)
            return RedirectResponse("/verify", status_code=303)

        conn.execute(
            "UPDATE games SET verified = 1, verified_by = ?, verified_at = ?, last_updated = ?,"
            " broken = ?, broken_status = ?, notes = ?, updated_at = ? WHERE id = ?",
            (user["id"], now(), today(), 0 if ok else 1, None if ok else "unaddressed",
             notes.strip() or game["notes"], now(), game_id))
        slots.credit_check(conn, game_id, user["id"])
        log_audit(conn, game_id, user["id"], "verified", "works" if ok else "broken")
        queues.clear_slot(conn, user["id"], queues.VERIFY, game_id)
        queues.refill(conn, user["id"], queues.VERIFY)
    exporter.export(tag="verify")
    return RedirectResponse("/verify", status_code=303)


# -------------------------------------------------------------------------- grade

@app.get("/grade", response_class=HTMLResponse)
def grade_queue(request: Request, user=Depends(require_user)):
    with db() as conn:
        games = [dict(r) for r in queues.slate(conn, user["id"], queues.GRADE)]
        remaining = queues.pool_count(conn, queues.GRADE)
    return render(request, "grade.html", user=user, games=games,
                  remaining=remaining, grades=GRADES)


@app.post("/grade/{game_id}")
def grade_submit(request: Request, game_id: int, grade: str = Form(""),
                 playtime: str = Form(""), keep_flag: str = Form(""),
                 notes: str = Form(""), user=Depends(require_user)):
    grade = (grade or "").strip().upper()
    if grade and grade not in GRADES:
        raise HTTPException(400, "Unknown grade.")
    try:
        minutes = int(playtime) if str(playtime).strip() else None
    except ValueError:
        minutes = None

    with db() as conn:
        game = conn.execute("SELECT notes FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game.")
        conn.execute(
            "UPDATE games SET grade = ?, graded_by = ?, graded_at = ?, playtime_minutes = ?,"
            " keep_flag = ?, notes = ?, last_updated = ?, updated_at = ? WHERE id = ?",
            (grade or None, user["id"], now(), minutes, keep_flag or None,
             notes.strip() or game["notes"], today(), now(), game_id))
        log_audit(conn, game_id, user["id"], "graded", grade or "no grade")
        queues.clear_slot(conn, user["id"], queues.GRADE, game_id)
        queues.refill(conn, user["id"], queues.GRADE)
    exporter.export(tag="grade")
    return RedirectResponse("/grade", status_code=303)


# ------------------------------------------------------------------------ library

@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", verified: str = "", grade: str = "",
            keep: str = "", section: str = "", status: str = "active",
            sort: str = "title", view: str = "grid", page: int = 1,
            partial: int = 0, user=Depends(require_user)):
    where, params = [], []
    if q.strip():
        where.append("g.title_norm LIKE ?")
        params.append("%" + norm_title(q) + "%")
    if verified in ("0", "1"):
        where.append("g.verified = ?")
        params.append(int(verified))
    if grade == "none":
        where.append("g.grade IS NULL")
    elif grade in GRADES:
        where.append("g.grade = ?")
        params.append(grade)
    if keep in ("keep", "remove"):
        where.append("g.keep_flag = ?")
        params.append(keep)
    if section:
        where.append("g.section = ?")
        params.append(section)
    if status == "slated":
        where.append("g.status = 'active' AND g.slated_at IS NOT NULL")
    elif status in ("active", "removed"):
        where.append("g.status = ?")
        params.append(status)

    sorts = {
        "title": "g.title COLLATE NOCASE",
        "added": "MAX(COALESCE(g.last_updated, ''), COALESCE(g.date_added, '')) DESC, g.id DESC",
        "updated": "g.last_updated DESC",
        "grade": "CASE g.grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2"
                 " WHEN 'C' THEN 3 WHEN 'D' THEN 4 ELSE 5 END, g.title COLLATE NOCASE",
        "playtime": "COALESCE(g.playtime_minutes, -1) ASC, g.title COLLATE NOCASE",
    }
    order = sorts.get(sort, sorts["title"])
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    page = max(1, page)
    per = 60 if view == "grid" else 100
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM games g" + clause, params).fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS grader FROM games g"
            " LEFT JOIN users u ON u.id = g.graded_by" + clause +
            " ORDER BY " + order + " LIMIT ? OFFSET ?", (*params, per, (page - 1) * per))]
        sections = [r["section"] for r in conn.execute(
            "SELECT DISTINCT section FROM games WHERE section IS NOT NULL ORDER BY section")
            if r["section"]]

    if partial:
        # Only the repeatable rows, for the infinite scroller to append.
        return templates.TemplateResponse(
            "_library_items.html",
            {"request": request, "games": rows, "view": view})

    return render(request, "library.html", user=user, games=rows, total=total, page=page,
                  pages=max(1, (total + per - 1) // per), sections=sections, grades=GRADES,
                  view=view,
                  f={"q": q, "verified": verified, "grade": grade, "keep": keep,
                     "section": section, "status": status, "sort": sort, "view": view})


@app.get("/recent", response_class=HTMLResponse)
def recent(request: Request, n: int = 50, user=Depends(require_user)):
    n = max(1, min(n, 500))
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS grader,"
            " MAX(COALESCE(g.last_updated, ''), COALESCE(g.date_added, '')) AS touched"
            " FROM games g LEFT JOIN users u ON u.id = g.graded_by"
            " WHERE g.status = 'active' ORDER BY touched DESC, g.id DESC LIMIT ?", (n,))]
    return render(request, "recent.html", user=user, games=rows, n=n,
                  fallback_date=FALLBACK_DATE)


@app.get("/game/{game_id}", response_class=HTMLResponse)
def game_detail(request: Request, game_id: int, user=Depends(require_user)):
    with db() as conn:
        row = conn.execute(
            "SELECT g.*, gu.username AS grader, vu.username AS verifier FROM games g"
            " LEFT JOIN users gu ON gu.id = g.graded_by"
            " LEFT JOIN users vu ON vu.id = g.verified_by WHERE g.id = ?", (game_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such game.")
        history = [dict(r) for r in conn.execute(
            "SELECT a.*, u.username AS display_name FROM audit a LEFT JOIN users u ON u.id = a.user_id"
            " WHERE a.game_id = ? ORDER BY a.id DESC LIMIT 30", (game_id,))]
    return render(request, "game.html", user=user, g=dict(row), history=history,
                  grades=GRADES, broken_statuses=BROKEN_STATUSES,
                  igdb=metadata.igdb_available())


@app.post("/game/{game_id}")
def game_update(request: Request, game_id: int, title: str = Form(...),
                verified: str = Form(""), grade: str = Form(""), playtime: str = Form(""),
                keep_flag: str = Form(""), notes: str = Form(""),
                broken_status: str = Form(""), version: str = Form(""),
                section: str = Form(""), date_added: str = Form(""),
                steam_appid: str = Form(""), cover_url: str = Form(""),
                store_url: str = Form(""), user=Depends(require_user)):
    grade = (grade or "").strip().upper()
    if grade and grade not in GRADES:
        raise HTTPException(400, "Unknown grade.")
    if broken_status and broken_status not in BROKEN_STATUSES:
        raise HTTPException(400, "Unknown broken status.")
    try:
        minutes = int(playtime) if str(playtime).strip() else None
    except ValueError:
        minutes = None
    try:
        appid = int(steam_appid) if str(steam_appid).strip() else None
    except ValueError:
        appid = None

    is_verified = 1 if verified == "1" else 0
    is_broken = 1 if broken_status and broken_status != "fixed_recheck" else 0
    if broken_status == "fixed_recheck":
        is_verified = 0
        broken_status = ""

    if appid and not store_url.strip():
        store_url = metadata.steam_store_url(appid)

    with db() as conn:
        conn.execute(
            "UPDATE games SET title = ?, title_norm = ?, section = ?, date_added = ?,"
            " verified = ?, grade = ?, playtime_minutes = ?, keep_flag = ?, notes = ?,"
            " broken = ?, broken_status = ?, version = ?, steam_appid = ?, cover_url = ?,"
            " store_url = ?, last_updated = ?, updated_at = ? WHERE id = ?",
            (title.strip(), norm_title(title), section.strip() or None,
             date_added.strip() or FALLBACK_DATE, is_verified, grade or None, minutes,
             keep_flag or None, notes.strip() or None, is_broken, broken_status or None,
             version.strip() or None, appid, cover_url.strip() or None,
             store_url.strip() or None, today(), now(), game_id))
        log_audit(conn, game_id, user["id"], "edited", "")
    exporter.export(tag="edit")
    return RedirectResponse("/game/%d" % game_id, status_code=303)


@app.post("/game/{game_id}/fetch")
def game_fetch(request: Request, game_id: int, user=Depends(require_user)):
    with db() as conn:
        row = conn.execute("SELECT title, steam_appid FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such game.")
    meta = metadata.lookup(row["title"], row["steam_appid"])
    if not meta:
        # Nothing confident enough to write - offer the search results instead.
        return RedirectResponse("/game/%d/match" % game_id, status_code=303)
    with db() as conn:
        metadata.apply(conn, game_id, meta)
        log_audit(conn, game_id, user["id"], "metadata", meta.get("meta_source", "igdb"))
    return RedirectResponse("/game/%d" % game_id, status_code=303)


@app.get("/game/{game_id}/match", response_class=HTMLResponse)
def game_match(request: Request, game_id: int, q: str = "", user=Depends(require_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such game.")
    term = q.strip() or row["title"]
    return render(request, "match.html", user=user, g=dict(row), term=term,
                  results=metadata.suggest(term, 12), igdb=metadata.igdb_available())


@app.post("/game/{game_id}/match")
def game_match_pick(request: Request, game_id: int, igdb_id: int = Form(...),
                    term: str = Form(""), retitle: str = Form(""),
                    user=Depends(require_user)):
    chosen = next((r for r in metadata.suggest(term, 12) if r.get("igdb_id") == igdb_id), None)
    if not chosen:
        raise HTTPException(400, "That result is no longer in the list.")
    with db() as conn:
        metadata.apply(conn, game_id, chosen, overwrite_title=(retitle == "1"))
        log_audit(conn, game_id, user["id"], "metadata", "picked by hand")
    return RedirectResponse("/game/%d" % game_id, status_code=303)


def _back_to(request: Request, fallback: str) -> str:
    """Send the user back where they came from, not to a fixed page."""
    ref = request.headers.get("referer") or ""
    return ref if ref.startswith("http") else fallback


@app.post("/game/{game_id}/slate")
def game_slate(request: Request, game_id: int, user=Depends(require_user)):
    """Queue for the next batch deletion. The game is still on the server."""
    with db() as conn:
        conn.execute("UPDATE games SET slated_at = ?, updated_at = ? WHERE id = ?",
                     (now(), now(), game_id))
        log_audit(conn, game_id, user["id"], "slated", "queued for removal")
    return RedirectResponse(_back_to(request, "/removal"), status_code=303)


@app.post("/game/{game_id}/unslate")
def game_unslate(request: Request, game_id: int, user=Depends(require_user)):
    with db() as conn:
        conn.execute("UPDATE games SET slated_at = NULL, updated_at = ? WHERE id = ?",
                     (now(), game_id))
        log_audit(conn, game_id, user["id"], "unslated", "taken off the removal list")
    return RedirectResponse(_back_to(request, "/removal"), status_code=303)


@app.post("/removal/commit")
def removal_commit(request: Request, user=Depends(require_user)):
    """Everything slated is now actually gone from the server."""
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title FROM games WHERE slated_at IS NOT NULL AND status = 'active'")]
        for row in rows:
            conn.execute(
                "UPDATE games SET status = 'removed', removed_at = ?, slated_at = NULL,"
                " updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
            log_audit(conn, row["id"], user["id"], "removed", "batch")
    if rows:
        exporter.export(tag="removed-batch")
    return RedirectResponse("/removal", status_code=303)


@app.post("/game/{game_id}/restore")
def game_restore(request: Request, game_id: int, user=Depends(require_user)):
    with db() as conn:
        conn.execute("UPDATE games SET status = 'active', removed_at = NULL, slated_at = NULL,"
                     " updated_at = ? WHERE id = ?", (now(), game_id))
        log_audit(conn, game_id, user["id"], "restored", "")
    exporter.export(tag="restore")
    return RedirectResponse("/game/%d" % game_id, status_code=303)


@app.post("/game/{game_id}/delete")
def game_delete(request: Request, game_id: int, confirm: str = Form(""),
                user=Depends(require_user)):
    """Erase the row outright. For mistakes and test data, not for culling."""
    if confirm != "delete":
        return RedirectResponse("/game/%d" % game_id, status_code=303)
    with db() as conn:
        conn.execute("DELETE FROM queue_slots WHERE game_id = ?", (game_id,))
        conn.execute("DELETE FROM audit WHERE game_id = ?", (game_id,))
        conn.execute("UPDATE slot_events SET game_id = NULL WHERE game_id = ?", (game_id,))
        conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
    exporter.export(tag="delete")
    return RedirectResponse("/library", status_code=303)


# ------------------------------------------------------------------------- broken

@app.get("/broken", response_class=HTMLResponse)
def broken_list(request: Request, show: str = "open", user=Depends(require_user)):
    clause = "broken = 1 AND status = 'active'"
    if show == "open":
        clause += " AND COALESCE(broken_status, '') != 'unfixable'"
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS verifier FROM games g"
            " LEFT JOIN users u ON u.id = g.verified_by"
            " WHERE " + clause + " ORDER BY g.broken_status, g.title COLLATE NOCASE")]
    return render(request, "broken.html", user=user, games=rows, show=show,
                  broken_statuses=BROKEN_STATUSES)


@app.post("/broken/{game_id}")
def broken_update(request: Request, game_id: int, broken_status: str = Form(...),
                  notes: str = Form(""), user=Depends(require_user)):
    if broken_status not in BROKEN_STATUSES:
        raise HTTPException(400, "Unknown status.")
    with db() as conn:
        game = conn.execute("SELECT notes FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game.")
        if broken_status == "fixed_recheck":
            conn.execute(
                "UPDATE games SET broken = 0, broken_status = NULL, notes = ?, verified = 0,"
                " verified_by = NULL, verified_at = NULL, last_updated = ?, updated_at = ?"
                " WHERE id = ?", (notes.strip() or game["notes"], today(), now(), game_id))
            log_audit(conn, game_id, user["id"], "fixed", "back to verify")
        else:
            conn.execute(
                "UPDATE games SET broken_status = ?, notes = ?, last_updated = ?, updated_at = ?"
                " WHERE id = ?", (broken_status, notes.strip() or game["notes"],
                                  today(), now(), game_id))
            log_audit(conn, game_id, user["id"], "broken", broken_status)
    exporter.export(tag="broken")
    return RedirectResponse("/broken", status_code=303)


# ------------------------------------------------------------------------ removal

@app.get("/removal", response_class=HTMLResponse)
def removal(request: Request, user=Depends(require_user)):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS grader FROM games g"
            " LEFT JOIN users u ON u.id = g.graded_by WHERE " + REMOVAL_SQL_G +
            " AND g.slated_at IS NULL"
            " ORDER BY CASE WHEN g.broken_status = 'unfixable' THEN 0"
            " WHEN g.keep_flag = 'remove' THEN 1 WHEN g.grade = 'D' THEN 2 ELSE 3 END,"
            " COALESCE(g.playtime_minutes, 0) ASC, g.title COLLATE NOCASE")]
        slated = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS grader FROM games g"
            " LEFT JOIN users u ON u.id = g.graded_by"
            " WHERE g.slated_at IS NOT NULL AND g.status = 'active'"
            " ORDER BY g.slated_at, g.title COLLATE NOCASE")]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'").fetchone()["n"]
    return render(request, "removal.html", user=user, games=rows, slated=slated, total=total)


# ----------------------------------------------------------------------- add/paste

@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request, user=Depends(require_user)):
    return render(request, "add.html", user=user, parsed=None, result=None)


@app.post("/add/preview", response_class=HTMLResponse)
def add_preview(request: Request, text: str = Form(...), user=Depends(require_user)):
    items = paste.parse(text)
    with db() as conn:
        for i, item in enumerate(items):
            item["i"] = i
            item["match"] = paste.match(conn, item["title"])
    return render(request, "add.html", user=user, parsed=items, result=None, raw=text)


@app.post("/add/confirm")
async def add_confirm(request: Request, text: str = Form(...), fetch: str = Form(""),
                      user=Depends(require_user)):
    items = paste.parse(text)
    added, updated, skipped = [], [], []
    form = await request.form()

    def link_to(conn, game_id, item, retitle=False):
        """Attach the pasted link to a row that already exists. No slot spent."""
        sets = ["store_url = ?", "updated_at = ?"]
        params = [metadata.steam_store_url(item["steam_appid"]) if item["steam_appid"]
                  else (item["url"] or None), now()]
        if item["steam_appid"]:
            sets.insert(0, "steam_appid = ?")
            params.insert(0, item["steam_appid"])
        if retitle:
            sets.insert(0, "title = ?")
            sets.insert(1, "title_norm = ?")
            params.insert(0, item["title"])
            params.insert(1, norm_title(item["title"]))
        params.append(game_id)
        conn.execute("UPDATE games SET " + ", ".join(sets) + " WHERE id = ?", params)
        updated.append({"id": game_id, "title": item["title"], "appid": item["steam_appid"]})

    with db() as conn:
        for i, item in enumerate(items):
            found = paste.match(conn, item["title"])
            decision = str(form.get("decision_%d" % i, "")).strip()

            if found["kind"] == "exact":
                if item["steam_appid"] or item["url"]:
                    link_to(conn, found["game"]["id"], item)
                else:
                    skipped.append(item["title"])
                continue

            if found["kind"] == "near":
                # Nothing happens to a near match unless it was confirmed on
                # the preview screen.
                if decision.startswith("link:"):
                    gid = int(decision.split(":", 1)[1])
                    link_to(conn, gid, item, retitle=(form.get("retitle_%d" % i) == "1"))
                    continue
                if decision != "new":
                    skipped.append(item["title"])
                    continue

            appid = item["steam_appid"]
            cur = conn.execute(
                "INSERT INTO games (title, title_norm, section, date_added, verified,"
                " steam_appid, store_url, cover_url, created_at, updated_at)"
                " VALUES (?, ?, 'Recently Added', ?, 0, ?, ?, ?, ?, ?)",
                (item["title"], norm_title(item["title"]), today(), appid,
                 metadata.steam_store_url(appid) if appid else (item["url"] or None),
                 None, now(), now()))
            slots.spend(conn, cur.lastrowid, user["id"], "added")
            log_audit(conn, cur.lastrowid, user["id"], "added", "from paste")
            added.append({"id": cur.lastrowid, "title": item["title"], "appid": appid})

    if fetch == "1":
        for entry in added + updated:
            meta = metadata.lookup(entry["title"], entry["appid"])
            if meta:
                with db() as conn:
                    metadata.apply(conn, entry["id"], meta)

    exporter.export(tag="add")
    return render(request, "add.html", user=user, parsed=None,
                  result={"added": added, "updated": updated, "skipped": skipped})


# ----------------------------------------------------------------------- wishlist

@app.get("/wishlist", response_class=HTMLResponse)
def wishlist(request: Request, user=Depends(require_user)):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT w.*, u.username AS display_name FROM wishlist w LEFT JOIN users u ON u.id = w.added_by"
            " ORDER BY w.created_at DESC")]
    return render(request, "wishlist.html", user=user, items=rows)


@app.post("/wishlist")
def wishlist_add(request: Request, text: str = Form(...), user=Depends(require_user)):
    with db() as conn:
        for item in paste.parse(text):
            if conn.execute("SELECT 1 FROM wishlist WHERE title_norm = ?",
                            (norm_title(item["title"]),)).fetchone():
                continue
            appid = item["steam_appid"]
            conn.execute(
                "INSERT INTO wishlist (title, title_norm, steam_appid, store_url, cover_url,"
                " added_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item["title"], norm_title(item["title"]), appid,
                 metadata.steam_store_url(appid) if appid else (item["url"] or None),
                 None, user["id"], now(), now()))
    return RedirectResponse("/wishlist", status_code=303)


@app.post("/wishlist/{item_id}/promote")
def wishlist_promote(request: Request, item_id: int, user=Depends(require_user)):
    """It's on the server now: move it into the library and spend a slot."""
    with db() as conn:
        item = conn.execute("SELECT * FROM wishlist WHERE id = ?", (item_id,)).fetchone()
        if not item:
            raise HTTPException(404, "Not on the wishlist.")
        existing = conn.execute("SELECT id FROM games WHERE title_norm = ?",
                                (item["title_norm"],)).fetchone()
        if not existing:
            cur = conn.execute(
                "INSERT INTO games (title, title_norm, section, date_added, verified,"
                " steam_appid, store_url, cover_url, notes, created_at, updated_at)"
                " VALUES (?, ?, 'Recently Added', ?, 0, ?, ?, ?, ?, ?, ?)",
                (item["title"], item["title_norm"], today(), item["steam_appid"],
                 item["store_url"], item["cover_url"], item["notes"], now(), now()))
            slots.spend(conn, cur.lastrowid, user["id"], "added")
            log_audit(conn, cur.lastrowid, user["id"], "added", "from wishlist")
        conn.execute("DELETE FROM wishlist WHERE id = ?", (item_id,))
    exporter.export(tag="wishlist")
    return RedirectResponse("/wishlist", status_code=303)


@app.post("/wishlist/{item_id}/delete")
def wishlist_delete(request: Request, item_id: int, user=Depends(require_user)):
    with db() as conn:
        conn.execute("DELETE FROM wishlist WHERE id = ?", (item_id,))
    return RedirectResponse("/wishlist", status_code=303)


# -------------------------------------------------------------------------- admin

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(require_admin)):
    with db() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, username, is_admin FROM users ORDER BY username")]
        settings = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        events = [dict(r) for r in slots.recent_events(conn, 20)]
        no_cover = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'"
            " AND (cover_url IS NULL OR cover_url = '')").fetchone()["n"]
    try:
        exports = sorted(os.listdir(EXPORT_DIR), reverse=True)[:10]
    except OSError:
        exports = []
    return render(request, "admin.html", user=user, users=users, settings=settings,
                  events=events, exports=exports, no_cover=no_cover,
                  igdb=metadata.igdb_available(), job=jobs.status())


@app.post("/admin/settings")
def admin_settings(request: Request, slot_limit: str = Form(...), checks_per_slot: str = Form(...),
                   queue_size: str = Form(...), library_target: str = Form(...),
                   user=Depends(require_admin)):
    for key, value in (("slot_limit", slot_limit), ("checks_per_slot", checks_per_slot),
                       ("queue_size", queue_size), ("library_target", library_target)):
        try:
            set_setting(key, str(max(1, int(value))))
        except ValueError:
            pass
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/slots")
def admin_slots(request: Request, balance: int = Form(...), user=Depends(require_admin)):
    with db() as conn:
        slots.set_balance(conn, balance, user["id"])
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/users")
def admin_users(request: Request, username: str = Form(...),
                password: str = Form(""), is_admin: str = Form(""), user=Depends(require_admin)):
    name = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", name):
        return RedirectResponse("/admin", status_code=303)
    with db() as conn:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, is_admin, created_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET"
            " display_name = excluded.username, is_admin = excluded.is_admin",
            (name, name, hash_password(password), 1 if is_admin == "1" else 0, now()))
    return RedirectResponse("/admin", status_code=303)


# ------------------------------------------------------------------ personalise

@app.get("/account", response_class=HTMLResponse)
def account(request: Request, user=Depends(require_user), ok: str = "", err: str = ""):
    return render(request, "account.html", user=user, ok=ok, err=err,
                  has_password=bool(user.get("password_hash")))


@app.post("/account")
def account_save(request: Request, username: str = Form(""),
                 current: str = Form(""), password: str = Form(""),
                 confirm: str = Form(""), user=Depends(require_user)):
    username = (username or "").strip().lower()

    if not re.fullmatch(r"[a-z0-9._-]{2,32}", username):
        return RedirectResponse(
            "/account?err=Username+must+be+2-32+characters,+letters+numbers+dot+dash+underscore",
            status_code=303)

    # Changing a password needs the current one, so a left-open session can't
    # be used to lock the owner out.
    if password:
        if password != confirm:
            return RedirectResponse("/account?err=The+two+passwords+don%27t+match", status_code=303)
        if len(password) < 4:
            return RedirectResponse("/account?err=Password+is+too+short", status_code=303)
        if user.get("password_hash") and not verify_password(current, user["password_hash"]):
            return RedirectResponse("/account?err=Current+password+is+wrong", status_code=303)

    with db() as conn:
        clash = conn.execute("SELECT id FROM users WHERE username = ? AND id != ?",
                             (username, user["id"])).fetchone()
        if clash:
            return RedirectResponse("/account?err=That+username+is+taken", status_code=303)
        # display_name is kept in step with username so the column never goes
        # stale, but there is only one name to edit.
        if password:
            conn.execute("UPDATE users SET display_name = ?, username = ?, password_hash = ?"
                         " WHERE id = ?",
                         (username, username, hash_password(password), user["id"]))
        else:
            conn.execute("UPDATE users SET display_name = ?, username = ? WHERE id = ?",
                         (username, username, user["id"]))
    return RedirectResponse("/account?ok=Saved", status_code=303)


@app.post("/account/password/clear")
def account_password_clear(request: Request, current: str = Form(""),
                           user=Depends(require_user)):
    """Drop the password back to none, for a LAN-only setup that doesn't want one."""
    if user.get("password_hash") and not verify_password(current, user["password_hash"]):
        return RedirectResponse("/account?err=Current+password+is+wrong", status_code=303)
    with db() as conn:
        conn.execute("UPDATE users SET password_hash = '' WHERE id = ?", (user["id"],))
    return RedirectResponse("/account?ok=Password+removed", status_code=303)


@app.get("/look", response_class=HTMLResponse)
def look(request: Request, user=Depends(require_user)):
    return render(request, "look.html", user=user, themes=THEMES, accents=ACCENTS,
                  densities=DENSITIES, tile_sizes=list(TILE_SIZES),
                  customs=themes.custom_list())


@app.post("/look")
def look_save(request: Request, theme: str = Form(""), accent: str = Form(""),
              density: str = Form(""), tile_size: str = Form(""),
              motion: str = Form(""), user=Depends(require_user)):
    valid = {t[0] for t in THEMES} | {c["slug"] for c in themes.custom_list()}
    accent = (accent or "").lstrip("#").lower()
    with db() as conn:
        conn.execute(
            "UPDATE users SET theme = ?, accent = ?, density = ?, tile_size = ?, motion = ?"
            " WHERE id = ?",
            (theme if theme in valid else DEFAULT_THEME,
             accent if re.fullmatch(r"[0-9a-f]{6}", accent or "") else "",
             density if density in DENSITIES else "comfortable",
             tile_size if tile_size in TILE_SIZES else "medium",
             "off" if motion == "off" else "on",
             user["id"]))
    return RedirectResponse("/look", status_code=303)


@app.get("/look/theme", response_class=HTMLResponse)
def theme_editor(request: Request, slug: str = "", base: str = "", user=Depends(require_user)):
    """Build a theme by starting from an existing one and adjusting colours."""
    editing = themes.get_custom(slug) if slug else {}
    if editing:
        values = themes.tokens_for(slug)
        start = editing.get("based_on") or DEFAULT_THEME
        name = editing["name"]
    else:
        start = base if base in {t[0] for t in THEMES} else DEFAULT_THEME
        values = {}
        name = ""
    palette = themes.builtin_palette(start)
    fields = [{"key": key, "label": label,
               "value": values.get(key) or _hex_of(palette.get(key), key)}
              for key, label in THEME_TOKENS]
    return render(request, "theme_edit.html", user=user, editing=editing, name=name,
                  base=start, fields=fields, themes=THEMES)


def _hex_of(value: str, key: str) -> str:
    """Colour inputs only accept #rrggbb, so anything else needs a stand-in."""
    value = (value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value
    if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
        return "#" + "".join(c * 2 for c in value[1:])
    return "#888888"


@app.post("/look/theme")
async def theme_save(request: Request, name: str = Form(""), base: str = Form(""),
                     slug: str = Form(""), user=Depends(require_user)):
    form = await request.form()
    tokens = {key: str(form.get("t_" + key, "")) for key, _ in THEME_TOKENS}
    saved = themes.save_custom(name, base, tokens, user["id"], slug=slug)
    with db() as conn:
        conn.execute("UPDATE users SET theme = ? WHERE id = ?", (saved, user["id"]))
    return RedirectResponse("/look", status_code=303)


@app.post("/look/theme/{slug}/delete")
def theme_delete(request: Request, slug: str, user=Depends(require_user)):
    themes.delete_custom(slug)
    return RedirectResponse("/look", status_code=303)


@app.post("/admin/igdb-test", response_class=HTMLResponse)
def admin_igdb_test(request: Request, user=Depends(require_admin)):
    return render(request, "covers_result.html", user=user, tried=0, found=0,
                  igdb_test=metadata.test_connection())


@app.post("/admin/import")
async def admin_import(request: Request, file: UploadFile = File(...),
                       replace: str = Form(""), user=Depends(require_admin)):
    text = (await file.read()).decode("utf-8-sig", errors="replace")
    try:
        result = importer.import_csv(text, user["id"], replace=(replace == "1"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    exporter.export(tag="import")
    return render(request, "import_result.html", user=user, result=result)


@app.post("/admin/covers")
def admin_covers(request: Request, scope: str = Form("missing"),
                 retitle: str = Form(""), user=Depends(require_admin)):
    """Walk the whole library in the background rather than in batches."""
    jobs.start_art_fetch(user["id"], only_missing=(scope != "all"),
                         overwrite_title=(retitle == "1"))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/covers/stop")
def admin_covers_stop(request: Request, user=Depends(require_admin)):
    jobs.cancel()
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/job")
def admin_job(request: Request, user=Depends(require_user)):
    return jobs.status()


@app.get("/needs-art", response_class=HTMLResponse)
def needs_art(request: Request, page: int = 1, user=Depends(require_user)):
    """The worklist left over after a run: everything still without art."""
    per, page = 100, max(1, page)
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'"
            " AND (cover_url IS NULL OR cover_url = '')").fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM games WHERE status = 'active'"
            " AND (cover_url IS NULL OR cover_url = '')"
            " ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?", (per, (page - 1) * per))]
    return render(request, "needs_art.html", user=user, games=rows, total=total,
                  page=page, pages=max(1, (total + per - 1) // per),
                  igdb=metadata.igdb_available())


@app.post("/admin/export")
def admin_export(request: Request, user=Depends(require_admin)):
    exporter.export(tag="manual")
    return RedirectResponse("/admin", status_code=303)


@app.get("/admin/export/{name}")
def admin_download(request: Request, name: str, user=Depends(require_admin)):
    if "/" in name or "\\" in name or not name.endswith(".csv"):
        raise HTTPException(400, "Bad filename.")
    path = os.path.join(EXPORT_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404, "No such export.")
    return FileResponse(path, filename=name, media_type="text/csv")


# --------------------------------------------------------------------- text export

@app.get("/export.txt", response_class=PlainTextResponse)
def export_text(request: Request, fmt: str = "markdown", scope: str = "recent",
                n: int = 50, section: str = "", user=Depends(require_user)):
    n = max(1, min(n, 3000))
    if scope == "recent":
        sql = ("SELECT * FROM games WHERE status = 'active'"
               " ORDER BY MAX(COALESCE(last_updated, ''), COALESCE(date_added, '')) DESC,"
               " id DESC LIMIT ?")
        params = (n,)
    elif scope == "section" and section:
        sql = ("SELECT * FROM games WHERE status = 'active' AND section = ?"
               " ORDER BY title COLLATE NOCASE LIMIT ?")
        params = (section, n)
    elif scope == "slated":
        sql = ("SELECT * FROM games WHERE slated_at IS NOT NULL AND status = 'active'"
               " ORDER BY title COLLATE NOCASE LIMIT ?")
        params = (n,)
    elif scope == "removal":
        sql = "SELECT * FROM games WHERE " + REMOVAL_SQL + " ORDER BY title COLLATE NOCASE LIMIT ?"
        params = (n,)
    else:
        sql = ("SELECT * FROM games WHERE status = 'active'"
               " ORDER BY title COLLATE NOCASE LIMIT ?")
        params = (n,)

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return exporter.plain_text(rows, fmt)


@app.get("/healthz")
def healthz():
    return {"ok": True}
