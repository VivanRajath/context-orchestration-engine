"""Allow `python -m context_orchestration`."""

import sys

from context_orchestration.cli import main

if __name__ == "__main__":
    sys.exit(main())
