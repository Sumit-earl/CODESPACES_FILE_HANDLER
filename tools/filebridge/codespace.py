"""Codespaces awareness: forwarded URLs, port visibility, tunnel registration.

The visibility problem, precisely:

`gh cs ports visibility` does not touch the Codespaces REST API.  It resolves
the codespace's Dev Tunnel and then calls

    GET /tunnels/{tunnelId}/ports/{port}?includePorts=true

which **404s** unless a *port entity* exists in the tunnel.  Merely listening on
a port does not create one, so the command fails with "Not Found" while
`gh cs ports` (Codespaces API) happily lists the port.  That mismatch is why the
port looks public in one place and stays private in the browser.

The port entity is created when a tunnel client connects to it.  So the recipe
is: open a temporary `gh cs ports forward <port>:<local>`, wait for the port to
appear, flip visibility, then drop the forward.  The port record (and its
visibility) outlives the forward.

Everything here is best-effort and reports what actually happened: a banner must
never claim "public" when the flip failed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass

DEFAULT_DOMAIN = "app.github.dev"
VIS_VALUES = ("private", "public", "org", "none")


# --------------------------------------------------------------- environment


def in_codespace() -> bool:
    return bool(os.environ.get("CODESPACE_NAME"))


def codespace_name() -> str:
    return os.environ.get("CODESPACE_NAME", "")


def forwarded_url(port: int) -> str | None:
    name = codespace_name()
    if not name:
        return None
    domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", DEFAULT_DOMAIN)
    return f"https://{name}-{port}.{domain}"


def default_bind() -> str:
    # A forwarded port only works when the process listens on all interfaces.
    return "0.0.0.0" if in_codespace() else "127.0.0.1"


# ------------------------------------------------------------------ gh calls


def _gh(*args: str, timeout: float = 45.0) -> tuple[int, str, str]:
    if not shutil.which("gh"):
        return 127, "", "gh CLI not found on PATH"
    try:
        done = subprocess.run(
            ["gh", *args],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout.strip(), done.stderr.strip()


def list_ports(name: str | None = None) -> dict[int, str] | None:
    """sourcePort -> visibility, or None when we could not ask."""
    name = name or codespace_name()
    if not name:
        return None
    code, out, _ = _gh("cs", "ports", "-c", name, "--json", "sourcePort,visibility")
    if code != 0 or not out:
        return None
    try:
        rows = json.loads(out)
        return {int(row["sourcePort"]): str(row.get("visibility") or "") for row in rows}
    except (ValueError, KeyError, TypeError):
        return None


def _flip(port: int, visibility: str) -> tuple[bool, str]:
    code, out, err = _gh(
        "cs", "ports", "visibility", f"{port}:{visibility}", "-c", codespace_name()
    )
    detail = (err or out or "").strip().splitlines()
    return code == 0, (detail[-1] if detail else f"gh exited {code}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Forward:
    """A temporary `gh cs ports forward`, killed on exit.

    Its only job is to make the Dev Tunnels service create a port entity.
    """

    def __init__(self, port: int):
        self.port = port
        self.local = _free_port()
        self.process: subprocess.Popen | None = None

    def start(self) -> bool:
        try:
            self.process = subprocess.Popen(
                ["gh", "cs", "ports", "forward", f"{self.port}:{self.local}",
                 "-c", codespace_name()],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError:
            self.process = None
        return self.process is not None

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            self.process.wait(timeout=5)


# ------------------------------------------------------------------- results


@dataclass
class VisibilityResult:
    ok: bool
    requested: str
    actual: str | None
    detail: str = ""
    method: str = "none"      # probe | direct | forward | skipped | failed

    def describe(self) -> str:
        shown = self.actual or "unknown"
        if self.ok and self.method == "probe":
            return f"{shown} (already set)"
        if self.ok:
            return f"{shown}"
        return f"{shown} (wanted {self.requested}: {self.detail})"


def ensure_visibility(
    port: int,
    visibility: str,
    timeout: float = 30.0,
    log=None,
) -> VisibilityResult:
    """Make the forwarded port's real visibility match `visibility`."""
    say = log or (lambda kind, message: None)

    if visibility in ("", "none"):
        return VisibilityResult(True, visibility, None, "skipped", "skipped")
    if not in_codespace():
        return VisibilityResult(False, visibility, None,
                                "not inside a codespace (CODESPACE_NAME unset)", "skipped")
    if not shutil.which("gh"):
        return VisibilityResult(False, visibility, None, "gh CLI not found on PATH", "failed")

    ports = list_ports()
    current = (ports or {}).get(port)

    if current == visibility:
        return VisibilityResult(True, visibility, current, "already set", "probe")
    if visibility == "private" and ports is not None and current != "public":
        # Not registered in the tunnel (or already private) = nothing to do.
        return VisibilityResult(True, "private", current or "private",
                                "already private", "probe")

    # Cheap attempt first: works when a port entity already exists.
    ok, detail = _flip(port, visibility)
    if ok:
        actual = (list_ports() or {}).get(port) or visibility
        if actual == visibility:
            return VisibilityResult(True, visibility, actual, "", "direct")

    lowered = detail.lower()
    if any(word in lowered for word in
           ("auth", "scope", "permission", "forbidden", "unauthorized")):
        # A credentials problem: registering the port cannot fix it.
        return VisibilityResult(False, visibility, current,
                                f"{detail} (run: gh auth refresh -s codespace)", "failed")

    # The port is not registered with the tunnel service yet.  Register it by
    # opening a temporary forward, then retry until the flip sticks.
    say("info", f"port {port} is not registered with the tunnel yet; "
                f"opening a temporary forward to register it")
    forward = _Forward(port)
    started = forward.start()
    deadline = time.monotonic() + max(5.0, timeout)
    final_detail = detail
    try:
        while time.monotonic() < deadline:
            time.sleep(1.5)
            ok, detail = _flip(port, visibility)
            final_detail = detail
            if ok:
                actual = (list_ports() or {}).get(port) or visibility
                if actual == visibility:
                    return VisibilityResult(True, visibility, actual, "", "forward")
                break
            if "not found" not in detail.lower():
                break
    finally:
        forward.stop()

    actual = (list_ports() or {}).get(port)
    if actual == visibility:
        return VisibilityResult(True, visibility, actual, "", "forward")

    if visibility == "private" and actual != "public":
        # Private is the default anyway; nothing to break if the flip failed.
        return VisibilityResult(True, "private", actual or "private",
                                "port already private", "probe")

    reason = final_detail or "timed out waiting for the port to register"
    if not started:
        reason += " (could not start gh cs ports forward)"
    return VisibilityResult(False, visibility, actual, reason, "failed")


# ------------------------------------------------------------------- helpers


def manual_snippet(port: int, visibility: str) -> str:
    """The exact three commands, for when the automatic path cannot run."""
    local = port + 10000
    return (
        f'gh cs ports forward {port}:{local} -c "$CODESPACE_NAME" & f=$!; sleep 4; '
        f'gh cs ports visibility {port}:{visibility} -c "$CODESPACE_NAME"; '
        f"kill $f"
    )


def visibility_command(port: int, visibility: str) -> str:
    return f'gh cs ports visibility {port}:{visibility} -c "$CODESPACE_NAME"'


def ssh_hint(port: int) -> str:
    return (
        f'gh cs ssh -c "$CODESPACE_NAME" -- -N -L {port}:localhost:{port}'
        f"   # then use http://localhost:{port}"
    )
