"""Load .env before anything reads os.environ.

metadata.py picks up TWITCH_CLIENT_ID at import time, so this has to happen
here - in the package __init__ - rather than in main.py, which imports it.
Real environment variables always win, so Docker's `environment:` block is
unaffected.
"""
import os

_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")


def _load_env(path: str = _ENV_FILE) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()
