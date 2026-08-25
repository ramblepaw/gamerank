"""Cover art, store links and release dates, from IGDB.

IGDB carries boxart for nearly everything including unreleased games, and its
`websites` / `external_games` fields already hold the store links - so nothing
here calls Steam. Steam would only be worth querying for version and update
information, which this app doesn't do yet.

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET; everything degrades to "no
result" without them.
"""
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .db import norm_title, now

TIMEOUT = 15
# How alike two normalised titles must be before art is written without asking.
SIMILARITY = 0.90
UA = "GameRank/1.0"

TWITCH_ID = os.environ.get("TWITCH_CLIENT_ID", "").strip()
TWITCH_SECRET = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()

_TOKEN_CACHE = {"value": None, "expires": 0}

# Matched against URLs rather than IGDB's category numbers, which have shifted
# between API versions.
STORES = [
    ("steam", re.compile(r"store\.steampowered\.com/app/(\d+)")),
    ("gog", re.compile(r"gog\.com/(?:en/)?game/")),
    ("epic", re.compile(r"epicgames\.com/")),
    ("itch", re.compile(r"\.itch\.io")),
    ("microsoft", re.compile(r"microsoft\.com/.*/p/")),
]


def igdb_available() -> bool:
    return bool(TWITCH_ID and TWITCH_SECRET)


def steam_store_url(appid) -> str:
    return "https://store.steampowered.com/app/%s/" % appid if appid else ""


# IGDB allows 4 requests a second. Going over gets 429s that would otherwise
# look exactly like "no such game", which is how a batch run quietly skips
# titles that are actually there.
MIN_INTERVAL = 0.28
_last_call = [0.0]


def _throttle():
    wait = MIN_INTERVAL - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()


