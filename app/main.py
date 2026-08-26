"""GameRank - verification, grading and slot tracking for a game library."""
import io
import json
import os
import re
import time
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request, Form, UploadFile, File, Depends, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeSerializer, BadSignature

from . import slots, queues, exporter, importer, metadata, paste, jobs, themes, grades
from .db import (
    db, init_db, now, today, norm_title, sort_title, log_audit, get_setting_conn, set_setting,
    hash_password, verify_password, GRADES, REPACKS, REPACK_KEYS,
    EXPORT_DIR, FALLBACK_DATE, DATA_DIR,
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
RAIL_LETTERS = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + ["#"]


def holds_credit(verified, broken, status: str = "active") -> bool:
    """Whether a game has been paid for the check it was given.

    A check is paid once the game is settled - it works, or it is gone. Broken
    and still on the server is the one state that owes nothing, which is the
    point: a broken game that paid its way would never get dealt with. Both
    ways out of that state settle it, so the withholding is pressure to resolve
    it rather than a fine for the game turning out bad.
    """
    if not verified:
        return False
    if status == "removed":
        return True
    return not broken


def settle_credit(conn, game_id, user_id, before: bool, after: bool) -> None:
    """Pay or take back a check's credit when a game crosses that line.

    Not used by the Verify queue, which credits its own submissions, nor by
    marking a game updated, which is priced at a whole slot rather than a
    check and would otherwise be charged for the same move twice.
    """
    if after and not before:
        slots.credit_check(conn, game_id, user_id)
    elif before and not after:
        slots.debit_check(conn, game_id, user_id)


def section_names(conn) -> list:
    """The category list, in the order the admin page put it."""
    return [r["name"] for r in conn.execute(
        "SELECT name FROM sections ORDER BY position, name")]


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


# --------------------------------------------------------------------- guests

# The viewing account exists so people on the game server can look up a grade
# without needing an account each. It is shared, so it gets no write of any
# kind: this is the single choke point, and a route added later is refused by
# default rather than having to be remembered and guarded.
GUEST_PAGES = {"/", "/library", "/recent", "/export.txt", "/logout", "/healthz",
               "/favicon.ico", "/site.webmanifest"}


def guest_may_see(method: str, path: str) -> bool:
    if method not in ("GET", "HEAD"):
        return False
    if path in GUEST_PAGES or path.startswith("/cover/"):
        return True
    return bool(re.fullmatch(r"/game/\d+", path))


@app.middleware("http")
async def _guest_is_read_only(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/static/"):
        user = current_user(request)
        if user and user["is_guest"] and not guest_may_see(request.method, path):
            return HTMLResponse(
                "<h1>Viewing only</h1><p>This account can browse the library but"
                " cannot change anything.</p><p><a href='/'>Back</a></p>",
                status_code=403)
    return await call_next(request)


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
            "SELECT COUNT(*) AS n FROM games WHERE broken = 1"
            " AND status = 'active'").fetchone()["n"]
        ctx["no_art_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'"
            " AND (cover_url IS NULL OR cover_url = '')").fetchone()["n"]
        ctx["target"] = int(get_setting_conn(conn, "library_target", "2000"))
    return templates.TemplateResponse(template, {"request": request, "user": user, **ctx})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        "login.html", {"request": request, "user": None,
                       "error": None, "asset_v": ASSET_V})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form("")):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username.strip().lower(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"] or ""):
        # Deliberately the same message either way, so the page cannot be used
        # to find out which accounts exist.
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "user": None,
             "error": "That username and password do not match.", "asset_v": ASSET_V},
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
        # Your own grades. A library-wide tally would count the other
        # person's letters, which is the thing being kept back.
        grade_dist = {r["grade"]: r["n"] for r in conn.execute(
            "SELECT gg.grade, COUNT(*) AS n FROM game_grades gg"
            " JOIN games g ON g.id = gg.game_id"
            " WHERE gg.user_id = ? AND gg.grade IS NOT NULL AND g.status = 'active'"
            " GROUP BY gg.grade", (user["id"],))}
        ungraded = conn.execute(
            "SELECT COUNT(*) AS n FROM games g WHERE g.verified = 1 AND g.status = 'active'"
            " AND NOT EXISTS (SELECT 1 FROM game_grades gg"
            " WHERE gg.game_id = g.id AND gg.user_id = ?)", (user["id"],)).fetchone()["n"]
        slated_total = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'"
            " AND slated_at IS NOT NULL").fetchone()["n"]
        removed_total = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'removed'").fetchone()["n"]
        wishlist = conn.execute("SELECT COUNT(*) AS n FROM wishlist").fetchone()["n"]
        recent = [dict(r) for r in conn.execute(
            "SELECT *, date_added AS touched FROM games WHERE status = 'active'"
            " ORDER BY COALESCE(date_added, '') DESC, id DESC LIMIT 12")]
        grades.attach(conn, recent, user)
    return render(request, "dashboard.html", user=user, grade_dist=grade_dist,
                  ungraded=ungraded, slated_total=slated_total, removed_total=removed_total,
                  wishlist=wishlist, recent=recent, grades=GRADES)


