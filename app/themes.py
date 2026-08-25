"""Theme resolution.

Built-in palettes live in the stylesheet as `[data-theme="..."]` blocks and are
read back out of it here, so a custom theme can start from one without the
values being written down twice and drifting apart.
"""
import json
import os
import re

from .db import db, now, DEFAULT_THEME, THEMES, THEME_TOKENS

CSS_PATH = os.path.join(os.path.dirname(__file__), "static", "style.css")
BUILTIN = {t[0] for t in THEMES}
_cache = {"mtime": 0, "palettes": {}}


def _parse_stylesheet() -> dict:
    try:
        mtime = os.path.getmtime(CSS_PATH)
    except OSError:
        return {}
    if _cache["palettes"] and _cache["mtime"] == mtime:
        return _cache["palettes"]

    try:
        with open(CSS_PATH, encoding="utf-8") as fh:
            css = fh.read()
    except OSError:
        return {}

    palettes = {}
    for match in re.finditer(r'\[data-theme="(\w+)"\]\s*\{(.*?)\n\}', css, re.S):
        name, body = match.group(1), match.group(2)
        palettes[name] = dict(re.findall(r"--([a-zA-Z0-9-]+)\s*:\s*([^;]+);", body))
    # The default theme is written on :root as well as its own block.
    root = re.search(r":root,\s*\n\[data-theme=", css)
    if root and DEFAULT_THEME not in palettes:
        palettes[DEFAULT_THEME] = {}

    _cache.update({"mtime": mtime, "palettes": palettes})
    return palettes


def builtin_palette(slug: str) -> dict:
    """Token values for a built-in theme, for seeding a custom one."""
    return dict(_parse_stylesheet().get(slug, {}))


def custom_list(conn=None) -> list:
    """Custom themes with their tokens already parsed, for rendering swatches."""
    def _read(c):
        rows = [dict(r) for r in c.execute(
            "SELECT ct.*, u.username AS author FROM custom_themes ct"
            " LEFT JOIN users u ON u.id = ct.created_by ORDER BY ct.name COLLATE NOCASE")]
        for row in rows:
            try:
                row["token_map"] = json.loads(row["tokens"]) or {}
            except (ValueError, TypeError):
                row["token_map"] = {}
        return rows
    if conn is not None:
        return _read(conn)
    with db() as c:
        return _read(c)


def get_custom(slug: str) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM custom_themes WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else {}


def tokens_for(slug: str) -> dict:
    """The inline CSS variables a custom theme needs. Empty for built-ins."""
    if not slug or slug in BUILTIN:
        return {}
    row = get_custom(slug)
    if not row:
        return {}
    try:
        stored = json.loads(row["tokens"])
    except (ValueError, TypeError):
        return {}
    allowed = {t[0] for t in THEME_TOKENS}
    return {k: v for k, v in stored.items() if k in allowed and v}


def slugify(name: str, taken=()) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-") or "theme"
    slug = "custom-" + base
    n = 2
    while slug in taken or slug in BUILTIN:
        slug = "custom-%s-%d" % (base, n)
        n += 1
    return slug


def save_custom(name: str, based_on: str, tokens: dict, user_id=None, slug: str = "") -> str:
    allowed = {t[0] for t in THEME_TOKENS}
    clean = {k: v.strip() for k, v in tokens.items()
             if k in allowed and re.fullmatch(r"#[0-9a-fA-F]{3,8}", (v or "").strip())}
    payload = json.dumps(clean)

    with db() as c:
        if slug:
            c.execute("UPDATE custom_themes SET name = ?, based_on = ?, tokens = ?,"
                      " updated_at = ? WHERE slug = ?",
                      (name.strip() or "Custom", based_on, payload, now(), slug))
            return slug
        taken = {r["slug"] for r in c.execute("SELECT slug FROM custom_themes")}
        slug = slugify(name, taken)
        c.execute("INSERT INTO custom_themes (slug, name, based_on, tokens, created_by,"
                  " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (slug, name.strip() or "Custom", based_on, payload, user_id, now(), now()))
    return slug


def delete_custom(slug: str) -> None:
    with db() as c:
        c.execute("DELETE FROM custom_themes WHERE slug = ?", (slug,))
        # Anyone using it falls back to the default rather than a blank page.
        c.execute("UPDATE users SET theme = ? WHERE theme = ?", (DEFAULT_THEME, slug))
