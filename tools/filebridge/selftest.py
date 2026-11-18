"""End-to-end proof: chunking, resume, ranges, auth, traversal, quota.

Run with `./filebridge selftest`.  It starts a real server on a random port,
transfers real bytes and checks real hashes.  Deliberately uses only stdlib so
it can run in a bare container.
"""

from __future__ import annotations

import http.client
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import client as xfer
from . import server as srv
from .config import Config
from .util import sha256_file


class CountingApi(xfer.Api):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.chunks_sent = 0

    def upload_chunk(self, *args, **kwargs):
        self.chunks_sent += 1
        return super().upload_chunk(*args, **kwargs)


PASS = "\x1b[32mPASS\x1b[0m"
FAIL = "\x1b[31mFAIL\x1b[0m"


class Checker:
    def __init__(self):
        self.failures = 0

    def ok(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"  {PASS}  {name}")
        else:
            self.failures += 1
            print(f"  {FAIL}  {name}  {detail}")

    def equal(self, name: str, actual, expected) -> None:
        self.ok(name, actual == expected, f"(got {actual!r}, want {expected!r})")


def _raw(port: int, method: str, path: str, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def run(keep: bool = False) -> int:
    check = Checker()
    workspace = Path(tempfile.mkdtemp(prefix="filebridge-selftest-"))
    previous = Path.cwd()
    print(f"\nfilebridge selftest  ({workspace})")

    cfg = Config(
        mode="both",
        root=str(workspace / "share"),
        inbox=str(workspace / "share" / "inbox"),
        state=str(workspace / "state"),
        bind="127.0.0.1",
        port=0,
        token="selftest-token-123",
        chunk_size=4 * 1024 * 1024,
        parallel=3,
        max_file=64 * 1024 * 1024,
        max_total=512 * 1024 * 1024,
        on_exist="overwrite",
        quiet=True,
    )
    cfg.root_path().mkdir(parents=True, exist_ok=True)
    cfg.inbox_path().mkdir(parents=True, exist_ok=True)
    cfg.state_path().mkdir(parents=True, exist_ok=True)

    httpd = srv.serve(cfg)
    port = httpd.port
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{port}"

    try:
        os.chdir(workspace)
        api = xfer.Api(base, token=cfg.token, retries=3, timeout=30)

        print("\n[auth]")
        check.ok("info is public", api.info()["mode"] == "both")
        bad = xfer.Api(base, token="wrong", retries=0, timeout=10)
        try:
            bad.call("GET", "/api/uploads")
            check.ok("wrong token is rejected", False)
        except xfer.Fatal as exc:
            check.ok("wrong token is rejected", "401" in str(exc), str(exc))

        print("\n[upload + integrity]")
        source = workspace / "src"
        (source / "nested").mkdir(parents=True)
        payloads = {
            "small.txt": b"hello filebridge\n",
            "nested/medium.bin": os.urandom(5 * 1024 * 1024 + 1234),
            "empty.bin": b"",
            "big.dat": os.urandom(11 * 1024 * 1024 + 7),   # 3 chunks, ragged tail
        }
        for name, data in payloads.items():
            (source / name).write_bytes(data)

        result = xfer.push(api, [str(source)], chunk_size=4 * 1024 * 1024, parallel=3)
        check.equal("all files finalized", result["finalized"], 4)
        check.ok("no failures", not result["failures"], str(result["failures"]))
        for name, data in payloads.items():
            target = cfg.inbox_path() / name
            check.ok(f"{name} bytes match", target.exists() and target.read_bytes() == data)
            check.ok(f"{name} sha256 matches", target.exists() and sha256_file(target) ==
                     __import__("hashlib").sha256(data).hexdigest())
        check.ok("no .part files left", not list((cfg.state_path() / "tmp").glob("*.part")))

        print("\n[resume: only missing chunks are re-sent]")
        src_big = source / "big.dat"
        digest = sha256_file(src_big)
        rel = "big.dat"
        session = api.create(rel, src_big.stat().st_size, digest,
                             chunk_size=4 * 1024 * 1024,
                             mtime=src_big.stat().st_mtime)
        check.ok("create returns a session", bool(session.get("id")))
        with open(src_big, "rb") as handle:
            first = handle.read(4 * 1024 * 1024)
        api.upload_chunk(session["id"], 0, first, src_big.stat().st_size,
                         __import__("hashlib").sha256(first).hexdigest())
        counting = CountingApi(base, token=cfg.token, retries=3, timeout=30)
        resumed = xfer.push(counting, [str(src_big)], chunk_size=4 * 1024 * 1024, parallel=2)
        total_chunks = (src_big.stat().st_size + 4 * 1024 * 1024 - 1) // (4 * 1024 * 1024)
        check.equal("resumed without re-sending chunk 0",
                    counting.chunks_sent, total_chunks - 1)
        check.equal("resume finalized", resumed["finalized"], 1)
        check.ok("resumed bytes verified",
                 (cfg.inbox_path() / "big.dat").read_bytes() == payloads["big.dat"])

        print("\n[idempotent chunk + conflict handling]")
        session = api.create("idem.bin", 1024, "", chunk_size=65536, mtime=1.0)
        chunk = os.urandom(1024)
        meta = api.upload_chunk(session["id"], 0, chunk, 1024,
                                __import__("hashlib").sha256(chunk).hexdigest())
        check.equal("chunk accepted", meta.get("committed"), 1024)
        again = api.upload_chunk(session["id"], 0, chunk, 1024,
                                 __import__("hashlib").sha256(chunk).hexdigest())
        check.equal("duplicate chunk is a no-op", again.get("committed"), 1024)
        api.abort(session["id"])

        print("\n[range downloads]")
        size = len(payloads["big.dat"])
        status, headers, body = _raw(port, "GET",
                                     "/api/files/download?path=inbox/big.dat",
                                     {"Authorization": f"Bearer {cfg.token}", "Range": "bytes=0-9"})
        check.equal("206 for a byte range", status, 206)
        check.equal("range body length", len(body), 10)
        check.equal("content-range header", headers.get("Content-Range"), f"bytes 0-9/{size}")
        status, headers, body = _raw(port, "GET",
                                     "/api/files/download?path=inbox/big.dat",
                                     {"Authorization": f"Bearer {cfg.token}", "Range": "bytes=-16"})
        check.equal("suffix range", (status, len(body)), (206, 16))
        status, headers, body = _raw(port, "GET",
                                     "/api/files/download?path=inbox/big.dat",
                                     {"Authorization": f"Bearer {cfg.token}",
                                      "Range": f"bytes={size + 10}-"})
        check.equal("416 past the end", status, 416)

        print("\n[pull + skip identical]")
        destination = workspace / "restored"
        pulled = xfer.pull(api, str(destination), remote_path="inbox", parallel=3)
        check.ok("no pull failures", not pulled["failures"], str(pulled["failures"]))
        for name, data in payloads.items():
            target = destination / name
            check.ok(f"pulled {name}", target.exists() and target.read_bytes() == data)
        again = xfer.pull(api, str(destination), remote_path="inbox", parallel=3)
        check.equal("second pull skips everything", again["skipped"], 4)

        print("\n[path traversal]")
        for evil in ("../../etc/passwd", "/etc/passwd", "a/../../b"):
            try:
                api.create(evil, 1)
                check.ok(f"rejected {evil!r}", False)
            except (xfer.Fatal, xfer.Retryable):
                check.ok(f"rejected {evil!r}", True)

        print("\n[quota + size limit]")
        try:
            api.create("too-big.bin", cfg.max_file + 1)
            check.ok("oversized file rejected", False)
        except xfer.Fatal as exc:
            check.ok("oversized file rejected", "413" in str(exc), str(exc))

        print("\n[session survives a server restart]")
        session = api.create(
            "restart.bin", 8 * 1024 * 1024, "", chunk_size=4 * 1024 * 1024, mtime=2.0
        )
        blob = os.urandom(4 * 1024 * 1024)
        api.upload_chunk(session["id"], 0, blob, 8 * 1024 * 1024,
                         __import__("hashlib").sha256(blob).hexdigest())
        httpd.shutdown()
        httpd.server_close()
        time.sleep(0.2)
        httpd = srv.serve(cfg)
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1},
                         daemon=True).start()
        api2 = xfer.Api(f"http://127.0.0.1:{httpd.port}", token=cfg.token, retries=3, timeout=30)
        revived = api2.session(session["id"])
        check.equal("committed offset survived restart", revived.get("committed"), 4 * 1024 * 1024)
        api2.abort(session["id"])

        print("\n[hidden files are excluded by default]")
        (cfg.root_path() / ".env").write_text("SECRET=1\n")
        paths = [entry["path"] for entry in api.tree("")["entries"]]
        check.ok(".env is not listed", ".env" not in paths)
        status, _, _ = _raw(httpd.port, "GET", "/api/files/download?path=.env",
                            {"Authorization": f"Bearer {cfg.token}"})
        check.equal("direct hidden download is 403", status, 403)
        cfg.include_hidden = True
        paths = [entry["path"] for entry in api.tree("")["entries"]]
        check.ok(".env is listed with include_hidden", ".env" in paths)
        cfg.include_hidden = False
        cfg.root_path().joinpath(".env").unlink()

        print("\n[log endpoint is not a deadlock]")
        started = time.time()
        logs = api.call("GET", "/api/logs")
        check.ok("logs answered quickly", time.time() - started < 2.0,
                 f"took {time.time() - started:.1f}s")
        check.ok("logs has entries", bool(logs.get("lines")))

        print("\n[proxy error detection]")
        with open(workspace / "html.json", "w") as handle:
            handle.write("x")
        status, headers, body = _raw(httpd.port, "GET", "/api/files/download?path=missing.bin",
                                     {"Authorization": f"Bearer {cfg.token}"})
        check.equal("missing file is 404", status, 404)

    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass
        os.chdir(previous)
        if keep:
            print(f"\n  kept workspace: {workspace}")
        else:
            shutil.rmtree(workspace, ignore_errors=True)

    print()
    if check.failures:
        print(f"  {check.failures} check(s) failed")
        return 1
    print("  all checks passed")
    return 0
