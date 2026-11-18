# FileBridge

Resumable bulk file transfer over a forwarded port.

Built for GitHub Codespaces (and any cloud dev box) where moving a few GB through
the editor's web UI freezes the window. One command, standard library only, no
build step.

```bash
./filebridge                            # from the repository root (launcher)
python3 tools/filebridge/__main__.py    # the program itself, run directly
PYTHONPATH=tools python3 -m filebridge  # package form
```

---

## Why this exists

VS Code Web sends a file upload as **one RPC frame on the same long-lived
WebSocket** that carries keystrokes, diagnostics and file operations. A large
frame therefore:

* blocks the tab's main thread while it reads and encodes the whole file;
* blocks every other message on that socket, so the explorer stops responding
  while the terminal (a *separate* connection) keeps working;
* has no chunking, no honest progress and no resume. If the codespace suspends
  at 47%, the next attempt starts at 0%.

FileBridge moves bulk bytes onto a separate HTTP data plane made of small,
bounded, retryable requests. The editor's control channel is never touched.

---

## Quick start

Commands below assume the repository root (`cd` there first).

```bash
./filebridge                 # safe defaults, zero questions
./filebridge setup           # same, but answer a few questions
./filebridge --help
```

Defaults: mode `both`, **read = the directory you run it from**, write =
`./inbox`, state = `./.filebridge`, port `5555` (falls back to a free one if
busy), generated token, port visibility **private**, dotfiles hidden.

The banner prints everything you need:

```
filebridge 0.1.0   mode both
  url     https://mycodespace-5555.app.github.dev
  visible private  (GitHub sign-in required at the proxy)
  access  token required
  token   Xk3-…
  read    /workspaces/repo
  write   /workspaces/repo/inbox
  hidden  dotfiles excluded (--include-hidden)
  limits  4.0 MB chunks · 3 parallel · 16.0 GB/file · 64.0 GB total
  stop    Ctrl-C   (upload progress survives restarts)
```

Open the URL, paste the token, drag files in.

### Requirements

Python 3.9+ (checked at import; tested on 3.14). Standard library only — nothing
to install, nothing to build, no lockfile, no network access at startup.

The only optional external tool is `gh`, used *only* when a forwarded port's
visibility has to change or be verified. `--visibility none` skips that check,
making startup fully offline.

### Bulk, from anywhere

```bash
# upload (resumes automatically if it dies halfway)
./filebridge push ./assets --to https://<name>-<port>.app.github.dev --token Xk3-…

# download a tree
./filebridge pull ./assets --from https://<name>-<port>.app.github.dev --token Xk3-… --path inbox

# what is running?
./filebridge status
```

Re-running `push` or `pull` after a crash, a suspend or a dead link continues
from the last committed chunk instead of starting over.

---

## The interactive layer

`setup` is a real menu, not a questionnaire you have to type numbers into:

```
? How should clients authenticate?
  ❯ auto-token (generated now)
    my-own-token
    no-auth
  ↑/↓ move · 1-3 select · 0/Enter default · Esc accept
```

* `↑`/`↓` (or `j`/`k`, `←`/`→`) move
* `1`…`N` pick directly, `0` or `Enter` takes the default
* `Esc` accepts whatever is highlighted

Every question has a default, so holding Enter produces a safe configuration.
Piped/CI input falls back to the same numbered prompt (and treats EOF as
"accept the default"). `--save` writes the answers to `.filebridge/config.json`
so the next run is a single command.

---

## How transfers work

```
POST   /api/uploads                     create a session (path, size, sha256)
PATCH  /api/uploads/{id}                one chunk, Content-Range + X-Chunk-Sha256
GET    /api/uploads/{id}                which byte ranges are still missing
POST   /api/uploads/{id}/finalize       verify + atomic rename into the inbox
DELETE /api/uploads/{id}                discard
```

Properties that matter:

* **Any chunk, any order, any number of times.** Chunks are written at their
  offset into a sparse `.part` file and recorded by index. Re-sending a chunk is
  a no-op, so "the response was lost" is indistinguishable from "fine".
