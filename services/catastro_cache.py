"""On-disk cache for Catastro lookups, so re-analyzing the same (or an
overlapping) area doesn't re-spend the free service's hourly per-IP request
quota -- see services/catastro.py for context on that limit.

Backed by SQLite (stdlib, no extra dependency) in a single file. This
persists for the lifetime of the running process/container. On a platform
like Render's free tier the filesystem resets on redeploy/restart, so this
isn't a durable store -- but it still meaningfully cuts request volume
across repeated or overlapping analyses run against the same live instance.

Both positive AND negative results are cached (e.g. "no cadastral parcel
at this point" is just as stable a fact as a real reference, and re-fetching
it wastes quota identically).
"""
import json
import os
import sqlite3
import threading
import time

_DB_PATH = os.environ.get(
    "CATASTRO_CACHE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "catastro_cache.sqlite3"),
)
_TTL_SECONDS = 60 * 24 * 60 * 60  # 60 days -- cadastral records rarely change
_lock = threading.Lock()


class _Miss:
    def __repr__(self):
        return "<cache miss>"


MISS = _Miss()


def _connect():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reference_cache (
            key TEXT PRIMARY KEY, rc TEXT, address TEXT, cached_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS details_cache (
            rc TEXT PRIMARY KEY, details_json TEXT, cached_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn


_conn = _connect()


def _reference_key(lon, lat):
    return f"{round(lon, 6)},{round(lat, 6)}"


def get_reference(lon, lat):
    """Return a cached (rc, address) tuple (rc may legitimately be None),
    or the MISS sentinel if there's no fresh cache entry."""
    key = _reference_key(lon, lat)
    with _lock:
        row = _conn.execute(
            "SELECT rc, address, cached_at FROM reference_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return MISS
    rc, address, cached_at = row
    if time.time() - cached_at > _TTL_SECONDS:
        return MISS
    return (rc, address)


def set_reference(lon, lat, rc, address):
    key = _reference_key(lon, lat)
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO reference_cache (key, rc, address, cached_at) VALUES (?, ?, ?, ?)",
            (key, rc, address, time.time()),
        )
        _conn.commit()


def get_details(rc):
    """Return the cached details dict (may legitimately be None, meaning
    'confirmed no details for this reference'), or MISS if not cached."""
    with _lock:
        row = _conn.execute(
            "SELECT details_json, cached_at FROM details_cache WHERE rc = ?", (rc,)
        ).fetchone()
    if row is None:
        return MISS
    details_json, cached_at = row
    if time.time() - cached_at > _TTL_SECONDS:
        return MISS
    return json.loads(details_json)


def set_details(rc, details):
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO details_cache (rc, details_json, cached_at) VALUES (?, ?, ?)",
            (rc, json.dumps(details), time.time()),
        )
        _conn.commit()
