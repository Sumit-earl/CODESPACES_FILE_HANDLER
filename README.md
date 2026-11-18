# FileBridge

Resumable bulk file transfer over a forwarded port — built for GitHub Codespaces,
where moving a few hundred MB through the editor's web UI freezes the window.

One command, zero dependencies: it runs on the `python3` already in every
Codespaces image.

```bash
./filebridge                 # start with safe defaults, no questions
./filebridge setup           # the same, but answer a few questions
./filebridge push ./dir --to https://<name>-<port>.app.github.dev --token <token>
./filebridge pull ./dir --from https://<name>-<port>.app.github.dev --token <token>
```

### Running it without the launcher

`./filebridge` is a 20-line shim. The program itself is
`tools/filebridge/__main__.py`:

```bash
python3 tools/filebridge/__main__.py            # run the file directly
PYTHONPATH=tools python3 -m filebridge          # package form
```

All three call the same `cli.main()`.

### No install, no downloads

Pure standard library. Nothing is fetched at startup — no pip, no wheels, no
fonts, no update check. The only optional external tool is the GitHub CLI
(`gh`), used *only* when a forwarded port's visibility has to change or be
verified; if it is missing you get a printed snippet instead. Python 3.9+ is
checked explicitly, with a readable message.

Full documentation: [`tools/filebridge/README.md`](tools/filebridge/README.md).

## Why it exists

VS Code Web sends an upload as **one RPC frame on the same WebSocket** that
carries keystrokes and file operations. One large frame therefore blocks the tab,
head-of-line-blocks every other message on that socket, and has no chunking or
resume — so the editor "freezes" or the upload stalls at 47% forever.

FileBridge moves bulk bytes to a separate HTTP data plane made of small,
resumable, checksummed requests. The editor's control channel is never touched.

## Layout

```
filebridge                     # single entry point (launcher)
tools/filebridge/              # the package
  cli.py  config.py  wizard.py # command line / config / interactive setup
  server.py  store.py          # HTTP data plane, resumable sessions
  client.py  codespace.py      # push/pull client, port URLs + visibility
  ui.html                      # browser UI (vanilla JS, no build step)
  selftest.py  tests/          # end-to-end and unit tests
  scripts/prove-remote.sh      # evidence bundle from any machine
```

## Verify

```bash
./filebridge selftest                          # end-to-end checks
python3 tools/filebridge/tests/test_units.py
python3 tools/filebridge/tests/test_visibility.py
python3 tools/filebridge/tests/test_wizard_keys.py
ruff check tools/filebridge
```

## Requirements

Python 3.9+ (tested on 3.14). Standard library only — nothing to install, nothing
to build, no lockfile.