# ------------------------------------------------------------------------- verify

@app.get("/verify", response_class=HTMLResponse)
def verify_queue(request: Request, user=Depends(require_user)):
    with db() as conn:
        games = [dict(r) for r in queues.slate(conn, user["id"], queues.VERIFY)]
        remaining = queues.pool_count(conn, queues.VERIFY, user["id"])
    return render(request, "verify.html", user=user, games=games, remaining=remaining,
                  repacks=REPACKS)


@app.post("/verify/{game_id}")
def verify_submit(request: Request, game_id: int, works: str = Form(...),
                  notes: str = Form(""), repack: str = Form(""),
                  user=Depends(require_user)):
    ok = works == "yes"
    repack = repack if repack in REPACK_KEYS else None
    with db() as conn:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game.")
        if game["verified"]:
            queues.clear_slot(conn, user["id"], queues.VERIFY, game_id)
            return RedirectResponse("/verify", status_code=303)

        conn.execute(
            "UPDATE games SET verified = 1, verified_by = ?, verified_at = ?,"
            " broken = ?, notes = ?, repack = ?, updated_at = ? WHERE id = ?",
            (user["id"], now(), 0 if ok else 1,
             notes.strip() or game["notes"], repack, now(), game_id))
        if ok:
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
        remaining = queues.pool_count(conn, queues.GRADE, user["id"])
    return render(request, "grade.html", user=user, games=games,
                  remaining=remaining, grades=GRADES)


@app.post("/grade/{game_id}")
def grade_submit(request: Request, game_id: int, grade: str = Form(""),
                 playtime: str = Form(""), notes: str = Form(""),
                 user=Depends(require_user)):
    grade = (grade or "").strip().upper()
    if grade and grade not in GRADES:
        raise HTTPException(400, "Unknown grade.")
    try:
        minutes = int(playtime) if str(playtime).strip() else None
    except ValueError:
        minutes = None

    if not grade:
        # A card with no letter on it is a slip or a page left open since the
        # game was graded somewhere else. Either way there is nothing to
        # record, and treating it as "clear the grade" threw away real work.
        # Removing a grade on purpose is done on the game's own page.
        return RedirectResponse("/grade", status_code=303)

    with db() as conn:
        game = conn.execute("SELECT notes FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game.")
        if notes.strip():
            conn.execute("UPDATE games SET notes = ?, updated_at = ? WHERE id = ?",
                         (notes.strip(), now(), game_id))
        grades.set_grade(conn, game_id, user["id"], grade, minutes)
        log_audit(conn, game_id, user["id"], "graded", grade)
        queues.clear_slot(conn, user["id"], queues.GRADE, game_id)
        queues.refill(conn, user["id"], queues.GRADE)
    exporter.export(tag="grade")
    return RedirectResponse("/grade", status_code=303)


# ------------------------------------------------------------------------ library

