"""Entry point.

Three equivalent ways to start the server:

    ./filebridge                                  # launcher at the repo root
    python3 tools/filebridge/__main__.py          # this file, run directly
    PYTHONPATH=tools python3 -m filebridge        # package form

All of them share `cli.main()`. Running as a file needs the parent directory on
sys.path so `import filebridge` resolves; `-m` and the launcher already set that
up.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):  # executed as a path, not with -m
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filebridge.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
