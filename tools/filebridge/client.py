"""CLI side transfers: push/pull with resume, retries and a progress bar.

The client is intentionally dumb about ordering: it asks the server which
chunks are still missing and fills the gaps.  That single decision makes
"resume after the codespace slept", "resume after my laptop's wifi died" and
"retry the chunk whose response was lost" the same code path.
"""

from __future__ import annotations

import fnmatch
import hashlib
import http.client
import json
import os
import random
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .util import human_bytes, human_duration, now, sha256_file


# ---------------------------------------------------------------- errors


class TransferError(Exception):
    pass


class Retryable(TransferError):
    pass


class SessionGone(TransferError):
    pass


class Fatal(TransferError):
    pass


RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


# ---------------------------------------------------------------- progress


class Progress:
    """Single-line aggregate progress; degrades to periodic lines."""

    def __init__(self, total: int, label: str = "", enabled: bool = True):
        self.total = max(0, total)
        self.done = 0
        self.label = label
        self.enabled = enabled and sys.stdout.isatty()
        self.started = now()
        self._last_print = now()
        self._lock = threading.Lock()
        self.failed = 0

    def add(self, amount: int, label: str | None = None) -> None:
        with self._lock:
            self.done += amount
            if label:
                self.label = label
            stamp = now()
            if self.enabled and stamp - self._last_print > 0.2:
                self._last_print = stamp
                self._render()
            elif not self.enabled and stamp - self._last_print > 5:
                self._last_print = stamp
                print(f"  {self._summary()}", flush=True)

    def _summary(self) -> str:
        elapsed = max(0.001, now() - self.started)
        speed = self.done / elapsed
        if self.total:
            pct = 100.0 * self.done / self.total
            eta = (self.total - self.done) / speed if speed > 1 else 0
            return (f"{pct:5.1f}%  {human_bytes(self.done)}/{human_bytes(self.total)}"
                    f"  {human_bytes(speed)}/s  eta {human_duration(eta)}")
        return f"{human_bytes(self.done)}  {human_bytes(speed)}/s"

    def _render(self) -> None:
        if self.total:
            width = 24
            filled = int(width * self.done / max(1, self.total))
            bar = "#" * filled + "-" * (width - filled)
        else:
            bar = "?" * 24
        line = f"  [{bar}] {self._summary()}  {self.label[:48]}"
        sys.stdout.write("\r" + line[:160].ljust(150))
        sys.stdout.flush()

    def close(self) -> None:
        if self.enabled:
            self._render()
            sys.stdout.write("\n")
            sys.stdout.flush()


# ------------------------------------------------------------------- api


@dataclass
class Job:
    local: Path
    remote: str
    size: int
    mtime: float
    session_id: str = ""
    chunk_size: int = 0
    missing: list = field(default_factory=list)
    sha256: str = ""
    error: str = ""
    skipped: bool = False