@app.get("/library", response_class=HTMLResponse)
def library(request: Request, q: str = "", verified: str = "", grade: str = "",
            section: str = "", status: str = "active",
            sort: str = "title", view: str = "grid", page: int = 1,
            letter: str = "", repack: str = "", partial: int = 0,
            user=Depends(require_user)):
    current = {"q": q, "verified": verified, "grade": grade,
               "section": section, "status": status, "sort": sort, "view": view,
               "letter": letter, "repack": repack}

    # A bare visit - from the nav or a breadcrumb - resumes the last filters.
    # It has to redirect rather than just apply them, because the scroller
    # builds its requests from the address bar.
    if not partial and not request.query_params:
        try:
            saved = json.loads(user.get("library_filters") or "{}")
        except (ValueError, TypeError):
            saved = {}
        saved = {k: v for k, v in saved.items() if k in current and v}
        if saved and saved != {"status": "active", "sort": "title", "view": "grid"}:
            return RedirectResponse("/library?" + urlencode(saved), status_code=303)

    where, params = [], []
    if q.strip():
        where.append("g.title_norm LIKE ?")
        params.append("%" + norm_title(q) + "%")
    if verified in ("0", "1"):
        where.append("g.verified = ?")
        params.append(int(verified))
    # Grade filters read your own row. Filtering on the rolled-up column would
    # hand back the other person's letters a search at a time.
    if grade == "none":
        where.append("NOT EXISTS (SELECT 1 FROM game_grades gg WHERE gg.game_id = g.id"
                     " AND gg.user_id = ? AND gg.grade IS NOT NULL)")
        params.append(user["id"])
    elif grade in GRADES:
        where.append("EXISTS (SELECT 1 FROM game_grades gg WHERE gg.game_id = g.id"
                     " AND gg.user_id = ? AND gg.grade = ?)")
        params.extend([user["id"], grade])
    if section:
        where.append("g.section = ?")
        params.append(section)
    if repack in REPACK_KEYS:
        where.append("g.repack = ?")
        params.append(repack)
    if status == "slated":
        where.append("g.status = 'active' AND g.slated_at IS NOT NULL")
    elif status in ("active", "removed"):
        where.append("g.status = ?")
        params.append(status)

    sorts = {
        "title": "g.title_sort",
        "added": "COALESCE(g.date_added, '') DESC, g.id DESC",
        "updated": "g.last_updated DESC",
        "grade": "CASE (SELECT gg.grade FROM game_grades gg WHERE gg.game_id = g.id"
                 " AND gg.user_id = %d) WHEN 'S' THEN 0 WHEN 'A' THEN 1 WHEN 'B' THEN 2"
                 " WHEN 'C' THEN 3 WHEN 'D' THEN 4 ELSE 5 END, g.title_sort"
                 % int(user["id"]),
        "playtime": "COALESCE(g.playtime_minutes, -1) ASC, g.title_sort",
    }
    order = sorts.get(sort, sorts["title"])

    # The A-Z rail counts against everything else being filtered, so a letter
    # with nothing under the current filters can be shown as unavailable.
    base_clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        rail = {r["bucket"]: r["n"] for r in conn.execute(
            "SELECT CASE WHEN substr(g.title_sort, 1, 1) BETWEEN 'a' AND 'z'"
            " THEN upper(substr(g.title_sort, 1, 1)) ELSE '#' END AS bucket,"
            " COUNT(*) AS n FROM games g" + base_clause + " GROUP BY bucket", params)}

    letter = (letter or "").strip().upper()[:1]
    if letter == "#":
        where.append("(g.title_sort IS NULL OR g.title_sort = ''"
                     " OR substr(g.title_sort, 1, 1) NOT BETWEEN 'a' AND 'z')")
    elif "A" <= letter <= "Z":
        where.append("substr(g.title_sort, 1, 1) = ?")
        params.append(letter.lower())
    else:
        letter = ""

    clause = (" WHERE " + " AND ".join(where)) if where else ""

    page = max(1, page)
    per = 60 if view == "grid" else 100
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM games g" + clause, params).fetchone()["n"]
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS grader FROM games g"
            " LEFT JOIN users u ON u.id = g.graded_by" + clause +
            " ORDER BY " + order + " LIMIT ? OFFSET ?", (*params, per, (page - 1) * per))]
        sections = section_names(conn)
        grades.attach(conn, rows, user)

    if partial:
        # Only the repeatable rows, for the infinite scroller to append.
        return templates.TemplateResponse(
            "_library_items.html",
            {"request": request, "games": rows, "view": view, "user": user,
             "show_removed": status == "removed"})

    # Remember what was being looked at, so coming back from a game page or the
    # nav resumes it rather than resetting to everything.
    with db() as conn:
        conn.execute("UPDATE users SET library_filters = ? WHERE id = ?",
                     (json.dumps(current), user["id"]))

    return render(request, "library.html", user=user, games=rows, total=total, page=page,
                  pages=max(1, (total + per - 1) // per), sections=sections, grades=GRADES,
                  view=view, rail_counts=rail, letters=RAIL_LETTERS, repacks=REPACKS,
                  show_removed=status == "removed",
                  f={"q": q, "verified": verified, "grade": grade,
                     "section": section, "status": status, "sort": sort, "view": view,
                     "letter": letter,
                     "repack": repack if repack in REPACK_KEYS else ""})


@app.get("/recent", response_class=HTMLResponse)
def recent(request: Request, n: int = 50, user=Depends(require_user)):
    n = max(1, min(n, 500))
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS grader, g.date_added AS touched"
            " FROM games g LEFT JOIN users u ON u.id = g.graded_by"
            " WHERE g.status = 'active'"
            " ORDER BY COALESCE(g.date_added, '') DESC, g.id DESC LIMIT ?", (n,))]
        grades.attach(conn, rows, user)
    return render(request, "recent.html", user=user, games=rows, n=n,
                  fallback_date=FALLBACK_DATE)


@app.get("/game/{game_id}", response_class=HTMLResponse)
def game_detail(request: Request, game_id: int, err: str = "", user=Depends(require_user)):
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
        panels = grades.panels(conn, game_id, user)
        mine = grades.for_game(conn, game_id).get(user["id"])
        sections = section_names(conn)
    return render(request, "game.html", user=user, g=dict(row), history=history, err=err,
                  grades=GRADES, panels=panels, repacks=REPACKS,
                  mine=dict(mine) if mine else None, sections=sections,
                  igdb=metadata.igdb_available())


