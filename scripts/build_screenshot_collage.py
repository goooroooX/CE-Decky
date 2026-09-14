#!/usr/bin/env python3
"""Compose the README screenshot strip from the tracked panel captures.

The tiles in `docs/assets/screenshots/` are the durable part: each one is a
single screen cut out of a composited Gamescope frame at full resolution, and
recutting them needs the device, a running game and the exact plugin state they
were taken in. Arranging them does not, so the arrangement lives here and the
strip can be rebuilt, reordered or restyled from what is already in the tree.

Tiles keep their native scale. A quick access panel is a tall narrow column and
a modal is a wide short sheet, and normalising them to one height would set the
same interface in two different type sizes on one image; they are laid out in a
row at 1:1 and centred against each other instead.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TILES = ROOT / "docs" / "assets" / "screenshots"
OUTPUT = ROOT / "docs" / "assets" / "readme-panels.png"

# Control, then finding a table, then authorizing one: the order the workflow
# actually happens in, and the order the README describes it in.
STRIP = ("panel", "search", "consent")
MARGIN = 60
GUTTER = 60
BORDER = 3
# Each tile already carries an equal margin of its own around the window, baked
# in when it was cut, because the tiles are published on their own as well as in
# this strip. So the layout adds only the edge that closes that margin off.
BACKGROUND = "0x05070a"
EDGE = "0x33404f"


def size(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


def main() -> int:
    tiles = [(name, TILES / f"{name}.png") for name in STRIP]
    missing = [str(path) for _, path in tiles if not path.exists()]
    if missing:
        print("missing tile(s): " + ", ".join(missing), file=sys.stderr)
        return 1

    boxes = [(name, path, *size(path)) for name, path in tiles]
    framed = [(width + BORDER * 2, height + BORDER * 2) for _, _, width, height in boxes]
    canvas_w = MARGIN * 2 + sum(w for w, _ in framed) + GUTTER * (len(framed) - 1)
    canvas_h = MARGIN * 2 + max(h for _, h in framed)

    inputs: list[str] = []
    for _, path, _, _ in boxes:
        inputs += ["-i", str(path)]

    steps = [
        f"[{index + 1}]pad=iw+{BORDER * 2}:ih+{BORDER * 2}:{BORDER}:{BORDER}:color={EDGE}[t{index}]"
        for index in range(len(boxes))
    ]
    stage = "0"
    x = MARGIN
    for index, (width, height) in enumerate(framed):
        y = (canvas_h - height) // 2
        target = "out" if index == len(framed) - 1 else f"s{index}"
        steps.append(f"[{stage}][t{index}]overlay={x}:{y}[{target}]")
        stage = target
        x += width + GUTTER

    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"color=c={BACKGROUND}:s={canvas_w}x{canvas_h}",
         *inputs,
         "-filter_complex", ";".join(steps), "-map", "[out]", "-frames:v", "1", str(OUTPUT)],
        check=True,
    )
    print(f"{OUTPUT} {canvas_w}x{canvas_h} from {', '.join(name for name, _, _, _ in boxes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