* **Resume is server state.** `sessions.json` plus the `.part` files are fsynced
  per chunk and reloaded on startup, so a suspend, an OOM kill or Ctrl-C does
  not lose progress. Sessions are kept for `--session-ttl` hours (default 24).
* **Integrity twice.** Per-chunk SHA-256 on write; whole-file SHA-256 at
  finalize. The CLI always sends one; the browser sends one for files ≤ 64 MB
  and falls back to size+mtime matching above that.
* **Atomic publish.** Bytes land in a staging file; `os.replace()` is the only
  moment a file appears in the inbox. No half-written files.
* **Small requests.** 4 MiB chunks stay under proxy body limits and keep every
  request short, so a stalled transfer cannot starve the editor.

Downloads use `Range`, so they resume from the byte where they stopped.

---

## Port visibility (why `gh cs ports visibility` says "Not Found")

GitHub makes forwarded ports **private** by default. Turning one public is the
part that fights back, and it is worth understanding why:

`gh cs ports visibility` does **not** use the Codespaces REST API. It resolves
your codespace's Dev Tunnel and calls the tunnel service:

```
GET /tunnels/{tunnelId}/ports/{port}?includePorts=true   ->  404 Not Found
```

A *port entity* only exists in the tunnel after something registers it, and
merely listening on a port does not do that. Meanwhile `gh cs ports` (the
Codespaces API) happily lists the port — which is why a tool can think a port is
public while the browser still asks you to sign in.

**The fix:** open a temporary `gh cs ports forward <port>:<local>` so the tunnel
creates the entity, flip visibility, verify, then drop the forward. The port
record and its visibility outlive the forward. FileBridge does this automatically
and reports only what it verified:

```
  visible  public  (ANYONE with the URL)
```

If it cannot apply the change you get the real state and the exact commands:

```
  visible  private  (GitHub sign-in required at the proxy)
  wanted   public  (could not apply - see below)
  fix      gh cs ports forward 5555:15555 -c "$CODESPACE_NAME" & f=$!; sleep 4; \
           gh cs ports visibility 5555:public -c "$CODESPACE_NAME"; kill $f
```

Apply or re-check at any time, without restarting the server:

```bash
./filebridge visibility --to public --port 5555
# port 5555: public
```

`--visibility-timeout 60` gives a slow tunnel longer to register.

### Prefer not to publish at all

```bash
# VS Code Desktop auto-tunnels forwarded ports to your own machine:
#   http://localhost:5555 just works, no visibility change.

# or tunnel it yourself:
gh cs ssh -c "$CODESPACE_NAME" -- -N -L 5555:localhost:5555
```

---

## Security model

Two independent knobs — the thing that confuses everyone:

| | **access** | **visible** |
|---|---|---|
| meaning | who can use FileBridge once they reach the URL | who can reach the port at all |
| values | `token required`, `NO AUTH` | `private`, `org`, `public` |
| default | token required | private |

`private` does **not** mean "no auth", and `NO AUTH` does **not** make the port
public. GitHub's proxy gates the URL; the token gates the files. `--no-auth`
removes only the second gate, and the banner says so in red.

Other defaults:

* **Dotfiles are hidden.** `.env`, `.git`, `.filebridge` are skipped by listings
  and rejected on direct download (`403`). `--include-hidden` serves them; the
  state directory is always blocked.
* **Paths are sandboxed** to the root/inbox: absolute paths, `..`, NUL bytes and
  escaping symlinks are rejected (`400`).
* **Delete is off** unless you pass `--allow-delete`.
* **Tokens never appear in logs.** `?t=…` is redacted; the UI keeps the token in
  `sessionStorage` and accepts it via the URL *hash* (`#t=…`), which is not sent
  to the server.
* **Quota preflight** rejects a file that would not fit (5% + 32 MB headroom)
  with `507` instead of filling the disk and killing your editor.

A public port plus `--no-auth` means anyone with the URL can read and write the
workspace. GitHub shows them a one-time "development port" warning; that
warning protects *them*, not you.

---

## Commands