@app.post("/game/{game_id}")
def game_update(request: Request, game_id: int, title: str = Form(...),
                verified: str = Form(""), grade: str = Form(""), playtime: str = Form(""),
                notes: str = Form(""),
                works: str = Form("yes"), version: str = Form(""),
                repack: str = Form(""), removed_at: str = Form(""),
                last_updated: str = Form(""),
                section: str = Form(""), date_added: str = Form(""),
                steam_appid: str = Form(""), cover_url: str = Form(""),
                store_url: str = Form(""), user=Depends(require_user)):
    grade = (grade or "").strip().upper()
    if grade and grade not in GRADES:
        raise HTTPException(400, "Unknown grade.")
    try:
        minutes = int(playtime) if str(playtime).strip() else None
    except ValueError:
        minutes = None
    try:
        appid = int(steam_appid) if str(steam_appid).strip() else None
    except ValueError:
        appid = None

    if appid and not store_url.strip():
        store_url = metadata.steam_store_url(appid)

    with db() as conn:
        was = conn.execute("SELECT verified, broken, status, removed_at, last_updated"
                           " FROM games WHERE id = ?", (game_id,)).fetchone()
        if not was:
            raise HTTPException(404, "No such game.")

        # Only a game that is actually gone carries a removal date, and the
        # archived back-catalogue needs its real one rather than the day it
        # was typed in.
        gone_on = was["removed_at"]
        if was["status"] == "removed":
            gone_on = removed_at.strip() or was["removed_at"]

        if user["is_admin"]:
            is_verified = 1 if verified == "1" else 0
            # Nothing unchecked can be known to be broken.
            is_broken = 1 if (is_verified and works == "no") else 0
        else:
            # Checking a game off outside the queue is an admin privilege.
            is_verified, is_broken = was["verified"], was["broken"]

        conn.execute(
            "UPDATE games SET title = ?, title_norm = ?, title_sort = ?, section = ?,"
            " date_added = ?, last_updated = ?, verified = ?, notes = ?, broken = ?, repack = ?,"
            " version = ?, steam_appid = ?, cover_url = ?, store_url = ?, removed_at = ?,"
            " updated_at = ? WHERE id = ?",
            (title.strip(), norm_title(title), sort_title(title), section.strip() or None,
             date_added.strip() or FALLBACK_DATE,
             # Saving the page is not an update, so this only moves when the
             # field itself is changed.
             last_updated.strip() or None, is_verified, notes.strip() or None,
             is_broken, repack if repack in REPACK_KEYS else None,
             version.strip() or None, appid,
             cover_url.strip() or None, store_url.strip() or None, gone_on,
             now(), game_id))
        grades.set_grade(conn, game_id, user["id"], grade or None, minutes)
        settle_credit(conn, game_id, user["id"],
                      holds_credit(was["verified"], was["broken"]),
                      holds_credit(is_verified, is_broken))
        log_audit(conn, game_id, user["id"], "edited", "")
    exporter.export(tag="edit")
    return RedirectResponse("/game/%d" % game_id, status_code=303)


COVER_DIR = os.path.join(DATA_DIR, "covers")
COVER_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif",
               "image/bmp", "image/tiff"}
COVER_MAX = 16 * 1024 * 1024
# Portrait boxart, matching what IGDB serves, so uploads sit in the grid at the
# same size as everything else.
COVER_W, COVER_H = 600, 900


def _to_cover(data: bytes) -> bytes:
    """Normalise an upload to one 600x900 JPEG.

    Stretched or squashed to the frame, never cropped and never padded. An odd
    shape comes out distorted, which is the trade: losing part of the art is
    worse than the whole of it being slightly off.
    """
    from PIL import Image, ImageOps

    src = Image.open(io.BytesIO(data))
    src = ImageOps.exif_transpose(src)
    if src.mode not in ("RGB", "L"):
        src = src.convert("RGB")

    out = src.resize((COVER_W, COVER_H), Image.LANCZOS)
    buf = io.BytesIO()
    out.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


@app.post("/game/{game_id}/cover")
async def game_cover(request: Request, game_id: int, file: UploadFile = File(...),
                     user=Depends(require_user)):
    """Upload a cover from disk, for the games IGDB will never have."""
    if (file.content_type or "").lower() not in COVER_TYPES:
        return RedirectResponse("/game/%d?err=Not+an+image" % game_id, status_code=303)
    data = await file.read(COVER_MAX + 1)
    if len(data) > COVER_MAX:
        return RedirectResponse("/game/%d?err=Image+is+over+16MB" % game_id, status_code=303)

    try:
        data = _to_cover(data)
    except Exception:
        return RedirectResponse("/game/%d?err=That+image+could+not+be+read" % game_id,
                                status_code=303)

    os.makedirs(COVER_DIR, exist_ok=True)
    for old in os.listdir(COVER_DIR):
        if old.rsplit(".", 1)[0] == str(game_id):
            try:
                os.remove(os.path.join(COVER_DIR, old))
            except OSError:
                pass
    with open(os.path.join(COVER_DIR, "%d.jpg" % game_id), "wb") as fh:
        fh.write(data)

    # Cache-busted, and marked manual so the art job leaves it alone.
    with db() as conn:
        conn.execute("UPDATE games SET cover_url = ?, meta_source = 'manual', updated_at = ?"
                     " WHERE id = ?",
                     ("/cover/%d?v=%s" % (game_id, int(time.time())), now(), game_id))
        log_audit(conn, game_id, user["id"], "cover", "uploaded")
    return RedirectResponse("/game/%d" % game_id, status_code=303)


