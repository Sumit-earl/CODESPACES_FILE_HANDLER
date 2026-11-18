"""Interactive setup layer.

Choice prompts are a real arrow-key menu (↑/↓/j/k, Enter selects, digits 1..N
select directly, 0 or Enter picks the default).  When stdin/stdout is not a TTY
(or on Windows) it falls back to a numbered prompt, so pipes and CI still work.

Every question has a default, so holding Enter produces a safe configuration.
"""

from __future__ import annotations

import getpass
import os
import select
import shutil
import sys

from . import codespace
from .config import Config, generate_token, resolve_token
from .util import Style, human_bytes, parse_size

NO_AUTH = "none"


# ------------------------------------------------------------------ terminal


def _style() -> Style:
    return Style(sys.stdout.isatty() and sys.stdin.isatty())


def _tty() -> bool:
    try:
        return (
            os.name == "posix"
            and sys.stdin.isatty()
            and sys.stdout.isatty()
        )
    except (AttributeError, ValueError):
        return False


def _truncate(text: str, width: int) -> str:
    if width <= 1 or len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _read_key(fd: int) -> str:
    """One keystroke, decoded just enough for menu navigation."""
    char = os.read(fd, 1)
    if not char:
        return "eof"
    if char == b"\x1b":
        ready, _, _ = select.select([fd], [], [], 0.03)
        sequence = os.read(fd, 2) if ready else b""
        return {
            b"[A": "up", b"OA": "up",
            b"[B": "down", b"OB": "down",
            b"[C": "right", b"OC": "right",
            b"[D": "left", b"OD": "left",
            b"[H": "home", b"OH": "home",
            b"[F": "end", b"OF": "end",
        }.get(sequence, "esc")
    if char in (b"\r", b"\n"):
        return "enter"
    if char == b"\x03":
        raise KeyboardInterrupt
    if char in (b"\x7f", b"\x08"):
        return "backspace"
    if char.isdigit():
        return char.decode()
    return "other"


class _Menu:
    """Redraw-in-place list widget."""

    def __init__(self, prompt: str, choices: list[str], default: str):
        self.prompt = prompt
        self.choices = choices
        self.index = choices.index(default) if default in choices else 0
        self.default = default
        self.style = _style()
        self.lines = len(choices) + 2
        self.width = max(40, shutil.get_terminal_size((80, 24)).columns)

    def _draw(self, first: bool) -> None:
        style = self.style
        out = sys.stdout
        if not first:
            out.write(f"\x1b[{self.lines}A")
        out.write("\x1b[2K" + style.cyan("?") + " "
                  + _truncate(self.prompt, self.width - 3) + "\n")
        for position, choice in enumerate(self.choices):
            label = _truncate(choice, self.width - 8)
            if position == self.index:
                out.write("\x1b[2K" + style.cyan("  ❯ ") + style.bold(label) + "\n")
            else:
                out.write("\x1b[2K" + "    " + style.dim(label) + "\n")
        hint = f"  ↑/↓ move · 1-{len(self.choices)} select · 0/Enter default · Esc accept"
        out.write("\x1b[2K" + style.dim(_truncate(hint, self.width - 1)) + "\n")
        out.flush()

    def _collapse(self, picked: str) -> None:
        style = self.style
        out = sys.stdout
        out.write(f"\x1b[{self.lines}A")
        for _ in range(self.lines):
            out.write("\x1b[2K\n")
        out.write(f"\x1b[{self.lines}A")
        out.write(f"{style.cyan('?')} {_truncate(self.prompt, self.width - 3)} "
                  f"{style.green('→')} {style.bold(picked)}\n")
        out.flush()

    def run(self) -> str:
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            self._draw(first=True)
            while True:
                key = _read_key(fd)
                if key in ("up", "left"):
                    self.index = (self.index - 1) % len(self.choices)
                elif key in ("down", "right"):
                    self.index = (self.index + 1) % len(self.choices)
                elif key == "home":
                    self.index = 0
                elif key == "end":
                    self.index = len(self.choices) - 1
                elif key in ("enter", "esc", "eof"):
                    break
                elif key.isdigit():
                    digit = int(key)
                    if digit == 0:
                        self.index = self.choices.index(self.default)
                        break
                    if 1 <= digit <= len(self.choices):
                        self.index = digit - 1
                        break
                self._draw(first=False)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        picked = self.choices[self.index]
        self._collapse(picked)
        return picked


# ------------------------------------------------------------------ prompts


def _ask(prompt: str, default: str = "", style: Style | None = None) -> str:
    style = style or _style()
    suffix = f" {style.dim('[' + default + ']')}" if default else ""
    try:
        answer = input(f"{style.cyan('?')} {prompt}{suffix}: ").strip()
    except EOFError:      # piped/CI input ran out: keep going with defaults
        print()
        return default
    return answer or default


def _ask_choice(prompt: str, choices: list[str], default: str) -> str:
    if default not in choices:
        default = choices[0]
    if _tty():
        try:
            return _Menu(prompt, choices, default).run()
        except (OSError, ValueError):
            pass
    return _ask_choice_plain(prompt, choices, default)


