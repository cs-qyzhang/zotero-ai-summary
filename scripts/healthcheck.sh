#!/usr/bin/env bash
# Simple liveness probe for the summarizer.  Restarts the unit when /healthz
# stops answering or reports a wedged state.
#
# Install (as root) with a timer, e.g. /etc/systemd/system/paper-summarizer-healthcheck.{service,timer}:
#   [Service] Type=oneshot
#   ExecStart=/path/to/zotero-ai-summary/scripts/healthcheck.sh
#   [Timer] OnUnitActiveSec=2min / OnBootSec=5min
set -uo pipefail

URL="${1:-http://127.0.0.1:13210/healthz}"
UNIT="${UNIT:-paper-summarizer}"
MAX_INFLIGHT_WEDGE="${MAX_INFLIGHT_WEDGE:-0}"   # 0 = 只检查可达性
STAMP="${STAMP:-/tmp/paper-summarizer-healthcheck.last}"

body=$(curl -fsS --max-time 10 "$URL" 2>/dev/null) || body=""
if [[ -z "$body" ]]; then
    echo "healthz unreachable -> restarting $UNIT"
    systemctl reset-failed "$UNIT" 2>/dev/null
    systemctl restart "$UNIT"
    exit 0
fi

# 连续 3 次 inflight 卡住不动（说明 worker 没回收），才认为卡死
inflight=$(echo "$body" | sed -n 's/.*"inflight":\([0-9]*\).*/\1/p')
prev=$(cat "$STAMP" 2>/dev/null || echo "")
if [[ -n "$inflight" && "$inflight" == "$prev" && "$inflight" -gt 0 ]]; then
    echo "inflight stuck at $inflight -> restarting $UNIT"
    systemctl reset-failed "$UNIT" 2>/dev/null
    systemctl restart "$UNIT"
    echo "0" > "$STAMP"
    exit 0
fi
echo "${inflight:-0}" > "$STAMP"
echo "ok: $body"