@app.get("/cover/{game_id}")
def cover_file(game_id: int, v: str = ""):
    try:
        names = os.listdir(COVER_DIR)
    except OSError:
        raise HTTPException(404, "No uploaded cover.")
    for name in names:
        if name.rsplit(".", 1)[0] == str(game_id):
            return FileResponse(os.path.join(COVER_DIR, name),
                                headers={"Cache-Control": "public, max-age=604800"})
    raise HTTPException(404, "No uploaded cover.")


@app.post("/game/{game_id}/cover/clear")
def game_cover_clear(request: Request, game_id: int, user=Depends(require_user)):
    """Drop the upload and let the art job manage it again."""
    try:
        for name in os.listdir(COVER_DIR):
            if name.rsplit(".", 1)[0] == str(game_id):
                os.remove(os.path.join(COVER_DIR, name))
    except OSError:
        pass
    with db() as conn:
        conn.execute("UPDATE games SET cover_url = NULL, meta_source = NULL, updated_at = ?"
                     " WHERE id = ?", (now(), game_id))
        log_audit(conn, game_id, user["id"], "cover", "removed")
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
def game_match(request: Request, game_id: int, q: str = "", back: str = "",
               user=Depends(require_user)):
    with db() as conn:
        row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not row:
            raise HTTPException(404, "No such game.")
    term = q.strip() or row["title"]
    # Remember where the user arrived from so picking a match returns them
    # there - coming from the worklist and landing on a game page means
    # navigating back for every single one.
    back = back or _referer_path(request)
    return render(request, "match.html", user=user, g=dict(row), term=term, back=back,
                  results=metadata.suggest(term, 12), igdb=metadata.igdb_available())


@app.post("/game/{game_id}/match")
def game_match_pick(request: Request, game_id: int, igdb_id: int = Form(...),
                    term: str = Form(""), retitle: str = Form(""),
                    back: str = Form(""), user=Depends(require_user)):
    chosen = next((r for r in metadata.suggest(term, 12) if r.get("igdb_id") == igdb_id), None)
    if not chosen:
        raise HTTPException(400, "That result is no longer in the list.")
    with db() as conn:
        metadata.apply(conn, game_id, chosen, overwrite_title=(retitle == "1"))
        log_audit(conn, game_id, user["id"], "metadata", "picked by hand")
    return RedirectResponse(back or "/game/%d" % game_id, status_code=303)


def _referer_path(request: Request) -> str:
    """The path part of where the request came from, or blank."""
    ref = request.headers.get("referer") or ""
    try:
        parsed = urlparse(ref)
    except ValueError:
        return ""
    # Only follow a referer from this app. Keeping just the path would be safe
    # enough, but a path borrowed from another site is meaningless here.
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return ""
    if parsed.path.startswith("/"):
        return parsed.path + (("?" + parsed.query) if parsed.query else "")
    return ""


def _back_to(request: Request, fallback: str) -> str:
    """Send the user back where they came from, not to a fixed page."""
    return _referer_path(request) or fallback


@app.post("/game/{game_id}/slate")
def game_slate(request: Request, game_id: int, user=Depends(require_user)):
    """Queue for the next batch deletion. The game is still on the server."""
    with db() as conn:
        conn.execute("UPDATE games SET slated_at = ?, updated_at = ? WHERE id = ?",
                     (now(), now(), game_id))
        log_audit(conn, game_id, user["id"], "slated", "queued for removal")
    return RedirectResponse(_back_to(request, "/removal"), status_code=303)


@app.post("/game/{game_id}/unslate")
def game_unslate(request: Request, game_id: int, user=Depends(require_admin)):
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
            "SELECT id, title, verified, broken, status FROM games"
            " WHERE slated_at IS NOT NULL AND status = 'active'")]
        for row in rows:
            conn.execute(
                "UPDATE games SET status = 'removed', removed_at = ?, slated_at = NULL,"
                " updated_at = ? WHERE id = ?", (now(), now(), row["id"]))
            settle_credit(conn, row["id"], user["id"],
                          holds_credit(row["verified"], row["broken"], row["status"]),
                          holds_credit(row["verified"], row["broken"], "removed"))
            log_audit(conn, row["id"], user["id"], "removed", "batch")
    if rows:
        exporter.export(tag="removed-batch")
    return RedirectResponse("/removal", status_code=303)


@app.post("/game/{game_id}/restore")
def game_restore(request: Request, game_id: int, user=Depends(require_user)):
    with db() as conn:
        was = conn.execute("SELECT verified, broken, status FROM games WHERE id = ?",
                           (game_id,)).fetchone()
        if not was:
            raise HTTPException(404, "No such game.")
        conn.execute("UPDATE games SET status = 'active', removed_at = NULL, slated_at = NULL,"
                     " updated_at = ? WHERE id = ?", (now(), game_id))
        settle_credit(conn, game_id, user["id"],
                      holds_credit(was["verified"], was["broken"], was["status"]),
                      holds_credit(was["verified"], was["broken"], "active"))
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
def broken_list(request: Request, user=Depends(require_user)):
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT g.*, u.username AS verifier FROM games g"
            " LEFT JOIN users u ON u.id = g.verified_by"
            " WHERE g.broken = 1 AND g.status = 'active' ORDER BY g.title_sort")]
        grades.attach(conn, rows, user)
    return render(request, "broken.html", user=user, games=rows)


