#!/usr/bin/env bash
# Profile the memory of one live uvicorn worker with memray (plan W8).
#
# Attaches to the worker with the largest RSS (or PID=<pid>), records for
# DURATION seconds (default 3 h, long enough to cover AutoSync cycles), then
# writes a flamegraph of the peak heap plus a text summary.
#
#   sudo apps/api/scripts/profile_worker_memory.sh            # biggest worker, 3 h
#   sudo DURATION=1800 PID=12345 apps/api/scripts/profile_worker_memory.sh
#
# Needs gdb (sudo apt-get install -y gdb) and ptrace rights over the worker,
# hence sudo. memray is installed into the API venv on first run. Attaching
# slows the profiled worker a little; the other workers are untouched.
set -euo pipefail

API_DIR="${API_DIR:-/home/ubuntu/codebase/airecruiter/apps/api}"
VENV="${VENV:-$API_DIR/venv}"
DURATION="${DURATION:-10800}"
OUT_DIR="${OUT_DIR:-/var/tmp/memray}"

command -v gdb >/dev/null || { echo "gdb is required: sudo apt-get install -y gdb" >&2; exit 1; }
"$VENV/bin/python" -c "import memray" 2>/dev/null || "$VENV/bin/pip" install --quiet memray

if [ -z "${PID:-}" ]; then
    # Workers are the uvicorn processes whose parent is also uvicorn (the master).
    PID=$(ps -eo pid=,ppid=,rss=,args= | awk '
        /uvicorn main:app/ { pid[$1]=1; ppid[$1]=$2; rss[$1]=$3 }
        END { for (p in pid) if (ppid[p] in pid && rss[p] > best) { best=rss[p]; b=p } print b }')
fi
[ -n "$PID" ] || { echo "no uvicorn worker found" >&2; exit 1; }

mkdir -p "$OUT_DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BIN="$OUT_DIR/worker-$PID-$STAMP.bin"
echo "worker $PID: RSS $(ps -o rss= -p "$PID" | awk '{printf "%.1f GB", $1/1048576}'), recording ${DURATION}s -> $BIN"

# --aggregate keeps the capture file small over hours of recording.
"$VENV/bin/memray" attach --aggregate --duration "$DURATION" -o "$BIN" "$PID"

# attach returns once tracking starts; wait for the capture to be finalised.
sleep "$((DURATION + 30))"

"$VENV/bin/memray" flamegraph -f -o "${BIN%.bin}.html" "$BIN"
"$VENV/bin/memray" stats "$BIN" > "${BIN%.bin}.stats.txt"
echo "flamegraph: ${BIN%.bin}.html"
echo "stats:      ${BIN%.bin}.stats.txt"
echo "RSS after:  $(ps -o rss= -p "$PID" 2>/dev/null | awk '{printf "%.1f GB", $1/1048576}')"
