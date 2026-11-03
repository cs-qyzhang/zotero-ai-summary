#!/usr/bin/env bash
# Regression / stress harness for the PDF summarizer server.
#
# Usage:
#   ./scripts/regress.sh [BASE_URL] [SERVER_PID]
#     BASE_URL    default http://127.0.0.1:13299  (start a test instance!)
#     SERVER_PID  optional; used for RSS / uploads-dir bookkeeping
#                 (auto-detected from the listening port when possible)
#
# Each case reports: HTTP code, wall time, peak RSS of the server tree,
# peak RSS of the worker child, and leftover files in uploads/.
set -uo pipefail

BASE_URL="${1:-http://127.0.0.1:13299}"
SERVER_PID="${2:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(cd "$HERE/.." && pwd)"

# 解释器解析顺序：$PY > venv > 已安装 systemd unit 的 ExecStart > python3
if [[ -z "${PY:-}" ]]; then
    for cand in "$WORKDIR/.venv/bin/python" "$WORKDIR/venv/bin/python" "${VIRTUAL_ENV:-}/bin/python"; do
        [[ -n "$cand" && -x "$cand" ]] && { PY="$cand"; break; }
    done
fi
if [[ -z "${PY:-}" ]]; then
    unit_py="$(awk -F= '/^ExecStart=/{print $2; exit}' \
        /etc/systemd/system/paper-summarizer.service 2>/dev/null | awk '{print $1}')"
    [[ -n "$unit_py" && -x "$unit_py" ]] && PY="$unit_py"
fi
PY="${PY:-$(command -v python3 || command -v python)}"
FIXTURES="${FIXTURES:-/tmp/zotero-regress}"
RESULTS="$FIXTURES/results"
PORT="${BASE_URL##*:}"
PORT="${PORT%%/*}"

mkdir -p "$FIXTURES" "$RESULTS"

if [[ -z "$SERVER_PID" ]]; then
    SERVER_PID="$(ss -ltnpH "sport = :$PORT" 2>/dev/null \
        | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
fi
if [[ -n "${UPLOADS_DIR_OVERRIDE:-}" ]]; then
    UPLOADS_DIR="$UPLOADS_DIR_OVERRIDE"
elif [[ -n "$SERVER_PID" && -d "/proc/$SERVER_PID/cwd" ]]; then
    UPLOADS_DIR="$(readlink "/proc/$SERVER_PID/cwd")/uploads"
else
    UPLOADS_DIR="$WORKDIR/uploads"
fi

echo "== fixtures =="
"$PY" "$HERE/mk_small_pdf.py" "$FIXTURES/small10.pdf"
"$PY" "$HERE/mk_text_bomb.py" 100 3 "$FIXTURES/text_bomb.pdf"
"$PY" "$HERE/mk_pages_pdf.py" "$FIXTURES/pages_at_limit.pdf" 300 >/dev/null
"$PY" "$HERE/mk_pages_pdf.py" "$FIXTURES/pages_over_limit.pdf" 301 >/dev/null
if [[ -z "${REAL_PDF:-}" ]]; then
    REAL_PDF="$(find "$WORKDIR/uploads" -maxdepth 1 -name '*.pdf' -size +5M | head -1)"
fi
if [[ -n "${REAL_PDF:-}" && -f "$REAL_PDF" ]]; then
    echo "real pdf: $REAL_PDF"
else
    echo "real pdf: (none found, skipping the 'pathological paper' case)"
fi

echo
echo "== target: $BASE_URL  server_pid=${SERVER_PID:-unknown}  uploads=${UPLOADS_DIR:-unknown} =="
echo

printf '%-26s %6s %8s %10s %12s %12s %9s\n' \
    CASE CODE WALL_S SRV_PEAK_MB WORKER_PEAK_MB LEFT_UPLOADS NOTE

note_file() { echo "$RESULTS/$1.note"; }

