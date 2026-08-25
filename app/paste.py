"""Parse the markdown list you post to Discord.

    * [Fish Hunters](https://store.steampowered.com/app/3468330/)

Bullets optional, link optional, numbered lists and bare titles all work.
Steam appids are pulled straight out of the URL, so anything added this way
arrives with its store link and cover art already attached.
"""
import re

from .db import norm_title

# A shortened sheet title has to be at least this long before it is offered as
# a possible match, or "Ico" would light up against half the library.
MIN_PREFIX = 12

LINK = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)\s*$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*(?P<title>.+?)\s*$")
APPID = re.compile(r"store\.steampowered\.com/app/(\d+)")


def steam_appid(url: str):
    match = APPID.search(url or "")
    return int(match.group(1)) if match else None


def parse(text: str) -> list:
    """Returns [{title, url, steam_appid}] in the order they appear."""
    out = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue

        match = LINK.match(line)
        if match:
            url = match.group("url").strip()
            out.append({
                "title": match.group("title").strip(),
                "url": url,
                "steam_appid": steam_appid(url),
            })
            continue

        match = BULLET.match(line)
        title = match.group("title").strip() if match else line.strip()
        # A bare URL on its own line has no title to work with.
        if not title or title.startswith(("http://", "https://")):
            continue
        out.append({"title": title, "url": "", "steam_appid": None})
    return out


def match(conn, title: str) -> dict:
    """Find the library row this pasted title refers to, if any.

    Titles in the sheet are sometimes shortened ("KOTAMON My Sis Found A
    Super-Rare Card" for a much longer store name), so an exact match alone
    creates duplicates. Anything short of exact is returned as a suggestion
    for a person to confirm - auto-merging on a prefix would happily fold
    "Portal" into "Portal 2".
    """
    key = norm_title(title)
    exact = conn.execute(
        "SELECT id, title, status FROM games WHERE title_norm = ?", (key,)).fetchone()
    if exact:
        return {"kind": "exact", "game": dict(exact)}

    if len(key) >= MIN_PREFIX:
        near = conn.execute(
            "SELECT id, title, status FROM games"
            " WHERE (? LIKE title_norm || '%' AND length(title_norm) >= ?)"
            "    OR (title_norm LIKE ? || '%')"
            " ORDER BY length(title_norm) DESC LIMIT 5",
            (key, MIN_PREFIX, key)).fetchall()
        if near:
            return {"kind": "near", "candidates": [dict(r) for r in near]}

    return {"kind": "new"}
