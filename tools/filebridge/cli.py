"""Entry point: `./filebridge` (no arguments) is the whole product.

Subcommands:
  (none) | serve   start the server with safe defaults, zero questions
  setup            interactive wizard (every answer has a default)
  push             upload files/directories to a running server
  pull             download a tree from a running server
  status           show server info
  token            print (or rotate) the access token
  url              print the forwarded URL / tunnel command
  selftest         end-to-end proof that resume, ranges and auth work
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import webbrowser
from pathlib import Path

from . import VERSION, codespace
from . import client as xfer
from . import server as srv
from .config import (
    Config,
    apply_env,
    apply_file,
    config_path,
    generate_token,
    load_file,
    resolve_token,
    save_file,
    validate,
)
from .util import Style, human_bytes, parse_size

SUBCOMMANDS = ("serve", "setup", "push", "pull", "status", "token", "url",
               "visibility", "selftest")
RUNTIME_FILE = Path(".filebridge") / "runtime.json"


# ------------------------------------------------------------------ helpers


def _style(no_color: bool) -> Style:
    return Style(sys.stdout.isatty() and not no_color and not os.environ.get("NO_COLOR"))


def _add_serve_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=["both", "upload", "download"], default=None)
    parser.add_argument("--root", default=None, help="directory served for downloads")
    parser.add_argument("--inbox", default=None, help="directory that receives uploads")
    parser.add_argument("--state", default=None, help="session state directory")
    parser.add_argument("--bind", default=None, help="listen address (default: auto)")
    parser.add_argument("--port", type=int, default=None, help="0 picks a free port")
    parser.add_argument("--token", default=None, help="access token, or 'auto'")
    parser.add_argument("--no-auth", action="store_true", default=None,
                        help="disable auth (dangerous on a public port)")
    parser.add_argument("--visibility", choices=["private", "public", "org", "none"],
                        default=None, help="codespaces port visibility")
    parser.add_argument("--visibility-timeout", type=float, default=None,
                        help="seconds to wait for the port to register with the tunnel")
    parser.add_argument("--chunk-size", type=parse_size, default=None,
                        help="chunk size, e.g. 4M")
    parser.add_argument("--parallel", type=int, default=None, help="parallel transfers")
    parser.add_argument("--max-file", type=parse_size, default=None)
    parser.add_argument("--max-total", type=parse_size, default=None)
    parser.add_argument("--on-exist", choices=["rename", "overwrite", "fail"], default=None)
    parser.add_argument("--session-ttl", type=float, default=None,
                        help="hours before an idle upload session is dropped")
    parser.add_argument("--cors", default=None, help="allow this origin (e.g. '*')")
    parser.add_argument("--allow-delete", action="store_true", default=None)
    parser.add_argument("--include-hidden", action="store_true", default=None,
                        help="serve dotfiles and dot-directories")
    parser.add_argument("--open", dest="open_browser", action="store_true", default=None)
    parser.add_argument("-i", "--interactive", action="store_true", default=None,
                        help="ask questions instead of using defaults")
    parser.add_argument("--save", action="store_true", default=None,
                        help="remember answers in .filebridge/config.json")
    parser.add_argument("--quiet", action="store_true", default=None)
    parser.add_argument("-v", "--verbose", action="store_true", default=None)
    parser.add_argument("--no-color", action="store_true", default=None)


def _add_client_flags(parser: argparse.ArgumentParser, url_flag: str, url_help: str) -> None:
    parser.add_argument(url_flag, dest="url", default=None, required=False, help=url_help)
    parser.add_argument("--token", default=None)
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    parser.add_argument("--exclude", action="append", default=[],
                        help="glob to skip (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-color", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filebridge",
        description="Resumable bulk file transfer over a forwarded port.",
    )
    parser.add_argument("--version", action="version", version=f"filebridge {VERSION}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="start the server (default)")
    _add_serve_flags(serve)

    setup = sub.add_parser("setup", help="interactive configuration wizard")
    _add_serve_flags(setup)

    push = sub.add_parser("push", help="upload files to a server")
    push.add_argument("sources", nargs="+")
    _add_client_flags(push, "--to", "server URL (default: local runtime)")
    push.add_argument("--chunk-size", type=parse_size, default=None)
    push.add_argument("--skip-identical", action="store_true")
    push.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    push.add_argument("--checksum", dest="verify", action="store_true")

    pull = sub.add_parser("pull", help="download a tree from a server")
    pull.add_argument("destination")
    _add_client_flags(pull, "--from", "server URL (default: local runtime)")
    pull.add_argument("--path", default="", help="remote subdirectory")
    pull.add_argument("--overwrite", action="store_true")
    pull.add_argument("--verify", action="store_true")

    status = sub.add_parser("status", help="print server info")
    status.add_argument("--url", default=None)
    status.add_argument("--token", default=None)
    status.add_argument("--insecure", action="store_true")

    token = sub.add_parser("token", help="print or rotate the access token")
    token.add_argument("--rotate", action="store_true")

    url = sub.add_parser("url", help="print the forwarded URL and tunnel command")
    url.add_argument("--port", type=int, default=None)

    visibility = sub.add_parser("visibility", help="apply and verify codespaces port visibility")
    visibility.add_argument("--to", choices=["private", "public", "org"], default=None)
    visibility.add_argument("--port", type=int, default=None)
    visibility.add_argument("--timeout", type=float, default=30.0)

    selftest = sub.add_parser("selftest", help="run the end-to-end checks")
    selftest.add_argument("--keep", action="store_true", help="keep the temp workspace")

    return parser


# ------------------------------------------------------------------ config


CLI_FIELDS = (
    "mode", "root", "inbox", "state", "bind", "port", "token", "visibility",
    "chunk_size", "parallel", "max_file", "max_total", "on_exist", "cors",
)
FLAG_FIELDS = ("allow_delete", "include_hidden", "open_browser", "quiet", "verbose",
               "interactive", "save")


def build_config(ns: argparse.Namespace) -> Config:
    cfg = Config()
    apply_file(cfg, load_file())
    apply_env(cfg)
    for name in CLI_FIELDS:
        value = getattr(ns, name, None)
        if value is not None:
            setattr(cfg, name, value)
    for name in FLAG_FIELDS:
        value = getattr(ns, name, None)
        if value is True:
            setattr(cfg, name, True)
        elif value is False and getattr(cfg, name, None) is True:
            setattr(cfg, name, False)
    if getattr(ns, "no_color", None):
        cfg.color = False
    if getattr(ns, "no_auth", None):
        cfg.token = "none"   # explicit: never confuse this with "auto-generate"
    if getattr(ns, "session_ttl", None) is not None:
        cfg.session_ttl = float(ns.session_ttl) * 3600
    return cfg


def runtime_info() -> dict:
    from .util import read_json

    return read_json(RUNTIME_FILE, {})


def resolve_client_target(ns: argparse.Namespace) -> tuple[str, str]:
    url = getattr(ns, "url", None)
    token = getattr(ns, "token", None)
    info = runtime_info()
    if not url:
        url = info.get("url") or ""
    if not token:
        token = os.environ.get("FB_TOKEN") or load_file().get("token") or info.get("token") or ""
    if not url:
        raise SystemExit("no server URL: pass --to/--from or start a local server first")
    return url, token


def make_api(ns: argparse.Namespace, quiet: bool = False) -> xfer.Api:
    url, token = resolve_client_target(ns)
    retries = getattr(ns, "retries", 8)

    def on_retry(attempt: int, delay: float, reason: str) -> None:
        if not quiet:
            print(f"  retry {attempt} in {delay:.1f}s  ({reason})", file=sys.stderr, flush=True)

    return xfer.Api(
        url,
        token=token,
        retries=retries,
        timeout=getattr(ns, "timeout", 60.0),
        on_retry=on_retry,
        insecure=getattr(ns, "insecure", False),
    )


# ------------------------------------------------------------------ serve


def _write_runtime(payload: dict) -> None:
    from .util import atomic_write_json

    try:
        atomic_write_json(RUNTIME_FILE, payload)
    except OSError:
        pass


def cmd_serve(ns: argparse.Namespace) -> int:
    from . import wizard

    cfg = build_config(ns)
    validate(cfg)

    interactive = ns.command == "setup" or bool(cfg.interactive)
    if interactive:
        cfg = wizard.run(cfg)
        validate(cfg)

    generated = cfg.token in ("", "auto")
    token_choice = cfg.token
    cfg.token = resolve_token(cfg, explicit_none=bool(getattr(ns, "no_auth", False)))
    cfg.bind = cfg.bind or codespace.default_bind()

    # Persist the *choice*: "none" stays "none", everything else stores the
    # concrete token so the next run does not invent a new one.
    if cfg.save:
        from dataclasses import replace

        saved = replace(cfg, token="none" if token_choice == "none" else cfg.token)
        path = save_file(saved)
        if not cfg.quiet:
            print(f"  saved   {path}")

    cfg.root_path().mkdir(parents=True, exist_ok=True)
    if cfg.can_upload():
        cfg.inbox_path().mkdir(parents=True, exist_ok=True)
    cfg.state_path().mkdir(parents=True, exist_ok=True)

    if not cfg.save and generated and cfg.token and not config_path().exists():
        from .util import atomic_write_json

        atomic_write_json(config_path(), {"token": cfg.token})

    httpd = srv.serve(cfg)
    port = httpd.port
    fell_back = bool(cfg.port) and port != cfg.port
    style = _style(not cfg.color)

    url = codespace.forwarded_url(port) if codespace.in_codespace() else None
    if url is None:
        host = cfg.bind if cfg.bind not in ("", "0.0.0.0") else "127.0.0.1"
        url = f"http://{host}:{port}"

    # Ask GitHub what the port's visibility *is*, then make it what was asked
    # for.  Never report a value we have not verified.
    vis = None
    if codespace.in_codespace() and cfg.visibility not in ("none", ""):
        timeout = getattr(ns, "visibility_timeout", None) or 30.0
        vis = codespace.ensure_visibility(port, cfg.visibility, timeout=timeout, log=srv.log)
        if not vis.ok:
            srv.log("warn", f"port {port} visibility is {vis.actual or 'unknown'}, "
                            f"not {cfg.visibility}: {vis.detail}")

    _write_runtime(
        {
            "pid": os.getpid(),
            "port": port,
            "url": url,
            "token": cfg.token,
            "mode": cfg.mode,
            "started": time.time(),
        }
    )

    if not cfg.quiet:
        print()
        print(style.bold(f"filebridge {VERSION}") + style.dim(f"   mode {cfg.mode}"))
        print(f"  {style.dim('url    ')} {style.cyan(url)}")
        if fell_back:
            print(f"  {style.dim('note   ')} port {cfg.port} was busy, using {port}")
        if codespace.in_codespace():
            actual = (vis.actual if vis and vis.actual else cfg.visibility)
            note = {
                "private": "GitHub sign-in required at the proxy",
                "org": "organization members only",
                "public": "ANYONE with the URL",
                "internal": "organization members only",
                "none": "forwarding not configured",
            }.get(actual, "")
            print(f"  {style.dim('visible')} {actual}"
                  + (style.dim(f"  ({note})") if note else ""))
            if vis and not vis.ok:
                print(f"  {style.dim('wanted ')} {cfg.visibility} "
                      + style.yellow("(could not apply - see below)"))
        if cfg.token:
            print(f"  {style.dim('access ')} token required")
            print(f"  {style.dim('token  ')} {cfg.token}")
        else:
            print(f"  {style.dim('access ')} "
                  + style.yellow("NO AUTH - anyone who can reach this URL can read and write"))
        if cfg.can_download():
            print(f"  {style.dim('read   ')} {cfg.root_path()}")
        if cfg.can_upload():
            print(f"  {style.dim('write  ')} {cfg.inbox_path()}")
        print(f"  {style.dim('hidden ')} "
              + ("served" if cfg.include_hidden else "dotfiles excluded (--include-hidden)"))
        print(f"  {style.dim('limits ')} {cfg.limits_summary()}")
        print(f"  {style.dim('stop   ')} Ctrl-C   "
              + style.dim("(upload progress survives restarts)"))
        if not cfg.token and (cfg.visibility == "public" or not codespace.in_codespace()):
            print()
            print(style.red("  WARNING: no token and no network isolation.")
                  + " Anyone who can reach this port can read and write these files.")
        if cfg.token and not codespace.in_codespace():
            print(style.dim(f"  tunnel  {codespace.ssh_hint(port)}"))
        if vis and not vis.ok:
            print(style.dim("  fix     " + codespace.manual_snippet(port, cfg.visibility)))
        print(flush=True)

    if cfg.open_browser:
        target = url
        if cfg.token:
            target = f"{url}/#t={cfg.token}"
        try:
            webbrowser.open(target)
        except Exception:
            pass

    def _stop(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        if not cfg.quiet:
            print("\n  stopping...")
    finally:
        httpd.shutdown()
        httpd.server_close()
        try:
            RUNTIME_FILE.unlink()
        except OSError:
            pass
    return 0


# ------------------------------------------------------------------ clients


def cmd_push(ns: argparse.Namespace) -> int:
    api = make_api(ns, quiet=ns.quiet)
    print(f"  push    {len(ns.sources)} source(s) -> {api.host}:{api.port}", flush=True)
    result = xfer.push(
        api,
        ns.sources,
        chunk_size=ns.chunk_size or 0,
        parallel=ns.parallel,
        verify=ns.verify,
        skip_identical=ns.skip_identical,
        exclude=ns.exclude,
        dry_run=ns.dry_run,
    )
    failures = result.get("failures") or []
    print(f"  summary {result['files']} file(s), {human_bytes(result['bytes'])}, "
          f"{result.get('finalized', 0)} finalized, "
          f"{result.get('skipped', 0)} skipped, {len(failures)} failed")
    if failures:
        for row in failures[:10]:
            print(f"    {row}", file=sys.stderr)
        return 1
    return 0


def cmd_pull(ns: argparse.Namespace) -> int:
    api = make_api(ns, quiet=ns.quiet)
    print(f"  pull    {api.host}:{api.port} -> {Path(ns.destination).resolve()}", flush=True)
    result = xfer.pull(
        api,
        ns.destination,
        remote_path=ns.path,
        parallel=ns.parallel,
        overwrite=ns.overwrite,
        verify=ns.verify,
        exclude=ns.exclude,
        dry_run=ns.dry_run,
    )
    failures = result.get("failures") or []
    print(f"  summary {result['files']} file(s), {human_bytes(result['bytes'])}, "
          f"{result.get('skipped', 0)} skipped, {len(failures)} failed")
    if failures:
        for row in failures[:10]:
            print(f"    {row}", file=sys.stderr)
        return 1
    return 0


def cmd_status(ns: argparse.Namespace) -> int:
    api = make_api(ns, quiet=True)
    info = api.info()
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def cmd_token(ns: argparse.Namespace) -> int:
    data = load_file()
    token = data.get("token") or runtime_info().get("token") or ""
    if token in ("none", ""):
        token = ""
    if ns.rotate or not token:
        token = generate_token()
        data["token"] = token
        from .util import atomic_write_json

        atomic_write_json(config_path(), data)
        print(token)
        return 0
    print(token)
    return 0


def cmd_url(ns: argparse.Namespace) -> int:
    port = ns.port or runtime_info().get("port") or load_file().get("port") or 5555
    url = codespace.forwarded_url(int(port))
    if url:
        print(url)
        ports = codespace.list_ports()
        actual = (ports or {}).get(int(port))
        print(f"visibility: {actual or 'unknown'}")
        requested = load_file().get("visibility", "private")
        if requested and requested != "none" and actual != requested:
            print(f"requested:  {requested}")
            print("apply with: " + codespace.manual_snippet(int(port), requested))
    else:
        print(f"http://127.0.0.1:{port}")
        print("(not in a codespace; use the port as-is or a tunnel)")
    print(codespace.ssh_hint(int(port)))
    return 0


def cmd_visibility(ns: argparse.Namespace) -> int:
    port = ns.port or runtime_info().get("port") or load_file().get("port") or 5555
    target = ns.to or load_file().get("visibility") or "private"
    if target == "none":
        print("nothing to do (visibility is 'none')")
        return 0
    if not codespace.in_codespace():
        print("not inside a codespace: nothing to forward")
        return 0
    result = codespace.ensure_visibility(
        int(port), target, timeout=ns.timeout,
        log=lambda kind, message: print(f"  {kind}: {message}", file=sys.stderr),
    )
    print(f"port {int(port)}: {result.describe()}")
    if not result.ok:
        print("run this by hand:")
        print("  " + codespace.manual_snippet(int(port), target))
        return 1
    return 0


def cmd_selftest(ns: argparse.Namespace) -> int:
    from . import selftest

    return selftest.run(keep=ns.keep)


# ------------------------------------------------------------------ main


def _print_version() -> int:
    print(f"filebridge {VERSION}")
    return 0


def _print_help() -> int:
    build_parser().print_help()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"]:
        return _print_version()
    if argv in (["-h"], ["--help"]):
        return _print_help()
    if not argv:
        argv = ["serve"]
    elif argv[0] not in SUBCOMMANDS and argv[0].startswith("-"):
        argv = ["serve"] + argv

    parser = build_parser()
    ns = parser.parse_args(argv)

    handlers = {
        "serve": cmd_serve,
        "setup": cmd_serve,
        "push": cmd_push,
        "pull": cmd_pull,
        "status": cmd_status,
        "token": cmd_token,
        "url": cmd_url,
        "visibility": cmd_visibility,
        "selftest": cmd_selftest,
    }
    handler = handlers.get(ns.command or "serve")
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(ns)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except (xfer.Fatal, xfer.Retryable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