def _request(url: str, headers=None, data=None, retries: int = 2):
    for attempt in range(retries + 1):
        _throttle()
        req = urllib.request.Request(
            url, data=data, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(1.0 + attempt)
                continue
            return None
        except (urllib.error.URLError, ValueError, TimeoutError, OSError):
            if attempt < retries:
                time.sleep(0.5)
                continue
            return None
    return None


def _token() -> str:
    if not igdb_available():
        return ""
    if _TOKEN_CACHE["value"] and time.time() < _TOKEN_CACHE["expires"] - 60:
        return _TOKEN_CACHE["value"]
    body = urllib.parse.urlencode({
        "client_id": TWITCH_ID,
        "client_secret": TWITCH_SECRET,
        "grant_type": "client_credentials",
    }).encode()
    data = _request("https://id.twitch.tv/oauth2/token", data=body)
    if not data or "access_token" not in data:
        return ""
    _TOKEN_CACHE["value"] = data["access_token"]
    _TOKEN_CACHE["expires"] = time.time() + int(data.get("expires_in", 3600))
    return _TOKEN_CACHE["value"]


FIELDS = ("fields name, cover.image_id, first_release_date, url,"
          " websites.url, external_games.uid, external_games.url;")


def _query(body: str):
    token = _token()
    if not token:
        return None
    return _request(
        "https://api.igdb.com/v4/games",
        headers={"Client-ID": TWITCH_ID, "Authorization": "Bearer " + token},
        data=body.encode(),
    )


def test_connection() -> dict:
    """Used by the admin page so credentials can be checked without guessing."""
    if not igdb_available():
        return {"ok": False, "detail": "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are not set."}
    if not _token():
        return {"ok": False, "detail": "Twitch rejected the credentials."}
    # Run a real lookup rather than a raw search, so the message reflects what
    # the app would actually store for a game.
    found = lookup("Portal 2")
    if not found:
        return {"ok": False, "detail": "Token works but the test lookup came back empty."}
    return {"ok": True, "detail": 'Connected. Looking up "Portal 2" resolved to "%s"%s.'
            % (found.get("title", "?"), " with cover art" if found.get("cover_url") else "")}


def _links(game: dict) -> dict:
    """Pull store URLs out of websites/external_games. Steam also yields an appid."""
    urls = [w.get("url", "") for w in (game.get("websites") or [])]
    urls += [e.get("url", "") for e in (game.get("external_games") or []) if e.get("url")]

    found = {}
    for url in urls:
        for name, pattern in STORES:
            hit = pattern.search(url or "")
            if hit and name not in found:
                found[name] = url
                if name == "steam":
                    found["steam_appid"] = int(hit.group(1))

    # external_games carries a bare Steam uid even when no URL is present.
    if "steam_appid" not in found:
        for ext in (game.get("external_games") or []):
            uid = str(ext.get("uid") or "")
            if uid.isdigit() and 1 <= len(uid) <= 8 and ext.get("url", "").find("steam") >= 0:
                found["steam_appid"] = int(uid)
                break
    return found


def _shape(game: dict) -> dict:
    cover = ""
    if (game.get("cover") or {}).get("image_id"):
        cover = ("https://images.igdb.com/igdb/image/upload/t_cover_big_2x/%s.jpg"
                 % game["cover"]["image_id"])
    release = ""
    if game.get("first_release_date"):
        release = time.strftime("%Y-%m-%d", time.gmtime(game["first_release_date"]))

    links = _links(game)
    store = (links.get("steam") or links.get("gog") or links.get("epic")
             or links.get("itch") or links.get("microsoft") or game.get("url") or "")

    return {
        "title": game.get("name"),
        "igdb_id": game.get("id"),
        "cover_url": cover,
        "store_url": store,
        "steam_appid": links.get("steam_appid"),
        "release_date": release,
        "meta_source": "igdb",
    }


def _safe(title: str) -> str:
    return re.sub(r'["\\]', "", title or "").strip()


def search(title: str, limit: int = 6) -> list:
    """IGDB's fuzzy search. Unreliable on multi-word titles - it happily
    returns "Metal Storm" for "Against the Storm" and omits the real game -
    so by_name/by_name_like are tried first."""
    safe = _safe(title)
    if not safe:
        return []
    data = _query('search "%s"; %s limit %d;' % (safe, FIELDS, limit))
    return [_shape(g) for g in (data or [])]


def by_name(title: str, limit: int = 3) -> list:
    """Exact name, case-insensitive. Precise and cheap."""
    safe = _safe(title)
    if not safe:
        return []
    data = _query('where name ~ "%s"; %s limit %d;' % (safe, FIELDS, limit))
    return [_shape(g) for g in (data or [])]


def by_name_like(title: str, limit: int = 6) -> list:
    """Substring match - finds the subtitled entries fuzzy search misses."""
    safe = _safe(title).lower()
    if len(safe) < 4:
        return []
    data = _query('where name ~ *"%s"*; %s limit %d;' % (safe, FIELDS, limit))
    return [_shape(g) for g in (data or [])]


def by_steam_appid(appid) -> dict:
    """Exact lookup when a pasted Discord link already told us the appid."""
    data = _query('where external_games.uid = "%s"; %s limit 1;' % (appid, FIELDS))
    if data:
        return _shape(data[0])
    return {}


ROMAN = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
    "xvi": 16, "xvii": 17, "xviii": 18, "xix": 19, "xx": 20,
}
ARABIC = {v: k for k, v in ROMAN.items()}


