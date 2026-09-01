"""How many people have opened the page.

One number, and it has to be true. A number invented in the browser, or one
that starts again from zero whenever the host recycles an instance, is worse
than no number: it is a claim on a page whose whole argument is that state
survives the thing holding it.

That is also why this is not a row in the run database. The playground writes
its execution state to SQLite under ``/tmp`` when it is deployed, which is
per-instance and vanishes with the instance. Fine for a run that begins and
ends inside one request. Useless for a total.

So there are two stores, and the counter says which one it is using:

* a Redis over HTTP, when the deployment has been given one. Upstash's REST
  API is two environment variables and no client library, which matters here:
  the deployed bundle is three packages and adding a Redis driver to hold one
  integer would be the largest dependency in it. Vercel's own KV integration
  sets the same pair under different names, and both are read.
* SQLite otherwise, which is durable exactly when the filesystem is - true
  for ``coe serve`` on a laptop, false for a serverless instance.

``durable`` is that distinction, and the page uses it: an undercount is not
shown at all rather than shown as if it were the total.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

KEY = "coe:visits"
TIMEOUT = 3.0

# Upstash's own names, then the ones Vercel's KV integration injects. Same
# service, same REST shape; only the environment variable differs.
URL_VARS = ("COE_COUNTER_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL")
TOKEN_VARS = ("COE_COUNTER_TOKEN", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN")


def _first(names: tuple[str, ...]) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def redis_config() -> tuple[str, str] | None:
    url, token = _first(URL_VARS), _first(TOKEN_VARS)
    return (url.rstrip("/"), token) if url and token else None


def _redis(command: str) -> int:
    """One Redis command over HTTP. Raises if the store cannot be reached."""
    config = redis_config()
    if config is None:  # pragma: no cover - guarded by the caller
        raise RuntimeError("no counter store configured")
    url, token = config
    request = urllib.request.Request(
        f"{url}/{command}",
        headers={"Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        body = json.loads(response.read().decode("utf-8"))
    # {"result": 41}, or {"result": null} for a key nobody has written yet.
    return int(body.get("result") or 0)


class VisitCounter:
    """Total page opens, and an honest answer about whether it is the total."""

    def __init__(self, db: str | Path = "playground.db") -> None:
        self.db = str(db)
        self.remote = redis_config() is not None

    @property
    def durable(self) -> bool:
        """False when the store dies with the process holding it."""
        if self.remote:
            return True
        # A serverless instance gets a fresh /tmp and then disappears with it.
        return not _serverless()

    # -- SQLite ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db, timeout=5.0)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS site_visits ("
            "  name TEXT PRIMARY KEY,"
            "  n    INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        return connection

    def _local_bump(self) -> int:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO site_visits (name, n) VALUES (?, 1) "
                "ON CONFLICT(name) DO UPDATE SET n = n + 1",
                (KEY,),
            )
            row = connection.execute(
                "SELECT n FROM site_visits WHERE name = ?", (KEY,)
            ).fetchone()
        return int(row[0]) if row else 0

    def _local_total(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT n FROM site_visits WHERE name = ?", (KEY,)
            ).fetchone()
        return int(row[0]) if row else 0

    # -- either store ----------------------------------------------------

    def bump(self) -> int:
        """Count one opening and return the new total."""
        if self.remote:
            try:
                return _redis(f"incr/{KEY}")
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                # A counter is not worth failing a page load over.
                return self._local_bump()
        return self._local_bump()

    def total(self) -> int:
        if self.remote:
            try:
                return _redis(f"get/{KEY}")
            except (urllib.error.URLError, OSError, ValueError, TimeoutError):
                return self._local_total()
        return self._local_total()


def _serverless() -> bool:
    return os.environ.get("COE_SERVERLESS", "").strip().lower() in {"1", "true", "yes", "on"}