@app.post("/broken/{game_id}")
def broken_action(request: Request, game_id: int, action: str = Form(...),
                  notes: str = Form(""), wishlist: str = Form(""),
                  user=Depends(require_user)):
    """Fixed, or gone.

    Removing here is the removal itself rather than a nomination: a broken game
    goes straight to the archive instead of joining the batch, which is for
    culling games that work. A replacement copy never comes back through this
    page - it is checked before it reaches the server, so it arrives as a
    working game like any other.
    """
    with db() as conn:
        game = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        if not game:
            raise HTTPException(404, "No such game.")
        if notes.strip():
            conn.execute("UPDATE games SET notes = ?, updated_at = ? WHERE id = ?",
                         (notes.strip(), now(), game_id))

        if action == "fixed":
            if not user["is_admin"]:
                raise HTTPException(403, "Only an admin can mark a game fixed.")
            conn.execute(
                "UPDATE games SET broken = 0, verified = 1, verified_by = ?, verified_at = ?,"
                " updated_at = ? WHERE id = ?", (user["id"], now(), now(), game_id))
            settle_credit(conn, game_id, user["id"],
                          holds_credit(game["verified"], game["broken"]), True)
            log_audit(conn, game_id, user["id"], "fixed", "runs again")
        elif action == "removed":
            conn.execute(
                "UPDATE games SET status = 'removed', removed_at = ?, slated_at = NULL,"
                " updated_at = ? WHERE id = ?", (now(), now(), game_id))
            settle_credit(conn, game_id, user["id"],
                          holds_credit(game["verified"], game["broken"], game["status"]),
                          holds_credit(game["verified"], game["broken"], "removed"))
            log_audit(conn, game_id, user["id"], "removed", "broken")
            if wishlist == "1" and not conn.execute(
                    "SELECT 1 FROM wishlist WHERE title_norm = ?",
                    (game["title_norm"],)).fetchone():
                conn.execute(
                    "INSERT INTO wishlist (title, title_norm, steam_appid, store_url,"
                    " cover_url, notes, added_by, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (game["title"], game["title_norm"], game["steam_appid"],
                     game["store_url"], game["cover_url"], "Broken copy removed",
                     user["id"], now(), now()))
                log_audit(conn, game_id, user["id"], "wishlisted", "want a working copy")
        else:
            raise HTTPException(400, "Unknown action.")
    exporter.export(tag="broken")
    return RedirectResponse(_back_to(request, "/broken"), status_code=303)


# ------------------------------------------------------------------------ removal

@app.get("/removal", response_class=HTMLResponse)
def removal(request: Request, q: str = "", user=Depends(require_user)):
    """The batch about to be deleted, plus a search for adding to it.

    There is no candidate list any more. Naming candidates meant naming the
    games the other person had graded C or below, which is the letter that is
    meant to stay out of sight until you have graded the game yourself.
    """
    found = []
    with db() as conn:
        slated = [dict(r) for r in conn.execute(
            "SELECT g.* FROM games g WHERE g.slated_at IS NOT NULL AND g.status = 'active'"
            " ORDER BY g.slated_at, g.title_sort")]
        if q.strip():
            found = [dict(r) for r in conn.execute(
                "SELECT g.* FROM games g WHERE g.status = 'active' AND g.slated_at IS NULL"
                " AND g.title_norm LIKE ? ORDER BY g.title_sort LIMIT 60",
                ("%" + norm_title(q) + "%",))]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM games WHERE status = 'active'").fetchone()["n"]
        grades.attach(conn, slated, user)
        grades.attach(conn, found, user)
    return render(request, "removal.html", user=user, slated=slated, found=found,
                  q=q, total=total)


# ----------------------------------------------------------------------- add/paste

ADD_MODES = ("add", "update", "archive")


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request, mode: str = "add", user=Depends(require_user)):
    return render(request, "add.html", user=user, parsed=None, result=None,
                  mode=mode if mode in ADD_MODES else "add")


@app.post("/add/preview", response_class=HTMLResponse)
def add_preview(request: Request, text: str = Form(...), mode: str = Form("add"),
                user=Depends(require_user)):
    items = paste.parse(text)
    with db() as conn:
        for i, item in enumerate(items):
            item["i"] = i
            item["match"] = paste.match(conn, item["title"])
    return render(request, "add.html", user=user, parsed=items, result=None, raw=text,
                  mode=mode)


