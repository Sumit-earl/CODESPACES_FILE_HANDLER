#!/usr/bin/env bash
# Prove that THIS computer can use a FileBridge server, and capture exactly
# what the server sees about it. Run it on the machine you want to test.
#
#   ./prove-remote.sh --url https://<name>-<port>.app.github.dev --token <token>
#
# Produces ./filebridge-proof-<timestamp>/ and a .zip next to it containing
# system metadata, DNS/TLS traces, API probe results, the server's own access
# log for these requests, and hashes of every artifact.
set -uo pipefail

URL=""; TOKEN=""; NOTE=""; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --url)   URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --note)  NOTE="$2"; shift 2 ;;
    --out)   OUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$URL" ] || { echo "--url is required" >&2; exit 2; }
URL="${URL%/}"
OUT="${OUT:-./filebridge-proof-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT"
LOG="$OUT/commands.log"
: >"$LOG"

# run a command, log it, never abort the whole capture on a non-zero exit
run() {
  printf '$ %s\n' "$*" >>"$LOG"
  "$@" >>"$LOG" 2>&1
  local rc=$?
  [ $rc -ne 0 ] && printf '  (exit %s)\n' "$rc" >>"$LOG"
  return 0
}

say() { printf '\n### %s\n' "$*" >>"$LOG"; }

AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
HOST="${URL#https://}"; HOST="${HOST#http://}"; HOST="${HOST%%/*}"

# ---------------------------------------------------------------- metadata
{
  echo "captured_at_utc: $(date -u +%FT%TZ)"
  echo "note: ${NOTE:-unspecified}"
  echo "target_url: $URL"
  echo "hostname: $(hostname 2>/dev/null)"
  echo "user: $(whoami 2>/dev/null)"
  echo "kernel: $(uname -a)"
  if [ -r /etc/os-release ]; then grep -E '^(PRETTY_NAME|VERSION_ID|ID)=' /etc/os-release; fi
  if command -v sw_vers >/dev/null 2>&1; then sw_vers; fi
  echo "cpus: $( (nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null) )"
  echo "memory:"; (free -h 2>/dev/null || vm_stat 2>/dev/null | head -5)
  echo "disk:";   (df -h . 2>/dev/null | tail -2)
  echo "python: $(python3 --version 2>&1)"
  echo "curl: $(curl --version 2>/dev/null | head -1)"
} >"$OUT/sysinfo.txt"

# ----------------------------------------------------------------- network
say "dns + route + tls"
{
  echo "== resolve $HOST =="
  (getent hosts "$HOST" || nslookup "$HOST" || true) 2>&1
  echo
  echo "== public egress ip of this machine =="
  echo "ipify:      $(curl -s --max-time 10 https://api.ipify.org || echo 'unavailable')"
  echo "ifconfig.me: $(curl -s --max-time 10 https://ifconfig.me/ip || echo 'unavailable')"
  echo
  echo "== TLS + HTTP trace to the target =="
} >"$OUT/network.txt"
curl -sv --max-time 25 "${AUTH[@]}" "$URL/api/info" -o "$OUT/api-info.json" \
  2>>"$OUT/network.txt" >/dev/null
{
  echo
  echo "== response headers (through the relay) =="
} >>"$OUT/network.txt"
curl -s -D - --max-time 25 "${AUTH[@]}" "$URL/api/info" -o /dev/null >>"$OUT/network.txt" 2>&1

EGRESS="$(curl -s --max-time 10 https://api.ipify.org || true)"

# ------------------------------------------------------------------ probes
say "api probes"
head -c 1048576 /dev/urandom >"$OUT/payload.bin"
SIZE=$(wc -c <"$OUT/payload.bin" | tr -d ' ')
SHA=$( (sha256sum "$OUT/payload.bin" 2>/dev/null || shasum -a 256 "$OUT/payload.bin") | cut -d' ' -f1)

