"""Configuration resolution: CLI flags > environment > config file > defaults.

The config file is `.filebridge/config.json` next to wherever you run the
command.  `./filebridge setup` writes it so repeat runs need zero answers.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .util import atomic_write_json, human_bytes, parse_size, read_json

CONFIG_DIR = ".filebridge"
CONFIG_FILE = "config.json"
STATE_DIR = ".filebridge"

DEFAULT_PORT = 5555
DEFAULT_CHUNK = 4 * 1024 * 1024          # 4 MiB: well under proxy body limits
DEFAULT_PARALLEL = 3                     # browsers cap ~6 conns/origin
DEFAULT_MAX_FILE = 16 * 1024**3           # 16 GiB
DEFAULT_MAX_TOTAL = 64 * 1024**3          # 64 GiB reserved
DEFAULT_TTL_HOURS = 24.0
DEFAULT_IO_TIMEOUT = 120.0

MODES = ("both", "upload", "download")
VISIBILITIES = ("private", "public", "org", "none")
ON_EXIST = ("rename", "overwrite", "fail")


@dataclass
class Config:
    mode: str = "both"
    root: str = "."                      # directory served for download (cwd)
    inbox: str = ""                      # where uploads land (default <root>/inbox)
    state: str = STATE_DIR               # sessions + .part files
    bind: str = ""                       # "" = auto (0.0.0.0 in codespaces)
    port: int = DEFAULT_PORT
    token: str = ""                      # "" = no auth, "auto" = generate
    visibility: str = "private"
    chunk_size: int = DEFAULT_CHUNK
    parallel: int = DEFAULT_PARALLEL
    max_file: int = DEFAULT_MAX_FILE
    max_total: int = DEFAULT_MAX_TOTAL
    on_exist: str = "rename"
    session_ttl: float = DEFAULT_TTL_HOURS * 3600
    io_timeout: float = DEFAULT_IO_TIMEOUT
    cors: str = ""
    allow_delete: bool = False
    include_hidden: bool = False          # serve dotfiles/dot-dirs
    open_browser: bool = False
    interactive: bool = False
    save: bool = False
    quiet: bool = False
    verbose: bool = False
    color: bool = True

    # ---- derived -------------------------------------------------------
    def root_path(self) -> Path:
        return Path(self.root).expanduser().resolve()

    def inbox_path(self) -> Path:
        inbox = self.inbox or str(Path(self.root) / "inbox")
        return Path(inbox).expanduser().resolve()

    def state_path(self) -> Path:
        return Path(self.state).expanduser().resolve()

    def can_upload(self) -> bool:
        return self.mode in ("both", "upload")

    def can_download(self) -> bool:
        return self.mode in ("both", "download")

    def limits_summary(self) -> str:
        return (
            f"{human_bytes(self.chunk_size)} chunks · "
            f"{self.parallel} parallel · "
            f"{human_bytes(self.max_file)}/file · "
            f"{human_bytes(self.max_total)} total"
        )


# --------------------------------------------------------------- config file


def config_path(cwd: Path | None = None) -> Path:
    return (cwd or Path.cwd()) / CONFIG_DIR / CONFIG_FILE


def load_file(cwd: Path | None = None) -> dict[str, Any]:
    return read_json(config_path(cwd), {})


def save_file(cfg: Config, cwd: Path | None = None) -> Path:
    payload = asdict(cfg)
    # never persist passwords from a one-off session unless asked
    path = config_path(cwd)
    atomic_write_json(path, payload)
    return path


def apply_file(cfg: Config, data: dict[str, Any]) -> Config:
    known = {field.name for field in fields(Config)}
    for key, value in data.items():
        if key not in known or value is None:
            continue
        setattr(cfg, key, value)
    return cfg


# --------------------------------------------------------------- environment


def apply_env(cfg: Config) -> Config:
    mapping = {
        "FB_MODE": ("mode", str),
        "FB_ROOT": ("root", str),
        "FB_INBOX": ("inbox", str),
        "FB_STATE": ("state", str),
        "FB_BIND": ("bind", str),
        "FB_PORT": ("port", int),
        "FB_TOKEN": ("token", str),
        "FB_VISIBILITY": ("visibility", str),
        "FB_CHUNK_SIZE": ("chunk_size", parse_size),
        "FB_PARALLEL": ("parallel", int),
        "FB_MAX_FILE": ("max_file", parse_size),
        "FB_MAX_TOTAL": ("max_total", parse_size),
        "FB_ON_EXIST": ("on_exist", str),
        "FB_CORS": ("cors", str),
    }
    for env_key, (field, caster) in mapping.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            setattr(cfg, field, caster(raw))
        except (TypeError, ValueError):
            pass
    return cfg


# ------------------------------------------------------------------ token


def generate_token() -> str:
    return secrets.token_urlsafe(24)  # 32 chars, URL safe


def resolve_token(cfg: Config, explicit_none: bool = False) -> str:
    if explicit_none or cfg.token == "none":
        return ""
    if cfg.token in ("", "auto"):
        cfg.token = generate_token()
        return cfg.token
    return cfg.token


def validate(cfg: Config) -> None:
    if cfg.mode not in MODES:
        raise SystemExit(f"mode must be one of {MODES}")
    if cfg.visibility not in VISIBILITIES:
        raise SystemExit(f"visibility must be one of {VISIBILITIES}")
    if cfg.on_exist not in ON_EXIST:
        raise SystemExit(f"on-exist must be one of {ON_EXIST}")
    if not (0 <= cfg.port <= 65535):
        raise SystemExit("port must be 0..65535 (0 = pick a free one)")
    if cfg.chunk_size < 64 * 1024:
        raise SystemExit("chunk-size must be at least 64KB")
    if cfg.chunk_size > 64 * 1024 * 1024:
        raise SystemExit("chunk-size above 64MB is pointless and hostile to the proxy")
    if cfg.parallel < 1:
        raise SystemExit("parallel must be >= 1")
    if cfg.token not in ("", "auto", "none"):
        if len(cfg.token) < 8 or any(ch.isspace() for ch in cfg.token):
            raise SystemExit(
                "token must be at least 8 characters with no spaces "
                "(use --token auto to generate one)"
            )