def _ask_choice_plain(prompt: str, choices: list[str], default: str) -> str:
    style = _style()
    print(f"{style.cyan('?')} {prompt}")
    for index, choice in enumerate(choices, 1):
        marker = style.green(">") if choice == default else " "
        print(f"   {marker} {index}) {choice}")
    while True:
        try:
            answer = input(
                f"  choose 1-{len(choices)} {style.dim('(0 = default)')}: "
            ).strip()
        except EOFError:
            print()
            return default
        if not answer or answer == "0":
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        if answer in choices:
            return answer
        print("  pick a number from the list")


def _ask_bool(prompt: str, default: bool) -> bool:
    return _ask_choice(prompt, ["no", "yes"], "yes" if default else "no") == "yes"


def _ask_int(prompt: str, default: int, low: int, high: int) -> int:
    while True:
        try:
            value = int(_ask(prompt, str(default)))
        except ValueError:
            print("  enter a number")
            continue
        if low <= value <= high:
            return value
        print(f"  must be {low}..{high}")


def _ask_size(prompt: str, default: int) -> int:
    while True:
        raw = _ask(prompt, human_bytes(default).replace(" ", ""))
        try:
            return parse_size(raw)
        except ValueError as exc:
            print(f"  {exc}")


# --------------------------------------------------------------------- flow


def run(cfg: Config) -> Config:
    style = _style()
    print()
    print(style.bold("filebridge setup") + style.dim("  — Enter accepts the default"))
    if codespace.in_codespace():
        print(style.dim(f"  codespace: {codespace.codespace_name()}"))
    print()

    mode_labels = {
        "both": "both (upload + download)",
        "upload": "upload only",
        "download": "download only",
    }
    cfg.mode = {label: key for key, label in mode_labels.items()}[
        _ask_choice(
            "What should this server allow?",
            list(mode_labels.values()),
            mode_labels.get(cfg.mode, mode_labels["both"]),
        )
    ]

    if cfg.can_download():
        cfg.root = _ask(
            "Directory to share for downloads",
            str(cfg.root_path()) if cfg.root not in ("", ".") else os.getcwd(),
        )
    if cfg.can_upload():
        cfg.inbox = _ask(
            "Directory to receive uploads",
            cfg.inbox or str(cfg.root_path() / "inbox"),
        )

    print()
    print(style.bold("Access"))
    auth_mode = _ask_choice(
        "How should clients authenticate?",
        ["auto-token (generated now)", "my-own-token", "no-auth"],
        "auto-token (generated now)" if cfg.token in ("", "auto") else (
            "no-auth" if cfg.token == NO_AUTH else "my-own-token"
        ),
    )
    if auth_mode == "no-auth":
        cfg.token = NO_AUTH
        print(style.yellow("  ! no token: anyone who can reach the port can read/write"))
    elif auth_mode == "my-own-token":
        while True:
            try:
                secret = getpass.getpass("  paste token (hidden): ").strip()
            except (EOFError, OSError):
                secret = input("  token: ").strip()
            if len(secret) >= 8 and not any(ch.isspace() for ch in secret):
                cfg.token = secret
                break
            print("  use at least 8 characters and no spaces")
    else:
        cfg.token = generate_token()

    if codespace.in_codespace():
        cfg.visibility = _ask_choice(
            "Port visibility in GitHub",
            ["private", "public", "org", "none"],
            cfg.visibility if cfg.visibility in ("private", "public", "org", "none")
            else "private",
        )
        if cfg.visibility == "public":
            print(style.yellow("  ! public means anyone with the URL reaches this port"))
            if cfg.token == NO_AUTH and not _ask_bool(
                "Really run with no token on a public port?", False
            ):
                cfg.visibility = "private"
    else:
        cfg.visibility = "none"

    print()
    print(style.bold("Limits"))
    cfg.port = _ask_int("Port (0 = pick a free one)", cfg.port, 0, 65535)
    if _ask_bool(f"Tune chunk size / parallelism / limits? (now {cfg.limits_summary()})", False):
        cfg.chunk_size = _ask_size("Chunk size (64KB..64MB)", cfg.chunk_size)
        cfg.parallel = _ask_int("Parallel transfers", cfg.parallel, 1, 32)
        cfg.max_file = _ask_size("Max size per file", cfg.max_file)
        cfg.max_total = _ask_size("Max total upload quota", cfg.max_total)
        cfg.on_exist = _ask_choice(
            "If the destination file exists", ["rename", "overwrite", "fail"], cfg.on_exist
        )
        cfg.include_hidden = _ask_bool(
            "Serve dotfiles (.env, .git, …) for download?", cfg.include_hidden
        )

    print()
    cfg.open_browser = _ask_bool("Open the URL when the server is up?", cfg.open_browser)
    cfg.save = _ask_bool("Remember these answers for next time?", True)

    effective = resolve_token(cfg)
    print()
    print(style.bold("Summary"))
    print(f"  mode        {cfg.mode}")
    if cfg.can_download():
        print(f"  read        {cfg.root_path()}")
    if cfg.can_upload():
        print(f"  write       {cfg.inbox_path()}")
    auth = f"token ({len(effective)} chars)" if effective else style.yellow("none")
    print(f"  auth        {auth}")
    print(f"  visibility  {cfg.visibility}")
    print(f"  hidden      {'served' if cfg.include_hidden else 'excluded'}")
    print(f"  port        {cfg.port or 'auto'}")
    print()
    if not _ask_bool("Start now?", True):
        print("  aborted")
        raise SystemExit(1)
    return cfg