@app.post("/add/confirm")
async def add_confirm(request: Request, text: str = Form(...), fetch: str = Form(""),
                      mode: str = Form("add"), user=Depends(require_user)):
    items = paste.parse(text)
    added, updated, refreshed, skipped = [], [], [], []
    form = await request.form()

    def mark_updated(conn, game_id, item):
        """A new build of a game already on the server.

        It costs a slot and goes back to unverified, because re-downloading and
        re-checking it is the same work as a new game - and an update is the
        most likely moment for something that used to run to stop running. The
        grade and playtime survive; those are opinions about the game, not the
        build. Any broken state is cleared, since the update may well be the fix.
        """
        conn.execute(
            "UPDATE games SET verified = 0, verified_by = NULL, verified_at = NULL,"
            " broken = 0, last_updated = ?, updated_at = ?"
            " WHERE id = ?", (today(), now(), game_id))
        slots.spend(conn, game_id, user["id"], "updated")
        log_audit(conn, game_id, user["id"], "updated", "new build, back to unverified")
        refreshed.append({"id": game_id, "title": item["title"]})

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
                gid = found["game"]["id"]
                if mode == "archive":
                    skipped.append(item["title"])
                    continue
                if item["steam_appid"] or item["url"]:
                    link_to(conn, gid, item)
                # In update mode every title already present is a new build,
                # which is the whole point of pasting that batch. In add mode
                # it is never assumed, so re-pasting a list is harmless.
                if mode == "update" or decision == "update":
                    mark_updated(conn, gid, item)
                elif not (item["steam_appid"] or item["url"]):
                    skipped.append(item["title"])
                continue

            if found["kind"] == "near":
                if mode == "archive":
                    skipped.append(item["title"])
                    continue
                # Nothing happens to a near match unless it was confirmed on
                # the preview screen.
                if decision.startswith("update:"):
                    gid = int(decision.split(":", 1)[1])
                    link_to(conn, gid, item, retitle=(form.get("retitle_%d" % i) == "1"))
                    mark_updated(conn, gid, item)
                    continue
                if decision.startswith("link:"):
                    gid = int(decision.split(":", 1)[1])
                    link_to(conn, gid, item, retitle=(form.get("retitle_%d" % i) == "1"))
                    continue
                if decision != "new":
                    skipped.append(item["title"])
                    continue

            # Update mode never creates anything: a title that is not there has
            # no build to update, and silently adding it would spend a slot on
            # something the batch was not about.
            if mode == "update":
                skipped.append(item["title"])
                continue

            appid = item["steam_appid"]
            # Archive mode records games that were gone before the app existed.
            # They arrive already removed, so there is nothing to check and no
            # slot to spend - they never joined the pile of work.
            gone = mode == "archive"
            cur = conn.execute(
                "INSERT INTO games (title, title_norm, title_sort, section, date_added, verified,"
                " status, removed_at, steam_appid, store_url, cover_url, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
                (item["title"], norm_title(item["title"]), sort_title(item["title"]),
                 None if gone else "Recently Added", today(),
                 "removed" if gone else "active", now() if gone else None, appid,
                 metadata.steam_store_url(appid) if appid else (item["url"] or None),
                 None, now(), now()))
            if gone:
                log_audit(conn, cur.lastrowid, user["id"], "archived", "already removed")
            else:
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
    return render(request, "add.html", user=user, parsed=None, mode=mode,
                  result={"added": added, "updated": updated,
                          "refreshed": refreshed, "skipped": skipped})


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
                "INSERT INTO games (title, title_norm, title_sort, section, date_added, verified,"
                " steam_appid, store_url, cover_url, notes, created_at, updated_at)"
                " VALUES (?, ?, ?, 'Recently Added', ?, 0, ?, ?, ?, ?, ?, ?)",
                (item["title"], item["title_norm"], sort_title(item["title"]),
                 today(), item["steam_appid"],
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
            "SELECT id, username, is_admin FROM users WHERE COALESCE(is_guest, 0) = 0"
            " ORDER BY username")]
        guest = conn.execute(
            "SELECT username, password_hash FROM users WHERE is_guest = 1").fetchone()
        section_rows = [dict(r) for r in conn.execute(
            "SELECT s.name, s.position,"
            " (SELECT COUNT(*) FROM games WHERE section = s.name) AS games"
            " FROM sections s ORDER BY s.position, s.name")]
        seats = {r["grade_seat"]: r["id"] for r in conn.execute(
            "SELECT id, grade_seat FROM users WHERE grade_seat IN (1, 2)")}
        checked = [dict(r) for r in conn.execute(
            "SELECT g.id, g.title, g.verified_at, g.broken, g.repack, g.status,"
            " u.username AS verifier FROM games g"
            " LEFT JOIN users u ON u.id = g.verified_by"
            " WHERE g.verified = 1 AND g.verified_at IS NOT NULL"
            " ORDER BY g.verified_at DESC, g.id DESC LIMIT 50")]
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
                  guest=dict(guest) if guest else None,
                  section_rows=section_rows, seats=seats, checked=checked,
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
    # Part-credit carried over from a wider ratio would otherwise sit there
    # unspendable once the ratio narrows.
    with db() as conn:
        per = max(1, int(get_setting_conn(conn, "checks_per_slot", "2")))
        conn.execute("UPDATE slot_state SET check_credit = MIN(check_credit, ?)"
                     " WHERE id = 1", (per - 1,))
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
            "INSERT INTO users (username, display_name, password_hash, is_admin, is_guest,"
            " created_at) VALUES (?, ?, ?, ?, 0, ?) ON CONFLICT(username) DO UPDATE SET"
            " display_name = excluded.username, is_admin = excluded.is_admin, is_guest = 0",
            (name, name, hash_password(password), 1 if is_admin == "1" else 0, now()))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/sections")
