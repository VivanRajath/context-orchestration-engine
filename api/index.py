"""Vercel entry point for the web playground.

Vercel discovers an ASGI app named ``app`` in this file and routes every path
to it (see ``vercel.json``). Two things differ from ``coe serve``:

* the package lives in ``src/`` and is not pip-installed on the build, so the
  path is added here rather than shipping a second copy of the source;
* a serverless filesystem is read-only apart from ``/tmp``, so SQLite is
  written there. That storage is per-instance and disappears when the instance
  does, which is fine for a demonstration and is why the run history panel can
  look empty - a full run still persists and reloads within a single request.

``COE_SERVERLESS`` (set in vercel.json) switches the playground onto its
single-request streaming path and closes live model calls. Set
``COE_ALLOW_LIVE=1`` on a private deployment that carries its own keys.

Building the app is wrapped, because this module runs during the import that
the host performs before it will serve anything at all. An exception here is
not a failed request, it is a dead deployment: the host has nothing to call,
so every URL returns its own crash page and the reason lives in a log the
person looking at the site cannot see. The fallback below turns that into a
page that says what broke.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("COE_SERVERLESS", "1")

try:
    from context_orchestration.web.server import create_app

    app = create_app(db=os.environ.get("COE_DB", "/tmp/playground.db"))
except Exception:  # pragma: no cover - exercised only by a broken build
    FAILURE = traceback.format_exc()

    def _report() -> str:
        listing = []
        for path in sorted(ROOT.rglob("*")):
            rel = path.relative_to(ROOT).as_posix()
            if path.is_file() and not rel.startswith((".git/", "node_modules/")):
                listing.append(rel)
        return (
            "The engine did not start.\n\n"
            f"python {sys.version.split()[0]}\n"
            f"root   {ROOT}\n\n"
            f"{FAILURE}\n"
            "Files in this build:\n  " + "\n  ".join(listing[:400])
        )

    async def app(scope, receive, send):  # type: ignore[misc]
        """A last-resort ASGI app: one plain-text page, every path, no imports."""
        if scope["type"] != "http":
            return
        body = _report().encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