MAX_SEQUENCE = 30


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein, giving up once it exceeds `cap`.

    A misspelling is a small number of character edits, which a similarity
    ratio measures badly: "snalland" against "smalland" is one substitution
    but only scores 0.875, while "fallout3" against "fallout4" is also one
    edit and must never match. Distance plus the number guard separates them.
    """
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def is_typo_of(wanted: str, name: str) -> bool:
    """Close enough to be a misspelling rather than a different game."""
    if len(wanted) < 6 or not name:
        return False
    # "portal" and "mortal" are one edit apart but different games. Typists
    # rarely get the first letter wrong, so requiring it costs nothing real
    # and removes a whole class of wrong match.
    if wanted[0] != name[0]:
        return False
    # One edit only. Two lets "Super Meatboy" match "Super Catboy".
    return edit_distance(wanted, name, 1) <= 1


def _numbers(title: str) -> list:
    """Sequence numbers in a title, with roman numerals folded to digits.

    Only small values count. "Warhammer 40,000" and "Tomb Raider (2013)" carry
    numbers that are part of the name, not an instalment number, and treating
    them as sequence numbers blocks correct matches.
    """
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", (title or "").lower())
    out = []
    for part in re.split(r"[^A-Za-z0-9]+", cleaned):
        if part.isdigit():
            value = int(part)
            if 1 <= value <= MAX_SEQUENCE:
                out.append(value)
        elif part in ROMAN:
            out.append(ROMAN[part])
    return sorted(out)


def same_numbers(a: str, b: str) -> bool:
    """Guard for fuzzy matching.

    "Roller Coaster Tycoon 2" and "RollerCoaster Tycoon 3" are one character
    apart and score 0.95 on similarity, but they are different games. A digit
    is never a typo, so any difference in the numbers is disqualifying.
    """
    return _numbers(a) == _numbers(b)


def numkey(title: str) -> str:
    """Comparison key where 'Final Fantasy 16' and 'Final Fantasy XVI' agree."""
    parts = re.split(r"[^A-Za-z0-9]+", (title or "").lower())
    out = []
    for p in parts:
        if not p:
            continue
        out.append(str(ROMAN[p]) if p in ROMAN else p)
    return "".join(out)


def _roman_variants(title: str) -> list:
    """Same title with trailing numbers swapped between arabic and roman."""
    out = []
    arabic = re.sub(r"\b(\d{1,2})\b",
                    lambda m: ARABIC.get(int(m.group(1)), m.group(1)).upper(), title)
    if arabic != title:
        out.append(arabic)
    roman = re.sub(r"\b([ivx]{1,6})\b",
                   lambda m: str(ROMAN[m.group(1).lower()]) if m.group(1).lower() in ROMAN
                   else m.group(1), title, flags=re.I)
    if roman != title:
        out.append(roman)
    return out


def _clean(title: str) -> str:
    """Loosen a title enough for search: punctuation to spaces, edition noise off."""
    t = re.sub(r"[:\-_/\\|,.!?~]+", " ", title)
    t = re.sub(r"\b(deluxe|definitive|complete|goty|game of the year|ultimate|"
               r"remastered|enhanced|special)\s+edition\b", " ", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def candidates(title: str, limit: int = 6) -> list:
    """Search results for a person to pick from, best-effort ordered.

    Retries with punctuation stripped and with roman/arabic numerals swapped,
    because a library this size is full of "Final Fantasy 16" where IGDB has
    "Final Fantasy XVI".
    """
    results = by_name(title)
    seen = {r.get("igdb_id") for r in results}

    def add(rows):
        for r in rows:
            if r.get("igdb_id") not in seen:
                seen.add(r.get("igdb_id"))
                results.append(r)

    if len(results) < limit:
        add(by_name_like(title, limit))
    if len(results) < limit:
        add(search(title, limit))

    tries = []
    cleaned = _clean(title)
    if cleaned.lower() != title.lower():
        tries.append(cleaned)
    tries += _roman_variants(title)
    for alt in tries:
        if len(results) >= limit:
            break
        add(by_name(alt))
        add(search(alt, limit))

    if not results:
        add(_space_variants(title))
    return results


def _space_variants(title: str) -> list:
    """Try inserting a space at each position of a run-together word.

    IGDB has "Fae Farm" and "Goblin Company"; the sheet says "Faefarm" and
    "GoblinCompany". No substring query crosses that space, but an exact-name
    query on the split form lands it.
    """
    words = title.split()
    if len(words) > 2:
        return []
    joined = "".join(words)
    if not joined.isalpha() or len(joined) < 6 or len(joined) > 24:
        return []
    for i in range(3, len(joined) - 2):
        hit = by_name(joined[:i] + " " + joined[i:], 2)
        if hit:
            return hit
    return []


def suggest(title: str, limit: int = 12) -> list:
    """Everything worth showing a person on the match page.

    Unlike `candidates`, this also searches the leading and trailing words, so
    a misspelled title still turns up options - which is the whole point of the
    page. Ranked by similarity to what the sheet says; nothing is filtered out
    by a threshold, because a person is doing the choosing.
    """
    found = list(candidates(title, limit))
    seen = {r.get("igdb_id") for r in found}

    words = title.split()
    stems = []
    for take in (len(words) - 1, 2, 1):
        if 1 <= take < len(words):
            stems.append(" ".join(words[:take]))
            stems.append(" ".join(words[-take:]))

    tried = set()
    for stem in stems:
        if len(found) >= limit or len(stem) < 4 or stem.lower() in tried:
            continue
        tried.add(stem.lower())
        for r in by_name_like(stem, 20):
            if r.get("igdb_id") not in seen:
                seen.add(r.get("igdb_id"))
                found.append(r)

    wanted = norm_title(title)
    found.sort(key=lambda r: difflib.SequenceMatcher(
        None, wanted, norm_title(r.get("title") or "")).ratio(), reverse=True)
    return found[:limit]


def _pc_release_rank(entry: dict) -> tuple:
    """Sort key for entries sharing a title. Lower is better.

    Art first, since an entry without it is useless here. Then a store link,
    which is what separates the PC release from a console port. Then the
    earliest date, which is the original rather than a later re-release.
    """
    return (
        0 if entry.get("cover_url") else 1,
        0 if entry.get("steam_appid") or entry.get("store_url") else 1,
        entry.get("release_date") or "9999",
    )


def lookup(title: str, appid=None) -> dict:
    """Best single match, or nothing.

    IGDB's search ranking is loose - "Portal 2" can return "Portal Maze 2" on
    top - so a result is only written automatically when it's an exact title
    or an unambiguous prefix of one. Anything looser is left for a person to
    pick from on the game page, because wrong art across a whole library is
    worse than no art.
    """
    if appid:
        found = by_steam_appid(appid)
        if found.get("cover_url"):
            found.setdefault("steam_appid", int(appid))
            found["store_url"] = found.get("store_url") or steam_store_url(appid)
            return found

    results = candidates(title)
    wanted, wantnum = norm_title(title), numkey(title)
    if not results:
        # No candidates at all is the normal shape of a misspelled title, so
        # this is precisely when the typo pass is worth running.
        return _typo_pass(title, wanted)

    # Duplicate names are the norm on IGDB, not the exception: console ports,
    # remasters and regional releases all carry the game's plain title. The
    # extras almost never have a store link, so preferring the entry that does
    # picks the PC release - which is the only kind this library holds.
    exact, seen_ids = [], set()
    for r in results:
        name = r.get("title") or ""
        if norm_title(name) == wanted or numkey(name) == wantnum:
            if r.get("igdb_id") not in seen_ids:
                seen_ids.add(r.get("igdb_id"))
                exact.append(r)
    if exact:
        exact.sort(key=_pc_release_rank)
        return dict(exact[0], match_kind="exact")

    # IGDB often carries the subtitled name: "Shapez 2: Factory" for what the
    # sheet calls "Shapez 2". Compare against the part before the subtitle.
    # This stays safe where a bare prefix would not - "Portal Knights" has no
    # subtitle to strip, so it can never be mistaken for "Portal".
    for r in results:
        base = re.split(r":| - ", r.get("title") or "", 1)[0]
        if base and (norm_title(base) == wanted or numkey(base) == wantnum):
            if r.get("cover_url"):
                return dict(r, match_kind="subtitle")

    # A prefix counts only when the two titles are close in length, so
    # "Forza Horizon 4" can take "Forza Horizon 4: Deluxe Edition" while
    # "Portal 2" never takes "Portal Maze 2".
    top = norm_title(results[0].get("title") or "")
    if top and results[0].get("cover_url"):
        short, long = sorted((wanted, top), key=len)
        if long.startswith(short) and len(short) / len(long) >= 0.7:
            return dict(results[0], match_kind="prefix")

    # Last resort: near-identical strings. Catches a missing apostrophe-s
    # ("Assassin Creed" for "Assassin's Creed") or a dropped article
    # ("Legends Returns" for "The Legend Returns"). The threshold is high
    # enough that "Portal 2" still scores too far from "Portal Maze 2".
    for r in results[:3]:
        if not r.get("cover_url"):
            continue
        name = norm_title(r.get("title") or "")
        if not name or not same_numbers(title, r.get("title") or ""):
            continue
        if difflib.SequenceMatcher(None, wanted, name).ratio() >= SIMILARITY:
            return dict(r, match_kind="similar")

    return _typo_pass(title, wanted)


def _closest(wanted: str, title: str, pieces, limit: int = 40) -> dict:
    """Search each substring and return the first result that is a typo of the
    title. Subtitled entries are compared on their base name too, so
    "Smalland: Survive the Wilds" still matches a sheet that just says
    "Smalland"."""
    tried = set()
    for piece in pieces:
        key = (piece or "").lower()
        if len(key) < 4 or key in tried:
            continue
        tried.add(key)

        # Rank rather than taking the first hit. A franchise brings back the
        # base game, its DLC, and its VR and deluxe editions all at once, and
        # the sheet almost always means the plain one - which is the entry
        # closest to the title and the shortest of the bunch.
        scored = []
        for r in by_name_like(key, limit):
            name = r.get("title") or ""
            if not r.get("cover_url") or not same_numbers(title, name):
                continue
            base = re.split(r":| - ", name, 1)[0]
            if is_typo_of(wanted, norm_title(name)) or is_typo_of(wanted, norm_title(base)):
                scored.append((edit_distance(wanted, norm_title(name), 99), len(name), r))
        if scored:
            scored.sort(key=lambda x: (x[0], x[1]))
            return dict(scored[0][2], match_kind="typo")
    return {}


def _typo_pass(title: str, wanted: str) -> dict:
    """Last resort for misspellings in the sheet.

    Searches on whichever part of the title is likely to be spelled right -
    leading words, trailing words, or character substrings from either end -
    and accepts a result only if it is within an edit or two of what the sheet
    says.
    """
    words = title.split()
    joined = "".join(words)
    if len(words) == 1 or len(joined) <= 16:
        # Character substrings, taken from both ends. A typo near the start
        # ("Snalland" for "Smalland", "Athermancer" for "Aethermancer") poisons
        # every prefix, so the suffix is the only usable handle - and the
        # reverse is true for a typo near the end.
        pieces = []
        for n in (7, 6, 5):
            if n < len(joined):
                pieces.append(joined[-n:])
                pieces.append(joined[:n])
        hit = _closest(wanted, title, pieces, limit=60)
        if hit:
            return hit
        if len(words) == 1:
            return {}

    stems = []
    for take in (len(words) - 1, len(words) - 2, 2, 1):
        if 1 <= take < len(words):
            stems.append(" ".join(words[:take]))
    # Suffixes matter as much as prefixes: IGDB writes "Pokemon" with an
    # accented e, so no leading stem of "Pokemon Uranium" will ever hit - but
    # the trailing word does.
    for take in (len(words) - 1, 2, 1):
        if 1 <= take < len(words):
            stems.append(" ".join(words[-take:]))

    tried = set()
    for stem in stems:
        key = stem.lower()
        if len(stem) < 4 or key in tried:
            continue
        tried.add(key)

        best, best_score = None, 0.0
        for r in by_name_like(stem, 30):
            if not r.get("cover_url") or not same_numbers(title, r.get("title") or ""):
                continue
            score = difflib.SequenceMatcher(
                None, wanted, norm_title(r.get("title") or "")).ratio()
            if score > best_score:
                best, best_score = r, score
        if best and best_score >= SIMILARITY - 0.02:
            return dict(best, match_kind="typo")
    return {}


def apply(conn, game_id: int, meta: dict, overwrite_title: bool = False) -> None:
    if not meta:
        return
    sets, params = [], []
    for column in ("steam_appid", "igdb_id", "cover_url", "store_url",
                   "release_date", "meta_source"):
        if meta.get(column):
            sets.append("%s = ?" % column)
            params.append(meta[column])
    if overwrite_title and meta.get("title"):
        # title_norm has to move with the title or every later match is
        # comparing against the old spelling.
        sets.append("title = ?")
        params.append(meta["title"])
        sets.append("title_norm = ?")
        params.append(norm_title(meta["title"]))
    if not sets:
        return
    sets.append("meta_fetched_at = ?")
    params.append(now())
    params.append(game_id)
    conn.execute("UPDATE games SET " + ", ".join(sets) + " WHERE id = ?", params)
