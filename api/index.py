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


def _installed() -> list[str]:
    try:
        from importlib.metadata import distributions

        return sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in distributions()
            if d.metadata["Name"]
        )
    except Exception:  # pragma: no cover - the report must not fail too
        return []


def _own_files() -> list[str]:
    """The project's own files, which is the part worth reading.

    Not the installed packages' files. The first version of this listed every
    file under the root and the answer, one missing package, was buried under
    four hundred lines of a vendored boto3.
    """
    skip = (".git/", "node_modules/", "_vendor/", "__pycache__/", "___vc/",
            ".venv/", "venv/", "dist/", ".pytest_cache/")
    files = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT).as_posix()
        if path.is_file() and not relative.startswith(skip) and "__pycache__/" not in relative:
            files.append(relative)
    return files


def _report(failure: str) -> str:
    """What broke, and the two things that differ from a laptop.

    A build is assembled by someone else's packer from someone else's index of
    releases, so it fails for one of two reasons: a file did not travel, or a
    package was not installed. Both are below, in that order, because the
    traceback names the symptom and these name the cause.
    """
    files = _own_files()
    packages = _installed()
    return (
        "The engine did not start.\n\n"
        f"python {sys.version.split()[0]}\n"
        f"root   {ROOT}\n\n"
        f"{failure}\n"
        f"Installed packages ({len(packages)}):\n  "
        + "\n  ".join(packages or ["(none found)"])
        + f"\n\nProject files in this build ({len(files)}):\n  "
        + "\n  ".join(files[:200])
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