class Api:
    def __init__(
        self,
        url: str,
        token: str = "",
        retries: int = 8,
        timeout: float = 60.0,
        on_retry=None,
        insecure: bool = False,
    ):
        parsed = urlsplit(url if "://" in url else f"http://{url}")
        if parsed.scheme not in ("http", "https"):
            raise Fatal(f"unsupported scheme: {parsed.scheme}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.base = parsed.path.rstrip("/")
        self.token = token
        self.retries = max(0, retries)
        self.timeout = timeout
        self.on_retry = on_retry
        self._tls = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self._local = threading.local()

    # -- connection management -------------------------------------------

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        if self.scheme == "https":
            conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=self._tls
            )
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self._local.conn = conn
        return conn

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        self._local.conn = None

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": "filebridge-client",
            "Connection": "keep-alive",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # -- retry core -------------------------------------------------------

    def _sleep(self, attempt: int, reason: str) -> None:
        delay = min(20.0, 0.4 * (2 ** attempt)) * (0.7 + random.random() * 0.6)
        if self.on_retry:
            self.on_retry(attempt + 1, delay, reason)
        time.sleep(delay)

    def _retry(self, fn, what: str):
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except Retryable as exc:
                last = exc
                self._drop()
                if attempt >= self.retries:
                    break
                self._sleep(attempt, f"{what}: {exc}")
        raise Retryable(f"{what} failed after {self.retries + 1} attempts: {last}")

    # -- requests ---------------------------------------------------------

    def _describe(self, status: int, body: bytes, headers) -> str:
        ctype = ""
        for key, value in headers:
            if key.lower() == "content-type":
                ctype = value.lower()
                break
        detail = ""
        try:
            payload = json.loads(body.decode("utf-8"))
            detail = payload.get("error") or ""
        except (UnicodeDecodeError, ValueError):
            detail = body[:200].decode("utf-8", "replace").replace("\n", " ").strip()
        if "text/html" in ctype:
            return (f"HTTP {status} from the forwarded port, not from FileBridge "
                    f"(proxy/HTML page; codespace asleep or port not forwarded?)")
        return f"HTTP {status}: {detail or 'no detail'}"

    def _raise(self, status: int, body: bytes, headers, what: str) -> None:
        message = self._describe(status, body, headers)
        if status in RETRY_STATUS:
            raise Retryable(message)
        if status == 404 and what.startswith("chunk"):
            raise SessionGone(message)
        raise Fatal(message)

    def call(self, method: str, path: str, payload=None, allow=(200, 201, 204)) -> dict:
        body = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        def attempt():
            conn = self._conn()
            try:
                conn.request(method, f"{self.base}{path}", body=body, headers=headers)
                response = conn.getresponse()
                data = response.read()
            except (OSError, http.client.HTTPException, socket.timeout) as exc:
                self._drop()
                raise Retryable(f"{type(exc).__name__}: {exc}") from exc
            if response.status not in allow:
                self._raise(response.status, data, response.getheaders(), path)
            if not data:
                return {}
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise Retryable("response was not JSON (proxy page?)")

        return self._retry(attempt, f"{method} {path}")

    # -- verbs ------------------------------------------------------------

    def info(self) -> dict:
        return self.call("GET", "/api/info")

    def create(self, path: str, size: int, sha256: str = "", chunk_size: int = 0,
               mtime: float = 0.0) -> dict:
        payload = {"path": path, "size": size, "sha256": sha256, "mtime": mtime}
        if chunk_size:
            payload["chunk_size"] = chunk_size
        return self.call("POST", "/api/uploads", payload, allow=(200, 201))

    def session(self, session_id: str) -> dict:
        return self.call("GET", f"/api/uploads/{session_id}")

    def finalize(self, session_id: str, sha256: str = "") -> dict:
        return self.call(
            "POST", f"/api/uploads/{session_id}/finalize", {"sha256": sha256}
        )

    def abort(self, session_id: str) -> None:
        try:
            self.call("DELETE", f"/api/uploads/{session_id}", allow=(200, 204, 404))
        except TransferError:
            pass

    def stat(self, path: str, hash_file: bool = False) -> dict | None:
        query = urlencode({"path": path, **({"hash": "1"} if hash_file else {})})
        try:
            return self.call("GET", f"/api/files/stat?{query}")
        except Fatal as exc:
            if "404" in str(exc):
                return None
            raise

    def tree(self, path: str = "") -> dict:
        return self.call("GET", f"/api/files/tree?{urlencode({'path': path})}")

    # -- chunk upload ------------------------------------------------------

    def upload_chunk(self, session_id: str, offset: int, data: bytes,
                     total: int, sha256: str = "", on_retry=None) -> dict:
        end = offset + len(data) - 1
        headers = self._headers()
        headers["Content-Type"] = "application/octet-stream"
        headers["Content-Range"] = f"bytes {offset}-{end}/{total}"
        if sha256:
            headers["X-Chunk-Sha256"] = sha256

        def attempt():
            conn = self._conn()
            try:
                conn.request("PATCH", f"{self.base}/api/uploads/{session_id}",
                             body=data, headers=headers)
                response = conn.getresponse()
                payload = response.read()
            except (OSError, http.client.HTTPException, socket.timeout) as exc:
                self._drop()
                raise Retryable(f"{type(exc).__name__}: {exc}") from exc
            if response.status == 409:
                # chunk already stored with different digest, or offset drift
                try:
                    return json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise Retryable("conflict without JSON body")
            if response.status not in (200, 204):
                self._raise(response.status, payload, response.getheaders(), "chunk")
            try:
                return json.loads(payload.decode("utf-8")) if payload else {}
            except (UnicodeDecodeError, ValueError):
                raise Retryable("chunk response was not JSON")

        return self._retry(attempt, "chunk")

    # -- download ----------------------------------------------------------

    def download(self, remote_path: str, destination: Path, expected_size: int | None = None,
                 progress: Progress | None = None, should_stop=None) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        written = part.stat().st_size if part.exists() else 0
        if expected_size is not None and written > expected_size:
            part.unlink()
            written = 0

        attempt = 0
        while True:
            if should_stop and should_stop():
                raise TransferError("stopped by user")
            try:
                written = self._download_once(
                    remote_path, part, written, progress, should_stop
                )
                break
            except SessionGone:
                raise
            except Retryable as exc:
                attempt += 1
                if attempt > self.retries:
                    raise Retryable(f"download failed: {exc}")
                self._sleep(attempt - 1, f"download {remote_path}: {exc}")
                self._drop()

        if expected_size is not None and written != expected_size:
            raise Fatal(f"size mismatch: got {written}, expected {expected_size}")
        os.replace(part, destination)
        return written

    def _download_once(self, remote_path: str, part: Path, written: int,
                       progress: Progress | None, should_stop) -> int:
        query = urlencode({"path": remote_path})
        headers = self._headers()
        headers["Accept"] = "*/*"
        if written:
            headers["Range"] = f"bytes={written}-"
        conn = self._conn()
        try:
            conn.request("GET", f"{self.base}/api/files/download?{query}", headers=headers)
            response = conn.getresponse()
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            self._drop()
            raise Retryable(str(exc)) from exc

        if response.status == 416:
            response.read()
            return written
        if response.status == 200:
            written = 0  # server ignored Range, start over
        elif response.status != 206:
            body = response.read()
            self._raise(response.status, body, response.getheaders(), remote_path)

        mode = "ab" if written else "wb"
        with open(part, mode) as handle:
            while True:
                if should_stop and should_stop():
                    handle.flush()
                    raise TransferError("stopped by user")
                try:
                    block = response.read(262144)
                except (OSError, http.client.HTTPException, socket.timeout) as exc:
                    handle.flush()
                    self._drop()
                    raise Retryable(str(exc)) from exc
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if progress:
                    progress.add(len(block), Path(remote_path).name)
            handle.flush()
            os.fsync(handle.fileno())
        return written


