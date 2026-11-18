"""HTTP data plane.

Design rules that matter:

* every request is small and bounded (one chunk per PATCH, one file per GET),
  so a single transfer can never head-of-line-block the IDE again;
* upload chunks are addressed by offset and are idempotent, so a lost response
  or a suspended codespace is a retry, not a restart;
* responses always carry Content-Length or are properly chunked, so
  keep-alive works through the Codespaces proxy;
* a dead or stalled peer cannot pin a worker forever (socket timeout).
"""

from __future__ import annotations

import collections
import hmac
import json
import mimetypes
import os
import re
import socket
import threading
import time
import traceback
from email.utils import formatdate
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import VERSION
from .config import Config
from .util import etag_for, human_bytes, now, safe_join, sha256_file
from .store import Store, StoreError

UI_PATH = Path(__file__).with_name("ui.html")
MAX_TREE_ENTRIES = 20_000

LOG_RING: collections.deque = collections.deque(maxlen=400)
LOG_LOCK = threading.RLock()


def log(kind: str, message: str) -> None:
    row = {"at": now(), "kind": kind, "msg": message}
    with LOG_LOCK:
        LOG_RING.append(row)
    stamp = time.strftime("%H:%M:%S")
    print(f"[{stamp}] {kind:6} {message}", flush=True)


def _is_hidden_rel(rel: str) -> bool:
    return any(part.startswith(".") for part in rel.split("/") if part)


