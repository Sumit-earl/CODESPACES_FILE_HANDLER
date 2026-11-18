"""Resumable upload sessions.

A session is a sparse `.part` file plus a set of completed chunk indexes.  Any
chunk can arrive in any order, many times, from any client; the file is only
promoted to its final name once every chunk is present and checksummed.

State lives in `<state>/sessions.json` so a codespace suspend, a crash or a
Ctrl-C does not throw away 4 GB of progress.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config
from .util import (
    atomic_write_json,
    now,
    safe_join,
    unique_path,
)


class StoreError(Exception):
    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class Session:
    id: str
    path: str
    size: int
    chunk_size: int
    created: float
    updated: float
    sha256: str = ""
    mtime: float = 0.0
    mode: int = 0o644
    chunks: dict[str, str] = field(default_factory=dict)
    committed_idx: int = 0
    state: str = "open"

    @property
    def nchunks(self) -> int:
        if self.size == 0:
            return 0
        return (self.size + self.chunk_size - 1) // self.chunk_size

    def chunk_bounds(self, index: int) -> tuple[int, int]:
        start = index * self.chunk_size
        end = min(start + self.chunk_size, self.size) - 1
        return start, end

    @property
    def committed(self) -> int:
        return min(self.committed_idx * self.chunk_size, self.size)

    def missing_ranges(self) -> list[list[int]]:
        ranges: list[list[int]] = []
        for index in range(self.nchunks):
            if str(index) in self.chunks:
                continue
            start, end = self.chunk_bounds(index)
            if ranges and ranges[-1][1] + 1 == start:
                ranges[-1][1] = end
            else:
                ranges.append([start, end])
        return ranges

    def done(self) -> bool:
        return len(self.chunks) >= self.nchunks

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def public(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "size": self.size,
            "chunk_size": self.chunk_size,
            "sha256": self.sha256,
            "mtime": self.mtime,
            "state": self.state,
            "committed": self.committed,
            "chunks_done": len(self.chunks),
            "chunks_total": self.nchunks,
            "missing": self.missing_ranges(),
            "created": self.created,
            "updated": self.updated,
        }


class Store:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.state_path()
        self.tmp = self.base / "tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.inbox = cfg.inbox_path()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._meta = threading.RLock()
        self._write_lock = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}
        self._sessions: dict[str, Session] = {}
        # JSON-ready copy of each session, refreshed while its lock is held.
        # `_persist` dumps only from here, so it never needs another session's
        # lock (that was a deadlock: persist/Lock ordering).
        self._cache: dict[str, dict] = {}
        self._recent: list[dict] = []
        self._load()
        self.cleanup()

    # ------------------------------------------------------------- state

    def _state_file(self) -> Path:
        return self.base / "sessions.json"

    def _load(self) -> None:
        from .util import read_json

        data = read_json(self._state_file(), {})
        for raw in data.get("sessions", []):
            try:
                session = Session.from_dict(raw)
            except (TypeError, ValueError):
                continue
            part = self._part_path(session.id)
            if not part.exists():
                continue
            if part.stat().st_size != session.size:
                try:
                    with open(part, "r+b") as handle:
                        handle.truncate(session.size)
                except OSError:
                    continue
            self._sessions[session.id] = session
            self._cache[session.id] = session.to_dict()
        self._recent = list(data.get("recent", []))[:25]

    def _cache_put(self, session: Session) -> None:
        with self._meta:
            self._cache[session.id] = session.to_dict()

    def _cache_drop(self, session_id: str) -> None:
        with self._meta:
            self._cache.pop(session_id, None)

    def _persist(self) -> None:
        # Snapshot under the meta lock only: no session locks, no lock cycles.
        with self._meta:
            payload = {
                "version": 1,
                "sessions": list(self._cache.values()),
                "recent": list(self._recent),
            }
        with self._write_lock:
            atomic_write_json(self._state_file(), payload)

    def _lock_for(self, session_id: str) -> threading.RLock:
        with self._meta:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[session_id] = lock
            return lock

    def _part_path(self, session_id: str) -> Path:
        return self.tmp / f"{session_id}.part"

    # ------------------------------------------------------------- helpers

    def _reserved(self) -> int:
        with self._meta:
            return sum(s.size for s in self._sessions.values())

    def _free_space(self) -> int:
        try:
            return shutil.disk_usage(self.tmp).free
        except OSError:
            return 0

    def _record_recent(self, entry: dict) -> None:
        with self._meta:
            self._recent.insert(0, entry)
            del self._recent[25:]

    # ------------------------------------------------------------- public

    def get(self, session_id: str) -> Session:
        with self._meta:
            session = self._sessions.get(session_id)
        if session is None:
            raise StoreError(404, "unknown upload session")
        return session

    def list(self, include_done: bool = False) -> list[dict]:
        with self._meta:
            sessions = list(self._sessions.values())
        rows = []
        for session in sessions:
            with self._lock_for(session.id):
                rows.append(session.public())
        rows.sort(key=lambda row: row["created"], reverse=True)
        return rows

    def recent(self) -> list[dict]:
        with self._meta:
            return list(self._recent)

    def stats(self) -> dict:
        with self._meta:
            sessions = list(self._sessions.values())
        return {
            "open_sessions": len(sessions),
            "reserved": sum(s.size for s in sessions),
            "free": self._free_space(),
        }

    # -------------------------------------------------------------- create

    def create(
        self,
        path: str,
        size: int,
        sha256: str = "",
        chunk_size: int | None = None,
        mtime: float = 0.0,
        mode: int = 0o644,
    ) -> tuple[Session, bool]:
        cfg = self.cfg
        if size < 0:
            raise StoreError(400, "size must be >= 0")
        if size > cfg.max_file:
            raise StoreError(
                413, f"file too large (limit {cfg.max_file} bytes)", limit=cfg.max_file
            )
        target = safe_join(self.inbox, path)
        if target == self.inbox:
            raise StoreError(400, "path must name a file")
        if not target.name:
            raise StoreError(400, "invalid path")

        chunk_size = int(chunk_size or cfg.chunk_size)
        if chunk_size < 64 * 1024 or chunk_size > 64 * 1024 * 1024:
            raise StoreError(400, "chunk_size must be 64KB..64MB")

        # Idempotent create: same destination + size + digest (or same mtime when
        # the browser could not hash a huge file) is the same job, so a re-drop
        # of the same file resumes instead of starting over.
        with self._meta:
            for existing in self._sessions.values():
                if existing.path != path or existing.size != size:
                    continue
                same_digest = bool(sha256) and existing.sha256 == sha256
                digest_unknown = not sha256 or not existing.sha256
                same_mtime = (
                    bool(mtime) and bool(existing.mtime)
                    and abs(existing.mtime - mtime) < 2
                )
                if same_digest or (digest_unknown and (same_mtime or not mtime)):
                    return existing, False

        reserved = self._reserved() + size
        if reserved > cfg.max_total:
            raise StoreError(
                507,
                "upload quota exhausted",
                limit=cfg.max_total,
                reserved=reserved,
            )
        needed = int(size * 1.05) + (32 * 1024 * 1024)
        if self._free_space() < needed:
            raise StoreError(
                507,
                "not enough free disk space on the codespace",
                free=self._free_space(),
                needed=needed,
            )

        session_id = "u_" + secrets.token_hex(8)
        part = self._part_path(session_id)
        try:
            with open(part, "wb") as handle:
                handle.truncate(size)
        except OSError as exc:
            raise StoreError(507, f"cannot allocate staging file: {exc}") from exc

        stamp = now()
        session = Session(
            id=session_id,
            path=path,
            size=size,
            chunk_size=chunk_size,
            created=stamp,
            updated=stamp,
            sha256=sha256,
            mtime=mtime or 0.0,
            mode=int(mode) & 0o777 or 0o644,
        )
        with self._meta:
            self._sessions[session_id] = session
        self._cache_put(session)
        self._persist()
        return session, True

    # --------------------------------------------------------------- write

    def write_chunk(
        self,
        session_id: str,
        start: int,
        end: int,
        total: int,
        length: int,
        reader,
        expect_sha: str = "",
    ) -> Session:
        session = self.get(session_id)
        with self._lock_for(session_id):
            if session.state != "open":
                raise StoreError(409, "session is not open")
            if total != session.size:
                raise StoreError(
                    400, "total size does not match session", expected=session.size
                )
            if start < 0 or end < start or start % session.chunk_size != 0:
                raise StoreError(416, "bad chunk offset")
            index = start // session.chunk_size
            if index >= max(session.nchunks, 1):
                raise StoreError(416, "chunk index out of range")
            want_start, want_end = session.chunk_bounds(index)
            if (start, end) != (want_start, want_end):
                raise StoreError(
                    416,
                    "chunk does not match session chunking",
                    expected=[want_start, want_end],
                )
            if length != end - start + 1:
                raise StoreError(400, "content-length does not match content-range")

            existing = session.chunks.get(str(index))
            if existing is not None:
                self._drain(reader, length)
                if expect_sha and expect_sha.lower() != existing:
                    raise StoreError(409, "chunk already stored with different digest")
                return session

            digest = hashlib.sha256()
            remaining = length
            part = self._part_path(session_id)
            with open(part, "r+b") as handle:
                handle.seek(start)
                while remaining > 0:
                    block = reader.read(min(262144, remaining))
                    if not block:
                        raise StoreError(400, "client closed before chunk was complete")
                    digest.update(block)
                    handle.write(block)
                    remaining -= len(block)
                handle.flush()
                os.fsync(handle.fileno())

            calculated = digest.hexdigest()
            if expect_sha and calculated != expect_sha.lower():
                # Region now holds bad bytes, but the index is *not* marked done,
                # so a retry overwrites it.  Never advance past a bad chunk.
                raise StoreError(
                    400,
                    "chunk checksum mismatch",
                    expected=expect_sha.lower(),
                    actual=calculated,
                )

            session.chunks[str(index)] = calculated
            session.updated = now()
            while (
                session.committed_idx < session.nchunks
                and str(session.committed_idx) in session.chunks
            ):
                session.committed_idx += 1
            self._cache_put(session)
            self._persist()
            return session

    def _drain(self, reader, length: int) -> None:
        remaining = length
        while remaining > 0:
            block = reader.read(min(262144, remaining))
            if not block:
                raise StoreError(400, "client closed before body was drained")
            remaining -= len(block)

    # ------------------------------------------------------------ finalize

    def finalize(self, session_id: str, expect_sha: str = "") -> dict:
        session = self.get(session_id)
        with self._lock_for(session_id):
            missing = session.missing_ranges()
            if missing:
                raise StoreError(
                    409,
                    "upload incomplete",
                    missing=missing,
                    committed=session.committed,
                )
            part = self._part_path(session_id)
            if not part.exists():
                raise StoreError(410, "staging file disappeared")
            if part.stat().st_size != session.size:
                raise StoreError(409, "staging file size mismatch")

            if expect_sha and session.sha256 and expect_sha.lower() != session.sha256:
                raise StoreError(400, "whole-file digest does not match session")
            if expect_sha:
                actual = self._hash_file(part)
                if actual != expect_sha.lower():
                    raise StoreError(
                        400,
                        "whole-file checksum mismatch",
                        expected=expect_sha.lower(),
                        actual=actual,
                    )

            target = safe_join(self.inbox, session.path)
            if target == self.inbox:
                raise StoreError(400, "path must name a file")
            if self.cfg.on_exist == "fail" and target.exists():
                raise StoreError(409, "destination exists")
            if self.cfg.on_exist == "rename":
                target = unique_path(target)
            target.parent.mkdir(parents=True, exist_ok=True)

            os.replace(part, target)
            if session.mtime:
                try:
                    os.utime(target, (session.mtime, session.mtime))
                except OSError:
                    pass
            try:
                os.chmod(target, session.mode)
            except OSError:
                pass

            with self._meta:
                self._sessions.pop(session_id, None)
                self._locks.pop(session_id, None)
            self._cache_drop(session_id)
            self._record_recent(
                {
                    "kind": "upload",
                    "path": str(target.relative_to(self.inbox)),
                    "size": session.size,
                    "at": now(),
                }
            )
            self._persist()
            result = {
                "ok": True,
                "id": session_id,
                "path": str(target.relative_to(self.inbox)),
                "size": session.size,
            }
            # Where the file is reachable through the download API, when the
            # inbox lives under the served root (it normally does).
            root = self.cfg.root_path()
            try:
                if target == root or root in target.parents:
                    result["root_path"] = target.relative_to(root).as_posix()
            except ValueError:
                pass
            return result

    def delete(self, session_id: str) -> None:
        with self._lock_for(session_id):
            with self._meta:
                session = self._sessions.pop(session_id, None)
                self._locks.pop(session_id, None)
            if session is None:
                raise StoreError(404, "unknown upload session")
            self._cache_drop(session_id)
            try:
                self._part_path(session_id).unlink()
            except OSError:
                pass
        self._persist()

    # ------------------------------------------------------------- housekeeping

    def cleanup(self) -> None:
        cutoff = now() - self.cfg.session_ttl
        dropped = []
        with self._meta:
            for session_id, session in list(self._sessions.items()):
                if session.updated < cutoff:
                    dropped.append(session_id)
                    self._sessions.pop(session_id, None)
                    self._locks.pop(session_id, None)
                    self._cache.pop(session_id, None)
            self._recent = [row for row in self._recent if row.get("at", 0) >= cutoff]
        for session_id in dropped:
            try:
                self._part_path(session_id).unlink()
            except OSError:
                pass
        for stale in self.tmp.glob("*.part"):
            if stale.stem not in self._sessions:
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    pass
        if dropped:
            self._persist()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1 << 20)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

