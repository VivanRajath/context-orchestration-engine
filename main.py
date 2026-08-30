"""Compatibility shim: `python main.py ...` still works after packaging.

The real entry point is the `coe` console script (see pyproject.toml), or
`python -m context_orchestration.cli`.
"""

import sys

from context_orchestration.cli import main

if __name__ == "__main__":
    sys.exit(main())