| command | purpose |
|---|---|
| `./filebridge` / `serve` | start the server (no questions) |
| `setup` | interactive wizard; `--save` remembers the answers |
| `push SRC…` | upload files/directories |
| `pull DEST` | download a tree |
| `status` | print `/api/info` |
| `token [--rotate]` | print or regenerate the token |
| `url` | forwarded URL, real visibility, tunnel command |
| `visibility --to public` | register the port, flip visibility, verify |
| `selftest` | end-to-end checks (resume, ranges, auth, traversal, restart) |

Serve flags: `--mode both\|upload\|download`, `--root`, `--inbox`, `--state`,
`--bind`, `--port`, `--token` (`auto`/`none`/literal), `--no-auth`,
`--visibility private\|public\|org\|none`, `--visibility-timeout`,
`--chunk-size 4M`, `--parallel 3`, `--max-file`, `--max-total`,
`--on-exist rename\|overwrite\|fail`, `--include-hidden`, `--session-ttl`,
`--cors ORIGIN`, `--allow-delete`, `--open`, `--save`, `-i/--interactive`,
`--quiet`, `-v`, `--no-color`.

Client flags: `--to`/`--from`, `--token`, `--parallel`, `--retries`, `--timeout`,
`--insecure`, `--exclude GLOB`, `--dry-run`, `--skip-identical`, `--no-verify`,
`--overwrite`, `--verify`, `--path`.

Configuration precedence: **CLI → `FB_*` environment → `.filebridge/config.json`
→ defaults.** Useful environment variables: `FB_PORT`, `FB_TOKEN`, `FB_ROOT`,
`FB_INBOX`, `FB_MODE`, `FB_VISIBILITY`, `FB_PARALLEL`, `FB_CHUNK_SIZE`,
`FB_MAX_FILE`, `FB_MAX_TOTAL`.

---

## HTTP API

Auth: `Authorization: Bearer <token>`. `GET /api/info` and `GET /healthz` are
public. Errors are JSON: `{"error": "...", "code": 409, ...}`.

| method | path | notes |
|---|---|---|
| `GET` | `/` | browser UI |
| `GET` | `/healthz` | liveness |
| `GET` | `/api/info` | mode, limits, quota, free space |
| `POST` | `/api/auth` | validate a token |
| `POST` | `/api/uploads` | `{path,size,sha256?,chunk_size?,mtime?}` → session |
| `GET` | `/api/uploads` | open sessions + recent transfers |
| `GET` | `/api/uploads/{id}` | committed offset, missing ranges |
| `PATCH` | `/api/uploads/{id}` | `Content-Range: bytes a-b/total`, raw body, optional `X-Chunk-Sha256` |
| `POST` | `/api/uploads/{id}/finalize` | `{sha256?}` → `{path, root_path, size}` |
| `DELETE` | `/api/uploads/{id}` | discard |
| `GET` | `/api/files/tree?path=` | recursive listing (size, mtime, etag) |
| `GET` | `/api/files/stat?path=&hash=1` | stat, optionally with sha256 |
| `GET` | `/api/files/download?path=` | bytes; `Range`, `ETag`, `?inline=1` |
| `GET` | `/api/bundle?path=&gzip=1` | streaming tar of a directory |
| `DELETE` | `/api/files?path=` | only with `--allow-delete` |
| `GET` | `/api/logs` | last 400 log lines |

`finalize` returns `root_path` when the inbox lives under the served root, so a
client knows exactly what to `GET` afterwards.

```bash
# upload one file with curl (one chunk)
curl -X POST "$URL/api/uploads" -H "Authorization: Bearer $T" \
  -H 'Content-Type: application/json' \
  -d '{"path":"demo.bin","size":1048576,"chunk_size":1048576}'
curl -X PATCH "$URL/api/uploads/<id>" -H "Authorization: Bearer $T" \
  -H 'Content-Range: bytes 0-1048575/1048576' --data-binary @demo.bin
curl -X POST "$URL/api/uploads/<id>/finalize" -H "Authorization: Bearer $T" -d '{}'
```

---

## Browser notes

* **One warning per codespace session.** Public ports show GitHub's "you are
  about to access a development port" page before your app loads. It is served by
  GitHub's relay, cannot be disabled, and only fires for **navigation** — API
  calls, `fetch`, XHR and `curl` go straight through.