# --------------------------------------------------------------- push


def _hash_cache_path() -> Path:
    return Path(".filebridge") / "hash-cache.json"


def file_sha(path: Path, cache: dict, use_cache: bool = True) -> str:
    stat = path.stat()
    key = f"{path.resolve()}::{stat.st_size}::{int(stat.st_mtime)}"
    if use_cache and key in cache:
        return cache[key]
    digest = sha256_file(path)
    cache[key] = digest
    return digest


def save_hash_cache(cache: dict) -> None:
    from .util import atomic_write_json

    try:
        atomic_write_json(_hash_cache_path(), cache)
    except OSError:
        pass


def load_hash_cache() -> dict:
    from .util import read_json

    return read_json(_hash_cache_path(), {})


def collect_files(sources: list[str], exclude: list[str] | None = None) -> list[tuple[Path, str]]:
    patterns = exclude or []
    found: list[tuple[Path, str]] = []
    for source in sources:
        path = Path(source).expanduser()
        if path.is_file():
            found.append((path, path.name))
            continue
        if not path.is_dir():
            raise Fatal(f"no such file or directory: {source}")
        base = path.parent if len(sources) > 1 else path
        for entry in sorted(path.rglob("*")):
            if not entry.is_file() or entry.is_symlink():
                continue
            relative = entry.relative_to(base).as_posix()
            if any(fnmatch.fnmatch(relative, pat) for pat in patterns):
                continue
            found.append((entry, relative))
    return found