class ApiError(Exception):
    def __init__(self, code: int, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.started = now()
        self.slots = threading.BoundedSemaphore(max(4, cfg.parallel * 2))
        self.active = 0
        self._active_lock = threading.Lock()
        self.failures = 0
        super().__init__(address, handler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def enter_slot(self) -> bool:
        acquired = self.slots.acquire(timeout=25)
        if acquired:
            with self._active_lock:
                self.active += 1
        return acquired

    def leave_slot(self) -> None:
        with self._active_lock:
            self.active = max(0, self.active - 1)
        try:
            self.slots.release()
        except ValueError:
            pass

    def handle_error(self, request, client_address):
        self.failures += 1
        log("error", f"{client_address[0]} connection failed: "
                     f"{traceback.format_exc(limit=1).strip().splitlines()[-1]}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"filebridge/{VERSION}"
    sys_version = ""

    # ------------------------------------------------------------- plumbing

    def setup(self) -> None:
        super().setup()
        try:
            self.connection.settimeout(self.server.cfg.io_timeout)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def _peer(self) -> str:
        """Peer address, plus the real client when a trusted proxy forwarded it.

        Through the Codespaces relay the TCP peer is always the local tunnel
        agent (127.0.0.1); the relay overwrites X-Forwarded-For with the origin
        IP, so only trust it when the peer is loopback.
        """
        peer = self.client_address[0]
        forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        if forwarded and peer in ("127.0.0.1", "::1") and forwarded != peer:
            return f"{peer} (via {forwarded})"
        return peer

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        # never let an access-log line persist a token from ?t=...
        message = re.sub(r"([?&]t=)[^&\s\"]+", r"\1<redacted>", message)
        log("http", f"{self._peer()} {message}")

    def log_error(self, fmt: str, *args) -> None:
        log("http", f"{self._peer()} {fmt % args}")

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def end_headers(self) -> None:
        cors = self.server.cfg.cors
        if cors:
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Vary", "Origin")
        super().end_headers()

    # ------------------------------------------------------------- dispatch

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        if not self.server.cfg.cors:
            self._fail(403, "CORS disabled")
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PATCH, DELETE")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Range, Content-Type, X-Chunk-Sha256, X-FB-Token",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        self._method = method
        try:
            parts, query = self._parse()
            self._parts = parts
            self._query = query
            self._route(method, parts, query)
        except ApiError as exc:
            self._fail(exc.code, exc.message, **exc.extra)
        except StoreError as exc:
            self._fail(exc.code, exc.message, **exc.extra)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except ValueError as exc:
            self._fail(400, str(exc))
        except Exception:
            log("error", traceback.format_exc(limit=4))
            try:
                self._fail(500, "internal error")
            except Exception:
                self.close_connection = True

    def _parse(self):
        split = urlsplit(self.path)
        parts = [unquote(seg) for seg in split.path.split("/") if seg]
        return parts, parse_qs(split.query)

    def _route(self, method: str, parts: list[str], query: dict) -> None:
        if not parts:
            if method in ("GET", "HEAD"):
                self._serve_ui()
                return
            raise ApiError(405, "method not allowed")
        head = parts[0]

        if head == "healthz" and method in ("GET", "HEAD"):
            self._send_json(200, {"ok": True, "uptime": round(now() - self.server.started, 1)})
            return
        if head == "favicon.ico":
            self._send_empty(204)
            return
        if head != "api":
            raise ApiError(404, "not found")

        if len(parts) == 2 and parts[1] == "info" and method in ("GET", "HEAD"):
            self._api_info()
            return

        if not self._authorized():
            self.close_connection = True
            raise ApiError(401, "missing or invalid token")

        rest = parts[1:]
        if not rest:
            raise ApiError(404, "not found")
        resource = rest[0]

        if resource == "uploads":
            self._route_uploads(method, rest[1:])
            return
        if resource == "files":
            self._route_files(method, rest[1:], query)
            return
        if resource == "bundle" and method in ("GET", "HEAD"):
            self._serve_bundle(query)
            return
        if resource == "logs" and method == "GET":
            # snapshot before responding: send_response() logs, and logging
            # takes the same lock.
            with LOG_LOCK:
                lines = list(LOG_RING)
            self._send_json(200, {"lines": lines})
            return
        if resource == "auth" and method == "POST":
            self._discard_body()
            self._send_json(200, {"ok": True})
            return
        raise ApiError(404, "not found")

    # ------------------------------------------------------------- auth

    def _authorized(self) -> bool:
        token = self.server.cfg.token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = (self.headers.get("X-FB-Token") or "").strip()
        if not supplied:
            supplied = (self._query.get("t") or [""])[0]
        return hmac.compare_digest(supplied, token)

    # ------------------------------------------------------------- responses

    def _send_empty(self, code: int, extra: dict | None = None) -> None:
        self.send_response(int(code))
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, code: int, payload, extra: dict | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(int(code))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _fail(self, code: int, message: str, **extra) -> None:
        if self._body_pending():
            self.close_connection = True
        payload = {"error": message, "code": int(code)}
        if extra:
            payload.update(extra)
        retry = extra.get("retry_after")
        headers = {"Retry-After": retry} if retry else None
        try:
            self._send_json(code, payload, headers)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _body_pending(self) -> bool:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return True
        return length > 0

    def _discard_body(self, limit: int = 1 << 20) -> None:
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.close_connection = True
            return
        if remaining <= 0:
            return
        if remaining > limit:
            self.close_connection = True
            return
        while remaining > 0:
            block = self.rfile.read(min(65536, remaining))
            if not block:
                self.close_connection = True
                return
            remaining -= len(block)

    def _read_json(self, limit: int = 1 << 20):
        try:
            remaining = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ApiError(411, "length required")
        if remaining <= 0:
            return {}
        if remaining > limit:
            self.close_connection = True
            raise ApiError(413, "request body too large")
        raw = bytearray()
        while len(raw) < remaining:
            block = self.rfile.read(remaining - len(raw))
            if not block:
                raise ApiError(400, "client closed mid-body")
            raw.extend(block)
        if not raw:
            return {}
        try:
            return json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "invalid JSON body")

    # ------------------------------------------------------------- UI

    def _serve_ui(self) -> None:
        try:
            body = UI_PATH.read_bytes()
        except OSError:
            raise ApiError(500, "ui.html is missing from the package")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _api_info(self) -> None:
        cfg = self.server.cfg
        info = {
            "app": "filebridge",
            "version": VERSION,
            "mode": cfg.mode,
            "can_upload": cfg.can_upload(),
            "can_download": cfg.can_download(),
            "auth": bool(cfg.token),
            "chunk_size": cfg.chunk_size,
            "parallel": cfg.parallel,
            "max_file": cfg.max_file,
            "max_total": cfg.max_total,
            "on_exist": cfg.on_exist,
            "port": self.server.port,
            "active": self.server.active,
            "stats": self.server.store.stats(),
            "limits": cfg.limits_summary(),
        }
        self._send_json(200, info)

    # ------------------------------------------------------------- uploads

    def _route_uploads(self, method: str, rest: list[str]) -> None:
        cfg = self.server.cfg
        if not cfg.can_upload():
            raise ApiError(403, "this server is download-only")

        if not rest:
            if method == "POST":
                self._upload_create()
                return
            if method in ("GET", "HEAD"):
                self._send_json(
                    200,
                    {
                        "sessions": self.server.store.list(),
                        "recent": self.server.store.recent(),
                    },
                )
                return
            raise ApiError(405, "method not allowed")

        session_id = rest[0]
        if len(rest) >= 2 and rest[1] == "finalize" and method == "POST":
            self._upload_finalize(session_id)
            return
        if method == "PATCH":
            self._upload_chunk(session_id)
            return
        if method in ("GET", "HEAD"):
            self._send_json(200, self.server.store.get(session_id).public())
            return
        if method == "DELETE":
            self._discard_body()
            self.server.store.delete(session_id)
            self._send_json(200, {"ok": True})
            return
        raise ApiError(405, "method not allowed")

    def _upload_create(self) -> None:
        payload = self._read_json()
        path = str(payload.get("path") or "").strip()
        if not path:
            raise ApiError(400, "path is required")
        try:
            size = int(payload.get("size", -1))
        except (TypeError, ValueError):
            raise ApiError(400, "size must be an integer")
        if size < 0:
            raise ApiError(400, "size is required")
        sha = str(payload.get("sha256") or "").strip().lower()
        if sha and (len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise ApiError(400, "sha256 must be 64 hex characters")
        chunk_size = payload.get("chunk_size")
        mtime = float(payload.get("mtime") or 0)
        mode = int(payload.get("mode") or 0o644)

        session, created = self.server.store.create(
            path=path,
            size=size,
            sha256=sha,
            chunk_size=int(chunk_size) if chunk_size else None,
            mtime=mtime,
            mode=mode,
        )
        log("upload", f"create {' '.join([session.id, path, human_bytes(size)])}"
                      f"{'' if created else ' (resumed existing session)'}")
        self._send_json(201 if created else 200, session.public())

    def _upload_chunk(self, session_id: str) -> None:
        if not self.server.enter_slot():
            raise ApiError(503, "server busy, retry shortly", retry_after=1)
        try:
            content_range = (self.headers.get("Content-Range") or "").strip()
            start, end, total = self._parse_content_range(content_range)
            try:
                length = int(self.headers.get("Content-Length") or -1)
            except (TypeError, ValueError):
                raise ApiError(411, "length required")
            if length < 0:
                raise ApiError(411, "length required")
            digest = (self.headers.get("X-Chunk-Sha256") or "").strip().lower()
            session = self.server.store.write_chunk(
                session_id, start, end, total, length, self.rfile, digest
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "committed": session.committed,
                    "done": len(session.chunks),
                    "total": session.nchunks,
                    "missing": session.missing_ranges(),
                },
            )
            if session.done():
                log("upload", f"{session_id} all chunks present "
                              f"({human_bytes(session.size)})")
        finally:
            self.server.leave_slot()

    def _upload_finalize(self, session_id: str) -> None:
        payload = self._read_json()
        expect = str(payload.get("sha256") or "").strip().lower()
        result = self.server.store.finalize(session_id, expect)
        log("upload", f"{session_id} finalized -> {result['path']} "
                      f"({human_bytes(result['size'])})")
        self._send_json(200, result)

    @staticmethod
    def _parse_content_range(value: str) -> tuple[int, int, int]:
        if not value.lower().startswith("bytes "):
            raise ApiError(400, "Content-Range must look like 'bytes a-b/total'")
        span, _, total_text = value[6:].strip().partition("/")
        start_text, _, end_text = span.partition("-")
        try:
            start, end, total = int(start_text), int(end_text), int(total_text)
        except ValueError:
            raise ApiError(400, "malformed Content-Range")
        if start < 0 or end < start or total <= 0:
            raise ApiError(416, "invalid byte range")
        return start, end, total

    # ------------------------------------------------------------- downloads

    def _route_files(self, method: str, rest: list[str], query: dict) -> None:
        cfg = self.server.cfg
        if not rest:
            raise ApiError(404, "not found")
        action = rest[0]
        if not cfg.can_download():
            raise ApiError(403, "this server is upload-only")

        if action == "tree" and method in ("GET", "HEAD"):
            self._files_tree(query)
            return
        if action == "stat" and method in ("GET", "HEAD"):
            self._files_stat(query)
            return
        if action == "download" and method in ("GET", "HEAD"):
            self._files_download(query)
            return
        if action == "delete" and method == "DELETE":
            self._files_delete(query)
            return
        raise ApiError(404, "not found")

    def _files_tree(self, query: dict) -> None:
        base = self._query_path(query)
        if not base.is_dir():
            raise ApiError(404, "not a directory")
        entries = []
        stack = [(base, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                children = sorted(os.scandir(current), key=lambda e: e.name.lower())
            except OSError as exc:
                log("warn", f"cannot list {current}: {exc}")
                continue
            for entry in children:
                if entry.name.endswith(".part"):
                    continue
                try:
                    state = self.server.cfg.state_path()
                    entry_path = Path(entry.path)
                    if entry_path == state or state in entry_path.parents:
                        continue
                    if not self.server.cfg.include_hidden and entry.name.startswith("."):
                        continue
                    if entry.is_symlink():
                        target = Path(entry.path).resolve()
                        root = self.server.cfg.root_path()
                        if root not in target.parents and target != root:
                            continue
                    if entry.is_dir():
                        entries.append(
                            {
                                "path": str(Path(entry.path).relative_to(base)),
                                "dir": True,
                                "size": 0,
                                "mtime": 0,
                            }
                        )
                        if depth < 12:
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    stat = entry.stat()
                    entries.append(
                        {
                            "path": str(Path(entry.path).relative_to(base)),
                            "dir": False,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                            "etag": etag_for(stat),
                        }
                    )
                except OSError:
                    continue
                if len(entries) >= MAX_TREE_ENTRIES:
                    log("warn", f"tree truncated at {MAX_TREE_ENTRIES} entries")
                    stack.clear()
                    break
        self._send_json(
            200,
            {"root": base.name, "path": str(base.relative_to(self.server.cfg.root_path()))
             if base != self.server.cfg.root_path() else "", "entries": entries},
        )

    def _files_stat(self, query: dict) -> None:
        target = self._query_path(query)
        if not target.exists():
            raise ApiError(404, "no such file")
        stat = target.stat()
        payload = {
            "path": str(target.relative_to(self.server.cfg.root_path())),
            "dir": target.is_dir(),
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "etag": etag_for(stat),
        }
        if (query.get("hash") or ["0"])[0] in ("1", "true", "yes") and target.is_file():
            payload["sha256"] = sha256_file(target)
        self._send_json(200, payload)

    def _files_download(self, query: dict) -> None:
        if not self.server.enter_slot():
            raise ApiError(503, "server busy, retry shortly", retry_after=1)
        try:
            target = self._query_path(query)
            if not target.exists():
                raise ApiError(404, "no such file")
            if target.is_dir():
                raise ApiError(400, "is a directory; use /api/bundle")
            inline = (query.get("inline") or ["0"])[0] in ("1", "true", "yes")
            self._serve_file(target, inline=inline)
        finally:
            self.server.leave_slot()

    def _files_delete(self, query: dict) -> None:
        if not self.server.cfg.allow_delete:
            raise ApiError(403, "deleting is disabled (start with --allow-delete)")
        target = self._query_path(query)
        if not target.exists():
            raise ApiError(404, "no such file")
        if target.is_dir():
            raise ApiError(400, "refusing to delete a directory")
        target.unlink()
        log("delete", str(target))
        self._send_json(200, {"ok": True})

    def _query_path(self, query: dict) -> Path:
        rel = (query.get("path") or [""])[0]
        root = self.server.cfg.root_path()
        try:
            target = safe_join(root, rel)
        except ValueError as exc:
            raise ApiError(400, f"bad path: {exc}")
        state = self.server.cfg.state_path()
        if target == state or state in target.parents:
            raise ApiError(403, "state directory is not accessible")
        if not self.server.cfg.include_hidden:
            relative = "" if target == root else target.relative_to(root).as_posix()
            if _is_hidden_rel(relative):
                raise ApiError(
                    403,
                    "hidden path: start with --include-hidden to serve dotfiles",
                )
        return target

    def _serve_file(self, path: Path, inline: bool = False) -> None:
        stat = path.stat()
        size = stat.st_size
        etag = etag_for(stat)
        modified = formatdate(stat.st_mtime, usegmt=True)

        if (self.headers.get("If-None-Match") or "") == etag:
            self._send_empty(304, {"ETag": etag})
            return

        start, end, partial = 0, size - 1, False
        raw_range = self.headers.get("Range")
        if raw_range:
            parsed = self._parse_range(raw_range, size)
            if parsed is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start, end = parsed
            partial = True

        length = end - start + 1 if size else 0
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not inline:
            ctype = "application/octet-stream"

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", modified)
        self.send_header("Cache-Control", "no-cache")
        if not inline:
            quoted = quote(path.name)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quoted}",
            )
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if self.command == "HEAD" or length == 0:
            return
        remaining = length
        with open(path, "rb") as handle:
            handle.seek(start)
            while remaining > 0:
                block = handle.read(min(262144, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    @staticmethod
    def _parse_range(value: str, size: int) -> tuple[int, int] | None:
        if not value.lower().startswith("bytes="):
            return None
        spec = value[6:].split(",")[0].strip()
        start_text, _, end_text = spec.partition("-")
        try:
            if start_text == "":
                suffix = int(end_text)
                if suffix <= 0:
                    return None
                start = max(0, size - suffix)
                return start, size - 1
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        except ValueError:
            return None
        if start > end or start >= size:
            return None
        return start, min(end, size - 1)

    # ------------------------------------------------------------- bundle

    def _serve_bundle(self, query: dict) -> None:
        import tarfile

        if not self.server.cfg.can_download():
            raise ApiError(403, "this server is upload-only")
        if not self.server.enter_slot():
            raise ApiError(503, "server busy, retry shortly", retry_after=1)
        try:
            base = self._query_path(query)
            if not base.exists():
                raise ApiError(404, "no such path")
            gzip = (query.get("gzip") or ["0"])[0] in ("1", "true", "yes")
            name = base.name or "bundle"
            suffix = ".tar.gz" if gzip else ".tar"
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar"
                              if not gzip else "application/gzip")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quote(name + suffix)}",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            sink = _ChunkWriter(self.wfile)
            mode = "w|gz" if gzip else "w|"
            with tarfile.open(fileobj=sink, mode=mode, dereference=False) as archive:
                if base.is_dir():
                    state = self.server.cfg.state_path()
                    for entry in sorted(base.rglob("*")):
                        if entry.name.endswith(".part"):
                            continue
                        try:
                            if entry == state or state in entry.resolve().parents:
                                continue
                        except OSError:
                            continue
                        relative = entry.relative_to(base)
                        if (not self.server.cfg.include_hidden
                                and any(part.startswith(".") for part in relative.parts)):
                            continue
                        try:
                            archive.add(
                                entry,
                                arcname=str(entry.relative_to(base.parent)),
                                recursive=False,
                            )
                        except OSError:
                            continue
                else:
                    archive.add(base, arcname=base.name, recursive=False)
            sink.finish()
        finally:
            self.server.leave_slot()


class _ChunkWriter:
    """File-like sink that emits HTTP/1.1 chunked framing."""

    def __init__(self, wfile, flush_at: int = 65536):
        self.wfile = wfile
        self.flush_at = flush_at
        self.buffer = bytearray()

    def write(self, data) -> int:
        if not data:
            return 0
        self.buffer.extend(data)
        if len(self.buffer) >= self.flush_at:
            self._emit()
        return len(data)

    def flush(self) -> None:
        self._emit()

    def _emit(self) -> None:
        if not self.buffer:
            return
        block = bytes(self.buffer)
        self.buffer.clear()
        self.wfile.write(b"%x\r\n" % len(block))
        self.wfile.write(block)
        self.wfile.write(b"\r\n")

    def finish(self) -> None:
        self._emit()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


# ---------------------------------------------------------------- helpers


def serve(cfg: Config, announce=None):
    """Bind, optionally flip port visibility, then serve forever."""
    store = Store(cfg)
    bind = cfg.bind or ("0.0.0.0")
    try:
        server = BridgeServer((bind, cfg.port), Handler, cfg, store)
    except OSError as exc:
        if cfg.port == 0:
            raise
        log("warn", f"port {cfg.port} unavailable ({exc}), picking a free one")
        server = BridgeServer((bind, 0), Handler, cfg, store)
    return server
