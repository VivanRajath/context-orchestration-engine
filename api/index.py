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
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("COE_SERVERLESS", "1")

from context_orchestration.web.server import create_app  # noqa: E402

app = create_app(db=os.environ.get("COE_DB", "/tmp/playground.db"))
