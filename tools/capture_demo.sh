#!/usr/bin/env bash
# Capture the transcript the README animation is built from, by actually running the CLI.
#
# This costs tokens: the compile step really does synthesise. That is the point — the
# demo has to show what the tool does, not what someone hoped it would do.
#
# Three steps, not two. A real compile prints lines 150 columns wide, and which of the
# eight generated cases to show is a judgement call, so **the middle step is yours**:
#
#     tools/capture_demo.sh              # runs the CLI, writes docs/demo.raw.txt
#     $EDITOR docs/demo.txt              # pick the lines, trim them to <= 92 columns
#     python tools/make_demo_svg.py      # docs/demo.txt -> docs/demo.svg
#
# It writes docs/demo.raw.txt (gitignored) and never touches docs/demo.txt, so a capture
# cannot silently throw away the curated version that is committed.
set -euo pipefail

cd "$(dirname "$0")/.."
HOME_DIR="${AGENTJIT_DEMO_HOME:-/tmp/agentjit-demo}"
OUT="${1:-docs/demo.raw.txt}"
PY="${PYTHON:-python3}"
RAW="$(mktemp -d)"
trap 'rm -rf "$RAW"' EXIT

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
  AGENTJIT_HOME="$HOME_DIR" "$PY" -m agentjit.cli "$@" 2>&1 | tee -a "$RAW/log" >/dev/null
  printf '~~~\n' >> "$RAW/log"
}

run compile examples/rank.json --name rank
run call rank '{"records":[{"name":"zoe","score":7},{"name":"amy","score":9}]}'
run list

cp "$RAW/log" "$OUT"

echo "captured: $OUT"
echo
echo "Next: copy the lines you want into docs/demo.txt, trimmed to <= 92 columns"
echo "(make_demo_svg.py refuses anything wider and names the offenders), then run"
echo "  python tools/make_demo_svg.py"
