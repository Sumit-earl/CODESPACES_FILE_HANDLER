"""FileBridge - resumable bulk file transfer over a forwarded port.

The whole point of this package is to keep bulk bytes off the IDE's control
channel (VS Code Web sends uploads as one giant RPC frame on the workbench
WebSocket, which is what freezes the UI).  Everything here is chunked,
resumable, checksummed and restartable.

Stdlib only on purpose: `python3 ./filebridge` must work in a bare Codespace
with no package install and no build step.  Nothing is fetched at startup - not
packages, not fonts, not updates.
"""

import sys

VERSION = "0.1.0"
APP = "filebridge"

if sys.version_info < (3, 9):
    raise SystemExit(
        f"filebridge needs Python 3.9 or newer (this is {sys.version.split()[0]}).\n"
        "There are no packages to install - just point it at a newer python3."
    )