def admin_sections(request: Request, action: str = Form(...), name: str = Form(""),
                   rename: str = Form(""), user=Depends(require_admin)):
    """Add, rename, reorder or remove a category."""
    name = name.strip()
    with db() as conn:
        if action == "add" and name:
            nxt = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM sections").fetchone()["p"]
            conn.execute("INSERT OR IGNORE INTO sections (name, position, created_at)"
                         " VALUES (?, ?, ?)", (name, nxt, now()))
        elif action == "rename" and name and rename.strip():
            new_name = rename.strip()
            row = conn.execute("SELECT id FROM sections WHERE name = ?", (name,)).fetchone()
            clash = conn.execute("SELECT id FROM sections WHERE name = ?", (new_name,)).fetchone()
            if row and not clash:
                conn.execute("UPDATE sections SET name = ? WHERE id = ?", (new_name, row["id"]))
                # Games carry the name, so they move with it.
                conn.execute("UPDATE games SET section = ?, updated_at = ? WHERE section = ?",
                             (new_name, now(), name))
        elif action == "delete" and name:
            # The games survive; they just stop belonging to a category.
            conn.execute("UPDATE games SET section = NULL, updated_at = ? WHERE section = ?",
                         (now(), name))
            conn.execute("DELETE FROM sections WHERE name = ?", (name,))
        elif action in ("up", "down") and name:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, name FROM sections ORDER BY position, name")]
            names = [r["name"] for r in rows]
            if name in names:
                i = names.index(name)
                j = i - 1 if action == "up" else i + 1
                if 0 <= j < len(rows):
                    rows[i], rows[j] = rows[j], rows[i]
                    for pos, r in enumerate(rows):
                        conn.execute("UPDATE sections SET position = ? WHERE id = ?",
                                     (pos, r["id"]))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/seats")
def admin_seats(request: Request, left: str = Form(""), right: str = Form(""),
                user=Depends(require_admin)):
    """Assign the two grade columns. Left is blue, right is purple."""
    def as_id(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    left_id, right_id = as_id(left), as_id(right)
    if left_id is not None and left_id == right_id:
        right_id = None
    with db() as conn:
        conn.execute("UPDATE users SET grade_seat = NULL")
        for seat, uid in ((1, left_id), (2, right_id)):
            if uid is not None:
                conn.execute(
                    "UPDATE users SET grade_seat = ? WHERE id = ?"
                    " AND COALESCE(is_guest, 0) = 0", (seat, uid))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/guest")
def admin_guest(request: Request, action: str = Form("on"), password: str = Form(""),
                user=Depends(require_admin)):
    """Turn the shared viewing account on or off, or change its password."""
    with db() as conn:
        if action == "off":
            conn.execute("DELETE FROM users WHERE is_guest = 1")
        else:
            conn.execute(
                "INSERT INTO users (username, display_name, password_hash, is_admin, is_guest,"
                " created_at) VALUES ('guest', 'guest', ?, 0, 1, ?)"
                " ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash,"
                " is_admin = 0, is_guest = 1",
                (hash_password(password), now()))
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
               " ORDER BY COALESCE(date_added, '') DESC, id DESC LIMIT ?")
        params = (n,)
    elif scope == "section" and section:
        sql = ("SELECT * FROM games WHERE status = 'active' AND section = ?"
               " ORDER BY title_sort LIMIT ?")
        params = (section, n)
    elif scope == "slated":
        sql = ("SELECT * FROM games WHERE slated_at IS NOT NULL AND status = 'active'"
               " ORDER BY title_sort LIMIT ?")
        params = (n,)
    else:
        sql = ("SELECT * FROM games WHERE status = 'active'"
               " ORDER BY title_sort LIMIT ?")
        params = (n,)

    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return exporter.plain_text(rows, fmt)


# Browsers ask for these at the root whatever the page says, and a bookmark
# made before the page loads asks for nothing else.
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(os.path.join(BASE, "static", "favicon.ico"),
                        media_type="image/x-icon")


@app.get("/site.webmanifest", include_in_schema=False)
def webmanifest():
    return FileResponse(os.path.join(BASE, "static", "site.webmanifest"),
                        media_type="application/manifest+json")


@app.get("/healthz")
def healthz():
    return {"ok": True}