* **CORS is off by default.** A page on another origin cannot read responses
  until you pass `--cors https://that.origin`. The bundled UI is same-origin and
  needs nothing.
* **Downloads** stream through the File System Access API (progress, pause,
  resume, no RAM blow-up) where available; otherwise they fall back to a plain
  link.

---

## Operations

| concern | behaviour |
|---|---|
| server killed / codespace suspend | sessions survive; `push`/`pull` resume |
| destination exists | `--on-exist rename` (default), `overwrite`, `fail` |
| disk full | `507` before writing; never fills the codespace |
| idle clients | 120 s socket timeout, no thread pinned forever |
| concurrency | bounded semaphore; overload returns `503 Retry-After` |
| client retries | exponential backoff + jitter on 408/425/429/5xx and proxy HTML |
| proxy page instead of JSON | detected by content-type, reported as "port not forwarded" |

---

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `HTTP 502/503` with an HTML body | GitHub's proxy answered, not FileBridge | codespace asleep or port not forwarded; the client retries |
| upload stops at the same % every time | a proxy body limit | lower `--chunk-size 1M` |
| `401` | wrong/absent token | `./filebridge token`, or `#t=<token>` in the UI URL |
| browser hangs at 0% | background-tab throttling | keep the tab focused, or use `push` |
| `gh: Not Found` on visibility | the port has no Dev Tunnels entity yet | `./filebridge visibility --to public --port N` |
| banner says public, browser asks to sign in | the flip never applied (old bug) | re-run `visibility` and read the verified result |
| `port 5555 was busy, using 41241` | another process owns 5555 | the banner now says so; use `--port 0` or stop it |
| server dies when the terminal closes | no supervisor | `tmux`/`nohup`, or a devcontainer `postStartCommand` |

A failed transfer is never a lost one: sessions stay resumable for
`--session-ttl` hours.

---

## Development

```
filebridge                      # single entry point (launcher)
tools/filebridge/
  cli.py          argument parsing, serve lifecycle, subcommands
  config.py       defaults, config file, env, validation
  wizard.py       interactive layer (arrow-key menu with plain fallback)
  server.py       HTTP data plane (stdlib http.server)
  store.py        resumable sessions, atomic publish, locking
  client.py       push/pull, retries with jitter, progress
  codespace.py    forwarded URLs, Dev Tunnels port visibility
  util.py         sizes, safe paths, hashing, atomic JSON
  ui.html         single-page UI (vanilla JS, no build)
  selftest.py     end-to-end checks
  scripts/prove-remote.sh   evidence bundle from any machine
  tests/          unit, visibility (fake gh), wizard (real pty)
  ruff.toml       lint config (line length 100)
```

```bash
./filebridge selftest                                # 40+ end-to-end checks
python3 tools/filebridge/tests/test_units.py          # paths, ranges, sizes
python3 tools/filebridge/tests/test_visibility.py     # Dev Tunnels flow
python3 tools/filebridge/tests/test_wizard_keys.py    # arrow keys via pty
ruff check tools/filebridge
```

### Proving it works from another machine

```bash
tools/filebridge/scripts/prove-remote.sh \
  --url https://<name>-<port>.app.github.dev --token <token> --note "my laptop"
```

Writes a zip with system metadata, DNS/TLS traces, API results, and the server's
own access log filtered to your egress IP (`X-Forwarded-For` is injected by the
relay and cannot be spoofed through it).

---

## Known limitations

* Empty directories are not transferred; files only.
* Browser uploads above 64 MB carry no whole-file hash (SubtleCrypto cannot
  stream); integrity rests on per-chunk hashes plus size/mtime resume matching.
  The CLI always sends a full hash.
* A 2-core codespace running this plus the IDE will contend; default
  `--parallel 3` is deliberate.
* No TLS inside the container — GitHub terminates TLS at the proxy. Use the SSH
  tunnel for raw local access.
* The Codespaces relay adds a hop; sustained multi-GB public traffic may be
  throttled and is a Terms-of-Service grey area.