def push(
    api: Api,
    sources: list[str],
    chunk_size: int = 0,
    parallel: int = 3,
    verify: bool = True,
    skip_identical: bool = False,
    exclude: list[str] | None = None,
    dry_run: bool = False,
    on_retry=None,
) -> dict:
    files = collect_files(sources, exclude)
    if not files:
        raise Fatal("nothing to upload")

    cache = load_hash_cache() if verify else {}
    jobs: list[Job] = []
    total = 0

    for local, remote in files:
        size = local.stat().st_size
        if skip_identical:
            remote_stat = api.stat(remote)
            if remote_stat and not remote_stat.get("dir") and remote_stat["size"] == size:
                local_mtime = int(local.stat().st_mtime)
                if abs(int(remote_stat.get("mtime", 0)) - local_mtime) <= 2:
                    jobs.append(Job(local, remote, size, local.stat().st_mtime, skipped=True))
                    continue
        if dry_run:
            jobs.append(Job(local=local, remote=remote, size=size,
                            mtime=local.stat().st_mtime))
            total += size
            continue
        digest = file_sha(local, cache) if verify else ""
        info = api.create(remote, size, digest, chunk_size,
                          mtime=local.stat().st_mtime)
        job = Job(
            local=local,
            remote=remote,
            size=size,
            mtime=local.stat().st_mtime,
            session_id=info["id"],
            chunk_size=int(info["chunk_size"]),
            missing=info.get("missing", []),
            sha256=digest,
        )
        jobs.append(job)
        total += size
        resumed = bool(info.get("committed"))
        percent = f"  ({round(100 * info['committed'] / max(1, size))}%)" if resumed else ""
        print(f"  {'resume' if resumed else 'new   '} {remote}  "
              f"{human_bytes(size)}{percent}", flush=True)

    if dry_run:
        print(f"  would upload {len(jobs)} file(s), {human_bytes(total)}")
        return {"files": len(jobs), "bytes": total, "dry_run": True}

    lock = threading.Lock()
    failures: list[str] = []

    tasks: list[tuple[Job, int, int]] = []
    for job in jobs:
        if job.skipped:
            continue
        for start, end in job.missing:
            step = job.chunk_size
            cursor = start
            while cursor <= end:
                chunk_end = min(cursor + step - 1, end)
                tasks.append((job, cursor, chunk_end))
                cursor = chunk_end + 1

    remaining_bytes = sum(end - start + 1 for _, start, end in tasks)
    progress = Progress(remaining_bytes, "starting")

    def worker() -> None:
        while True:
            with lock:
                if not tasks:
                    return
                job, start, end = tasks.pop()
            length = end - start + 1
            try:
                with open(job.local, "rb") as handle:
                    handle.seek(start)
                    data = handle.read(length)
                if len(data) != length:
                    raise Fatal(f"short read on {job.local}")
                digest = hashlib.sha256(data).hexdigest() if verify else ""
                api.upload_chunk(job.session_id, start, data, job.size, digest,
                                 on_retry=on_retry)
                progress.add(len(data), job.remote)
            except (TransferError, OSError) as exc:
                if isinstance(exc, SessionGone):
                    with lock:
                        job.error = "session disappeared; re-run to resume"
                else:
                    with lock:
                        job.error = str(exc)
                with lock:
                    failures.append(f"{job.remote}@{start}: {exc}")
                return

    threads = [
        threading.Thread(target=worker, daemon=True)
        for _ in range(max(1, min(parallel, max(1, len(tasks)))))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    progress.close()

    finalized = 0
    for job in jobs:
        if job.skipped:
            continue
        if job.error:
            print(f"  failed  {job.remote}: {job.error}", file=sys.stderr)
            continue
        try:
            api.finalize(job.session_id, job.sha256 if verify else "")
            finalized += 1
            print(f"  done    {job.remote}", flush=True)
        except TransferError as exc:
            failures.append(f"{job.remote} finalize: {exc}")
            print(f"  failed  {job.remote}: {exc}", file=sys.stderr)

    if verify:
        save_hash_cache(cache)

    return {
        "files": len(jobs),
        "finalized": finalized,
        "skipped": sum(1 for job in jobs if job.skipped),
        "bytes": total,
        "failures": failures,
    }


# --------------------------------------------------------------- pull


def pull(
    api: Api,
    destination: str,
    remote_path: str = "",
    parallel: int = 3,
    overwrite: bool = False,
    verify: bool = False,
    exclude: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    root = Path(destination).expanduser().resolve()
    listing = api.tree(remote_path)
    patterns = exclude or []
    files = [
        entry for entry in listing["entries"]
        if not entry["dir"]
        and not any(fnmatch.fnmatch(entry["path"], pat) for pat in patterns)
    ]
    print(f"  remote {listing.get('path') or '/'}: {len(files)} file(s)", flush=True)
    if dry_run:
        total = sum(entry["size"] for entry in files)
        return {"files": len(files), "bytes": total, "dry_run": True}

    progress = Progress(sum(entry["size"] for entry in files), "starting")
    lock = threading.Lock()
    queue = list(files)
    failures: list[str] = []
    skipped = 0

    def worker() -> None:
        nonlocal skipped
        while True:
            with lock:
                if not queue:
                    return
                entry = queue.pop()
            target = root / entry["path"]
            try:
                if (
                    target.exists()
                    and not overwrite
                    and target.stat().st_size == entry["size"]
                ):
                    with lock:
                        skipped += 1
                        progress.add(entry["size"], entry["path"])
                    continue
                remote_rel = (
                    (Path(remote_path) / entry["path"]).as_posix()
                    if remote_path else entry["path"]
                )
                wrote = api.download(
                    remote_rel,
                    target,
                    expected_size=entry["size"],
                    progress=progress,
                )
                os.utime(target, (entry["mtime"], entry["mtime"]))
                if verify:
                    remote_stat = api.stat(remote_rel, hash_file=True)
                    local = sha256_file(target)
                    if remote_stat and remote_stat.get("sha256") and local != remote_stat["sha256"]:
                        raise Fatal("checksum mismatch")
                if wrote != entry["size"]:
                    raise Fatal(f"size mismatch {wrote} != {entry['size']}")
            except TransferError as exc:
                with lock:
                    failures.append(f"{entry['path']}: {exc}")
                print(f"  failed  {entry['path']}: {exc}", file=sys.stderr)

    threads = [
        threading.Thread(target=worker, daemon=True)
        for _ in range(max(1, min(parallel, max(1, len(files)))))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    progress.close()

    return {
        "files": len(files),
        "skipped": skipped,
        "bytes": sum(entry["size"] for entry in files),
        "failures": failures,
    }
