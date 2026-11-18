"""Keyboard behaviour of the wizard menu, driven through a real pty.

Skips itself when the platform has no pty (the plain numbered fallback is
covered by the non-tty test at the bottom).
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2]
PROGRAM = f"""
import sys
sys.path.insert(0, {str(TOOLS)!r})
from filebridge.wizard import _ask_choice
picked = _ask_choice("pick one", ["alpha", "bravo", "charlie"], "alpha")
print("PICK=" + picked, flush=True)
"""


def _run_pty(keys: bytes, timeout: float = 10.0) -> bytes:
    try:
        import pty
    except ImportError:  # pragma: no cover
        raise unittest.SkipTest("no pty on this platform")

    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", PROGRAM],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    output = b""
    deadline = time.time() + timeout
    try:
        # wait for the menu to be drawn before typing
        while b"pick one" not in output and time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                output += os.read(master, 4096)
        os.write(master, keys)
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if b"PICK=" in output and b"PICK=" in output[output.index(b"PICK="):]:
                    if process.poll() is not None:
                        break
            elif process.poll() is not None:
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        try:
            os.close(master)
        except OSError:
            pass
    return output


class ArrowKeyTests(unittest.TestCase):
    def assertPick(self, keys: bytes, expected: str) -> None:
        output = _run_pty(keys)
        self.assertIn(f"PICK={expected}".encode(), output, output[-400:])

    def test_enter_takes_default(self):
        self.assertPick(b"\r", "alpha")

    def test_down_down_enter(self):
        self.assertPick(b"\x1b[B\x1b[B\r", "charlie")

    def test_up_wraps_to_last(self):
        self.assertPick(b"\x1b[A\r", "charlie")

    def test_digit_selects_immediately(self):
        self.assertPick(b"2", "bravo")

    def test_zero_selects_default(self):
        self.assertPick(b"0", "alpha")

    def test_digit_three(self):
        self.assertPick(b"3", "charlie")


class PlainFallbackTests(unittest.TestCase):
    """No tty: the numbered prompt must still accept 0 and N."""

    def run_piped(self, stdin: str) -> str:
        return subprocess.run(
            [sys.executable, "-c", PROGRAM],
            input=stdin, capture_output=True, text=True, timeout=10,
            env={**os.environ, "NO_COLOR": "1"},
        ).stdout

    def test_enter_uses_default(self):
        self.assertIn("PICK=alpha", self.run_piped("\n"))

    def test_number_selects(self):
        self.assertIn("PICK=bravo", self.run_piped("2\n"))

    def test_zero_selects_default(self):
        self.assertIn("PICK=charlie", self.run_piped("3\n"))
        self.assertIn("PICK=alpha", self.run_piped("0\n"))

    def test_reprompts_on_garbage(self):
        self.assertIn("PICK=bravo", self.run_piped("9\nnope\n2\n"))


if __name__ == "__main__":
    unittest.main()
