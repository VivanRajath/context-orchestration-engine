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

Building the app is guarded, because this module runs during the import the
host performs before it will serve anything at all. An exception here is not a
failed request, it is a deployment with nothing to call: every URL returns the
host's own crash page and the reason lives in a log the person looking at the
site cannot see. So a failure produces a page that says what broke instead.

The guard lives in a function, and ``app`` is assigned at the bottom of this
file at the top level. That placement is load-bearing rather than stylistic:
the host finds the application by reading this file, not by importing it, so
an ``app`` indented inside a ``try`` is an ``app`` it cannot see, and the build
fails with "none define a top-level app".
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("COE_SERVERLESS", "1")


def _report(failure: str) -> str:
    """What broke, and what was in the build when it broke.

    The second half is the useful half. A build is assembled by someone else's
    packer, so the usual reason something runs on a laptop and not here is
    that a file did not travel.
    """
    listing = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT).as_posix()
        if path.is_file() and not relative.startswith((".git/", "node_modules/")):
            listing.append(relative)
    return (
        "The engine did not start.\n\n"
        f"python {sys.version.split()[0]}\n"
        f"root   {ROOT}\n\n"
        f"{failure}\n"
        "Files in this build:\n  " + "\n  ".join(listing[:400])
    )


def _broken(failure: str):
    """A last-resort ASGI app: one plain-text page, every path, no imports."""
    body = _report(failure).encode("utf-8")

    async def application(scope, receive, send):
        if scope["type"] != "http":
            return
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

    return application


def _build():
    try:
        from context_orchestration.web.server import create_app

        return create_app(db=os.environ.get("COE_DB", "/tmp/playground.db"))
    except Exception:  # pragma: no cover - exercised only by a broken build
        return _broken(traceback.format_exc())


app = _build()