# post <case> <url-path> <curl-args...>
post() {
    local case="$1" path="$2"
    shift 2
    local body="$RESULTS/$case.body" watchrc=0
    local before after
    before=$(find "$UPLOADS_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)

    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        "$PY" "$HERE/rss_watch.py" "$SERVER_PID" "$RESULTS/$case.rss" 0.1 &
        local watch_pid=$!
    else
        local watch_pid=""
    fi

    local out
    out=$(curl -sS -o "$body" -w '%{http_code} %{time_total}' \
        --max-time 300 "$@" "$BASE_URL$path") || out="000 0"
    local code="${out%% *}" wall="${out##* }"

    if [[ -n "$watch_pid" ]]; then
        kill -TERM "$watch_pid" 2>/dev/null
        wait "$watch_pid" 2>/dev/null
    fi
    sleep 0.5  # let the server unlink its upload

    after=$(find "$UPLOADS_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)

    local srv_mb="-" wrk_mb="-"
    if [[ -f "$RESULTS/$case.rss" ]]; then
        srv_mb=$(( $(jq -r '.peak_total_kb' "$RESULTS/$case.rss") / 1024 ))
        wrk_mb=$(( $(jq -r '.peak_child_kb' "$RESULTS/$case.rss") / 1024 ))
    fi

    printf '%-26s %6s %8s %10s %12s %12s %9s\n' \
        "$case" "$code" "$wall" "$srv_mb" "$wrk_mb" "$((after - before))" "$(cat "$(note_file "$case")" 2>/dev/null)"

    [[ "$code" == 2* ]] && echo "  body: $(head -c 160 "$body")"
    [[ "$code" != 2* ]] && echo "  body: $(head -c 200 "$body")"
}

small_post() { # name file filename expected_note
    post "$1" /parse_pdf \
        -F "title=regress" -F "link=regress" \
        -F "chunk_size=1000" -F "chunk_overlap=0" \
        -F "pdf=@$2;filename=$3"
}

# ---------------------------------------------------------------- case 1/2/3
echo "normal 10-page PDF" > "$(note_file normal_small)"
small_post normal_small "$FIXTURES/small10.pdf" regress_small.pdf

# 与 zotero_script.js 完全一致的默认参数（chunkSize=64000 / chunkOverlap=1000）：
# 之前服务端把 chunk_size 上限设成 20000，导致线上真实请求全部 400。
echo "client defaults chunkSize=64000/chunkOverlap=1000" > "$(note_file client_defaults)"
post client_defaults /parse_pdf \
    -F "title=regress" -F "link=regress" \
    -F "chunk_size=64000" -F "chunk_overlap=1000" \
    -F "pdf=@$FIXTURES/small10.pdf;filename=client_defaults.pdf"

echo "100 pages x 3MB extractable text in a 1.6MB file" > "$(note_file text_bomb)"
small_post text_bomb "$FIXTURES/text_bomb.pdf" regress_bomb.pdf

if [[ -n "${REAL_PDF:-}" && -f "$REAL_PDF" ]]; then
    echo "real paper with pathological pages" > "$(note_file real_paper)"
    small_post real_paper "$REAL_PDF" regress_real.pdf
fi

echo "300 pages (at MAX_PDF_PAGES)" > "$(note_file pages_at_limit)"
post pages_at_limit /parse_pdf \
    -F "title=regress" -F "link=regress" \
    -F "chunk_size=64000" -F "chunk_overlap=1000" \
    -F "pdf=@$FIXTURES/pages_at_limit.pdf;filename=pages300.pdf"

echo "301 pages (one over MAX_PDF_PAGES)" > "$(note_file pages_over_limit)"
post pages_over_limit /parse_pdf \
    -F "title=regress" -F "link=regress" \
    -F "chunk_size=64000" -F "chunk_overlap=1000" \
    -F "pdf=@$FIXTURES/pages_over_limit.pdf;filename=pages301.pdf"

# ------------------------------------------------- case 4: same-name concurrency
echo "5 concurrent, same filename" > "$(note_file same_name_x5)"
for i in 1 2 3 4 5; do
    (curl -sS -o "$RESULTS/same_name_$i.body" -w '%{http_code}\n' \
        -F "title=t" -F "link=l" -F "chunk_size=1000" -F "chunk_overlap=0" \
        -F "pdf=@$FIXTURES/small10.pdf;filename=collide.pdf" \
        "$BASE_URL/parse_pdf" >> "$RESULTS/same_name.codes") &
done
wait
sleep 1
codes=$(sort "$RESULTS/same_name.codes" | uniq -c | tr '\n' ' ')
printf '%-26s %6s %8s %10s %12s %12s %9s\n' \
    "same_name_x5" "-" "-" "-" "-" "-" "$(echo $codes | tr -d '\n')"
echo "  codes: $codes (a 'No such file' 422 means the upload was deleted by a peer)"
rm -f "$RESULTS/same_name.codes"

# ------------------------------------------------- case 5: rate-limit spoofing
# nginx appends the real client address, so uvicorn must pick the LAST entry.
echo "X-Forwarded-For spoofing the limiter (nginx-style)" > "$(note_file xff_spoof)"
spoof_codes=""
seen_ips=""
for i in 1 2 3 4 5; do
    resp=$(curl -sS -H "X-Forwarded-For: 203.0.113.$i, 198.51.100.7" \
        -w ' %{http_code}' "$BASE_URL/test_ip")
    spoof_codes+="$(echo "$resp" | awk '{print $NF}') "
    seen_ips+="$(echo "$resp" | jq -r .client_ip 2>/dev/null) "
done
printf '%-26s %6s %8s %10s %12s %12s %9s\n' \
    "xff_spoof_x5" "-" "-" "-" "-" "-" \
    "codes: $spoof_codes"
echo "  resolved client_ip per request: $seen_ips (all must be 198.51.100.7)"
[[ "$seen_ips" == *203.0.113* ]] && echo "  FAIL: spoofed address was trusted"

# ------------------------------------------------------- case 6: leftover state
echo
echo "== state after run =="
if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "server alive: yes (pid $SERVER_PID)"
    echo "server RSS:  $(( $(awk '/VmRSS/{print $2}' /proc/$SERVER_PID/status 2>/dev/null || echo 0) / 1024 )) MB"
else
    echo "server alive: NO  <-- FAIL"
fi
echo "orphan pdf_worker processes: $(pgrep -fc '^[^ ]*python[0-9.]* [^ ]*pdf_worker\.py$' 2>/dev/null || true)"
if [[ -n "${UPLOADS_DIR:-}" && -d "$UPLOADS_DIR" ]]; then
    echo "leftover files in uploads/: $(find "$UPLOADS_DIR" -maxdepth 1 -type f | wc -l)"
    find "$UPLOADS_DIR" -maxdepth 1 -type f -printf '  %f  %s bytes\n' 2>/dev/null | head -5
fi
echo
echo "results written to $RESULTS"
