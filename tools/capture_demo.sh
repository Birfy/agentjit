#!/usr/bin/env bash
# Capture the transcript the README animation is built from, by actually running the CLI.
#
# This costs tokens: the compile step really does synthesise. That is the point — the
# demo has to show what the tool does, not what someone hoped it would do.
#
#     tools/capture_demo.sh && python tools/make_demo_svg.py
set -euo pipefail

cd "$(dirname "$0")/.."
HOME_DIR="${AGENTJIT_DEMO_HOME:-/tmp/agentjit-demo}"
OUT="${1:-docs/demo.txt}"
RAW="$(mktemp -d)"

rm -rf "$HOME_DIR"
# Echo the command the way a person would have typed it: an argument carrying spaces or
# braces gets its quotes back, so the transcript is something you can paste.
show() {
  local out="" a
  for a in "$@"; do
    case "$a" in *[\ \{\}\"]*) out="$out '$a'" ;; *) out="$out $a" ;; esac
  done
  printf '$ agentjit%s\n' "$out" >> "$RAW/log"
}

run() {
  show "$@"
  AGENTJIT_HOME="$HOME_DIR" python3 -m agentjit.cli "$@" 2>&1 | tee -a "$RAW/log" >/dev/null
  printf '~~~\n' >> "$RAW/log"
}

run compile examples/rank.json --name rank
run call rank '{"records":[{"name":"zoe","score":7},{"name":"amy","score":9}]}'
run list

cp "$RAW/log" "$RAW/full.txt"
mv "$RAW/full.txt" "${OUT%.txt}.full.txt"
cp "$RAW/log" "$OUT"
rm -rf "$RAW"

echo "raw transcript: ${OUT%.txt}.full.txt"
echo "demo source:    $OUT  (trim it to <= 92 columns, then run tools/make_demo_svg.py)"
