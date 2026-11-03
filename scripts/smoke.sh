#!/usr/bin/env bash
# Manual smoke test for a running summarizer instance.
#
#   ./scripts/smoke.sh <pdf> [base_url] [chunk_size] [chunk_overlap]
#
# Defaults: http://127.0.0.1:13210, chunk_size=64000, chunk_overlap=1000
# (exactly what zotero_script.js sends, so a 200 here means the Zotero flow works).
set -euo pipefail

PDF="${1:?usage: smoke.sh <pdf> [base_url] [chunk_size] [chunk_overlap]}"
BASE_URL="${2:-http://127.0.0.1:13210}"
CHUNK_SIZE="${3:-64000}"
CHUNK_OVERLAP="${4:-1000}"
NAME="$(basename "$PDF")"

echo "== healthz =="
curl -fsS --max-time 10 "$BASE_URL/healthz" | sed 's/^/  /'
echo

echo "== parse_pdf ($NAME, chunk_size=$CHUNK_SIZE) =="
body=$(mktemp)
code=$(curl -sS -o "$body" -w '%{http_code} %{time_total}' --max-time 180 \
    -F "title=smoke" -F "link=smoke" \
    -F "chunk_size=$CHUNK_SIZE" -F "chunk_overlap=$CHUNK_OVERLAP" \
    -F "pdf=@$PDF;filename=$NAME" \
    "$BASE_URL/parse_pdf")
echo "  http=$code"
head -c 300 "$body"; echo
if [[ "${code%% *}" != "200" ]]; then
    echo "  -> parse failed"
    rm -f "$body"
    exit 1
fi

echo "== md_to_html =="
curl -sS --max-time 30 -F "markdown=# smoke

- ok" -F "model_name=smoke" "$BASE_URL/md_to_html" | head -c 200; echo

echo "== healthz (after) =="
curl -fsS --max-time 10 "$BASE_URL/healthz" | sed 's/^/  /'
echo
rm -f "$body"
