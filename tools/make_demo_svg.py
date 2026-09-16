"""Build the animated terminal demo in the README.

A self-contained animated SVG: no JavaScript (GitHub strips it), no external fonts, no
external requests. Animation is SMIL only, which GitHub does render.

**Everything it displays has to be real captured output.** The point of a demo is to show
what the tool does; a hand-written one shows what someone hoped it would do. So this
script takes a transcript file, and `tools/capture_demo.sh` is what produces that
transcript by actually running the CLI.

    tools/capture_demo.sh                       # runs the CLI, writes docs/demo.txt
    python tools/make_demo_svg.py               # docs/demo.txt -> docs/demo.svg

Transcript format: a line starting with `$ ` is a command (typed out character by
character); everything else is output (revealed a line at a time). A line of `~~~` is a
pause.
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- geometry -----------------------------------------------------------------
FONT_SIZE = 13.5
CHAR_W = FONT_SIZE * 0.601              # DejaVu Sans Mono's advance width ratio
LINE_H = 19.0
PAD_X, PAD_TOP = 22.0, 52.0
PAD_BOTTOM = 20.0
COLS = 92
RADIUS = 10.0

# --- timing (seconds) ---------------------------------------------------------
TYPE_PER_CHAR = 0.035
AFTER_COMMAND = 0.35                    # the pause between hitting enter and output
PER_OUTPUT_LINE = 0.10
PAUSE = 1.1                             # an explicit `~~~`
TAIL = 3.0                              # how long the final frame is held

# --- colours (a dark terminal; readable on both GitHub themes) -----------------
BG = "#11151c"
BG_BAR = "#1a1f29"
FG = "#c8d3e0"
DIM = "#6b7688"
PROMPT = "#7aa2f7"
CMD = "#e6edf5"
OK = "#5ac489"
WARN = "#e0af68"
ACCENT = "#a78bfa"

# Output lines are coloured by what they start with. Substance only — no line gets a
# colour it has not earned.
RULES: list[tuple[str, str]] = [
    ("PASS", OK), ("ok ", OK), ("result: ok", OK),
    ("FAIL", "#e06c75"), ("error", "#e06c75"),
    ("!", WARN),
    ("miss:", ACCENT), ("cache:", ACCENT), ("name ", ACCENT),
    ("level:", DIM), ("requirement", DIM), ("seeds", DIM), ("NAME", DIM),
]


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def colour_for(line: str) -> str:
    stripped = line.lstrip()
    for prefix, col in RULES:
        if stripped.startswith(prefix):
            return col
    return FG


def parse(text: str) -> list[tuple[str, str]]:
    """-> [(kind, text)] where kind is cmd | out | pause."""
    steps = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "~~~":
            steps.append(("pause", ""))
        elif line.startswith("$ "):
            steps.append(("cmd", line[2:]))
        else:
            steps.append(("out", line))
    return steps


def schedule(steps: list[tuple[str, str]]) -> tuple[list[dict], float]:
    """Give every line a start time, and return the total duration."""
    t = 0.4
    out = []
    for kind, text in steps:
        if kind == "pause":
            t += PAUSE
            continue
        if kind == "cmd":
            dur = len(text) * TYPE_PER_CHAR
            out.append({"kind": "cmd", "text": text, "t": t, "dur": dur})
            t += dur + AFTER_COMMAND
        else:
            out.append({"kind": "out", "text": text, "t": t, "dur": 0.0})
            t += PER_OUTPUT_LINE
    return out, t + TAIL


def animate_opacity(t0: float, total: float) -> str:
    """Invisible, then visible from t0 until the loop restarts."""
    k = max(0.0, min(1.0, t0 / total))
    return (f'<animate attributeName="opacity" begin="0s" dur="{total:.2f}s" '
            f'repeatCount="indefinite" calcMode="discrete" '
            f'values="0;1" keyTimes="0;{k:.5f}"/>')


def render(lines: list[dict], total: float, title: str) -> str:
    n = len(lines)
    height = PAD_TOP + n * LINE_H + PAD_BOTTOM
    width = PAD_X * 2 + COLS * CHAR_W

    body: list[str] = []
    clips: list[str] = []

    for i, item in enumerate(lines):
        y = PAD_TOP + i * LINE_H
        text = item["text"]

        if item["kind"] == "out":
            col = colour_for(text)
            body.append(
                f'<text x="{PAD_X:.1f}" y="{y:.1f}" fill="{col}" opacity="0" '
                f'xml:space="preserve">{esc(text)}'
                f'{animate_opacity(item["t"], total)}</text>')
            continue

        # A command: the prompt appears, then the text types itself out under a clip
        # rect that widens, with a cursor riding the right-hand edge.
        w = len(text) * CHAR_W
        x0 = PAD_X + 2 * CHAR_W
        cid = f"t{i}"
        clips.append(
            f'<clipPath id="{cid}"><rect x="{x0:.1f}" y="{y - LINE_H:.1f}" '
            f'height="{LINE_H * 1.6:.1f}" width="0">'
            f'<animate attributeName="width" begin="{item["t"]:.2f}s" '
            f'dur="{item["dur"]:.2f}s" from="0" to="{w:.1f}" fill="freeze"/>'
            f'<animate attributeName="width" begin="{total:.2f}s" dur="0.01s" '
            f'to="0" fill="freeze" repeatCount="indefinite"/></rect></clipPath>')
        body.append(
            f'<text x="{PAD_X:.1f}" y="{y:.1f}" fill="{PROMPT}" opacity="0">$'
            f'{animate_opacity(item["t"], total)}</text>')
        body.append(
            f'<text x="{x0:.1f}" y="{y:.1f}" fill="{CMD}" clip-path="url(#{cid})" '
            f'xml:space="preserve">{esc(text)}</text>')
        body.append(
            f'<rect x="{x0:.1f}" y="{y - FONT_SIZE + 2.5:.1f}" width="{CHAR_W:.1f}" '
            f'height="{FONT_SIZE:.1f}" fill="{CMD}" opacity="0">'
            f'<animate attributeName="x" begin="{item["t"]:.2f}s" '
            f'dur="{item["dur"]:.2f}s" from="{x0:.1f}" to="{x0 + w:.1f}" fill="freeze"/>'
            f'<animate attributeName="opacity" begin="0s" dur="{total:.2f}s" '
            f'repeatCount="indefinite" calcMode="discrete" values="0;0.85;0" '
            f'keyTimes="0;{item["t"] / total:.5f};'
            f'{(item["t"] + item["dur"]) / total:.5f}"/></rect>')

    dots = "".join(
        f'<circle cx="{22 + k * 18}" cy="22" r="5.5" fill="{c}"/>'
        for k, c in enumerate(("#ff5f57", "#febc2e", "#28c840")))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" \
height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" \
font-family="ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace" \
font-size="{FONT_SIZE}" role="img" aria-label="{esc(title)}">
<title>{esc(title)}</title>
<defs>{''.join(clips)}</defs>
<rect width="{width:.0f}" height="{height:.0f}" rx="{RADIUS}" fill="{BG}"/>
<path d="M0 {RADIUS} A{RADIUS} {RADIUS} 0 0 1 {RADIUS} 0 H{width - RADIUS:.0f} \
A{RADIUS} {RADIUS} 0 0 1 {width:.0f} {RADIUS} V40 H0 Z" fill="{BG_BAR}"/>
{dots}
<text x="{width / 2:.0f}" y="26" fill="{DIM}" font-size="11.5" text-anchor="middle">\
{esc(title)}</text>
{chr(10).join(body)}
</svg>
"""


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    src = Path(argv[0]) if argv else root / "docs" / "demo.txt"
    dst = Path(argv[1]) if len(argv) > 1 else root / "docs" / "demo.svg"

    if not src.exists():
        print(f"no transcript at {src} — run tools/capture_demo.sh first", file=sys.stderr)
        return 1

    lines, total = schedule(parse(src.read_text()))
    over = [x["text"] for x in lines if len(x["text"]) > COLS]
    if over:
        print(f"{len(over)} line(s) are wider than {COLS} columns and will overflow:",
              file=sys.stderr)
        for line in over[:5]:
            print(f"  {len(line):3d}  {line[:70]}...", file=sys.stderr)
        return 1

    dst.write_text(render(lines, total, "agentjit — text in, code out, fetch it by name"))
    print(f"{dst}  {len(lines)} lines, {total:.1f}s loop, {dst.stat().st_size // 1024}KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