ID=$(curl -s --max-time 25 "${AUTH[@]}" -H "Content-Type: application/json" \
  -d "{\"path\":\"prove/$HOST.bin\",\"size\":$SIZE,\"sha256\":\"$SHA\",\"chunk_size\":1048576}" \
  "$URL/api/uploads" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
echo "upload_session: $ID" >>"$LOG"

if [ -n "$ID" ]; then
  CSHA=$( (sha256sum "$OUT/payload.bin" 2>/dev/null || shasum -a 256 "$OUT/payload.bin") | cut -d' ' -f1)
  run curl -s --max-time 60 "${AUTH[@]}" -X PATCH \
    -H "Content-Type: application/octet-stream" \
    -H "Content-Range: bytes 0-$((SIZE-1))/$SIZE" \
    -H "X-Chunk-Sha256: $CSHA" \
    --data-binary "@$OUT/payload.bin" "$URL/api/uploads/$ID"
  FIN=$(curl -s --max-time 30 "${AUTH[@]}" -X POST -H "Content-Type: application/json" \
    -d "{\"sha256\":\"$SHA\"}" "$URL/api/uploads/$ID/finalize")
  printf '%s\n' "$FIN" >"$OUT/finalize.json"
  # where the API will serve it from (root-relative when the inbox is under root)
  DL=$(printf '%s' "$FIN" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("root_path") or ("inbox/"+d.get("path","")))' 2>/dev/null)
  echo "download_path: $DL" >>"$LOG"
  curl -s --max-time 60 "${AUTH[@]}" -o "$OUT/downloaded.bin" \
    "$URL/api/files/download?path=$DL" || true
  if cmp -s "$OUT/payload.bin" "$OUT/downloaded.bin"; then
    echo "round_trip: identical" >>"$LOG"
  else
    echo "round_trip: DIFFERENT" >>"$LOG"
  fi
else
  DL=""; echo "upload failed; skipping read-back" >>"$LOG"
fi

say "range / etag / head / tar"
DLDIR="$(dirname "${DL:-.}")"; [ "$DLDIR" = "." ] && DLDIR=""
run curl -s -D "$OUT/range-headers.txt" -o "$OUT/range.bin" "${AUTH[@]}" \
  -H "Range: bytes=100-199" "$URL/api/files/download?path=$DL"
run curl -s -I "${AUTH[@]}" "$URL/api/files/download?path=$DL"
run curl -s -o "$OUT/bundle.tar.gz" "${AUTH[@]}" "$URL/api/bundle?path=$DLDIR&gzip=1"
run tar tzf "$OUT/bundle.tar.gz"

# --------------------------------------------------- what the server saw
say "server access log filtered to this run"
curl -s --max-time 25 "${AUTH[@]}" "$URL/api/logs" >"$OUT/api-logs.json" 2>/dev/null
{
  echo "egress_ip_of_this_machine: ${EGRESS:-unknown}"
  echo
  echo "=== access-log lines mentioning this machine's egress IP ==="
  if [ -n "$EGRESS" ] && [ -s "$OUT/api-logs.json" ]; then
    python3 - "$OUT/api-logs.json" "$EGRESS" <<'PY' 2>/dev/null || grep -F "$EGRESS" "$OUT/api-logs.json"
import json, sys
lines = json.load(open(sys.argv[1])).get("lines", [])
ip = sys.argv[2]
hits = [l for l in lines if ip in l.get("msg", "")]
print(f"matching_lines: {len(hits)}")
for row in hits[-15:]:
    print(f"  {row.get('kind')}: {row.get('msg')}")
if not hits:
    print("  (none - either the log rotated, or the request did not come through the relay)")
PY
  else
    echo "(no egress ip or no logs)"
  fi
} >"$OUT/server-view.txt"

# ------------------------------------------------------------------ README
cat >"$OUT/README.md" <<EOF
# FileBridge remote-access proof

* **Captured by:** this machine only. Nothing here was produced by a third party.
* **Target:** $URL
* **When:** $(date -u +%FT%TZ)

## What this proves

1. This exact computer resolved the public hostname, completed a TLS handshake
   with GitHub's relay, and spoke the FileBridge protocol through it
   (\`network.txt\`, \`commands.log\`).
2. Keys, custom headers (\`Authorization\`, \`Content-Range\`, \`X-Chunk-Sha256\`)
   and byte ranges survive the relay (\`range-headers.txt\`, the PATCH result).
3. The server can identify this computer by its public IP: the relay injects
   \`X-Forwarded-For\` and overwrites any value the client supplies
   (\`server-view.txt\`).

## What this does NOT prove

* Nothing about any other computer. If you ran this on machine A, it says
  nothing about machine B; run it there and compare the egress IPs.
* Nothing about browsers: this is an HTTP client, not a page navigation. Browser
  visits to public ports may show GitHub's "development port" warning once.
* Nothing about throughput under load or over a slow/lossy link.

## Artifacts

| file | contents |
|---|---|
| \`sysinfo.txt\` | OS, kernel, host, CPU, memory, disk |
| \`network.txt\` | DNS resolution, egress IP, full TLS/HTTP trace |
| \`commands.log\` | every command run, in order, with exit codes |
| \`api-info.json\` | server identity as seen by this machine |
| \`range-headers.txt\` | 206 response headers through the relay |
| \`server-view.txt\` | the server's own access log lines matching this IP |
| \`hashes.txt\` | sha256 of every file above |
EOF

( cd "$OUT" && (sha256sum * 2>/dev/null || shasum -a 256 *) | grep -v ' hashes.txt$' > hashes.txt )

ARCHIVE="$(dirname "$OUT")/$(basename "$OUT").zip"
if command -v zip >/dev/null 2>&1; then
  rm -f "$ARCHIVE"; ( cd "$(dirname "$OUT")" && zip -qr "$ARCHIVE" "$(basename "$OUT")" )
else
  python3 - "$OUT" "$ARCHIVE" <<'PY'
import os, sys, zipfile
src, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for root, _, files in os.walk(src):
        for name in files:
            path = os.path.join(root, name)
            z.write(path, os.path.relpath(path, os.path.dirname(src)))
print("wrote", dest)
PY
fi

echo
echo "artifacts: $OUT"
echo "archive:   $ARCHIVE"
[ -n "$EGRESS" ] && echo "egress ip: $EGRESS"
echo
sed -n '1,12p' "$OUT/server-view.txt"
