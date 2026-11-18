"""Small shared helpers: sizes, paths, hashing, atomic JSON."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- sizes / fmt

_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}


def parse_size(value) -> int:
    """'4M' / '512kb' / '2G' / 1048576 -> bytes."""
    if isinstance(value, int):
        if value < 0:
            raise ValueError("size must be >= 0")
        return value
    text = str(value).strip().lower().replace("_", "").replace(" ", "")
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)([a-z]*)", text)
    if not match:
        raise ValueError(f"cannot parse size: {value!r}")
    number, unit = float(match.group(1)), match.group(2)
    if unit not in _UNITS:
        raise ValueError(f"unknown size unit: {unit!r}")
    return int(number * _UNITS[unit])


def human_bytes(number: float) -> str:
    number = float(number)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024 or unit == "TB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, seconds = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def now() -> float:
    return time.time()


# ------------------------------------------------------------------- json I/O


def read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def atomic_write_json(path: Path, payload, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.monotonic_ns()}.tmp"
    tmp = path.with_name(unique)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


# ------------------------------------------------------------------- paths


def safe_rel(rel: str) -> str:
    """Normalise a client supplied relative path, reject anything escaping."""
    rel = (rel or "").replace("\\", "/").strip()
    if rel.startswith("/"):
        raise ValueError("absolute paths are not allowed")
    while rel.startswith("./"):
        rel = rel[2:]
    parts = [part for part in rel.split("/") if part not in ("", ".")]
    for part in parts:
        if part == "..":
            raise ValueError("path escapes root: '..'")
        if "\x00" in part:
            raise ValueError("path contains NUL byte")
        if len(part) > 255:
            raise ValueError("path component too long")
    return "/".join(parts)


def safe_join(root: Path, rel: str) -> Path:
    """Join `rel` under `root`, refusing symlink escapes."""
    clean = safe_rel(rel)
    root = Path(root).resolve()
    target = (root / clean).resolve() if clean else root
    if target != root and root not in target.parents:
        raise ValueError("path escapes root")
    return target


def unique_path(path: Path) -> Path:
    """`a.txt` -> `a (1).txt`, `a (2).txt`, ..."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("too many name collisions")


# ------------------------------------------------------------------ hashing


def sha256_file(path: Path, buffer_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(buffer_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def etag_for(stat_result: os.stat_result) -> str:
    return f'"{stat_result.st_size:x}-{int(stat_result.st_mtime):x}"'


# ------------------------------------------------------------------ console


class Style:
    """Tiny ANSI helper that degrades to plain text when not a TTY."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self._wrap("1", text)

    def dim(self, text: str) -> str:
        return self._wrap("2", text)

    def green(self, text: str) -> str:
        return self._wrap("32", text)

    def yellow(self, text: str) -> str:
        return self._wrap("33", text)

    def red(self, text: str) -> str:
        return self._wrap("31", text)

    def cyan(self, text: str) -> str:
        return self._wrap("36", text)
