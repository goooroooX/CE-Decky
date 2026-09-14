#!/usr/bin/env python3
"""Capture the live Steam Game Mode screen, including the Decky overlay.

Gamescope composites Steam, Decky and the running game into one output, so its
own capture is the only one that shows what the user actually sees. There are
two of them and they are not equivalent: `gamescopectl screenshot` writes the
base plane only, which is the running game without any overlay, while the
`GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT` root property makes gamescope write the
whole composited frame including Steam and Decky. Only the second one can answer
a question about the plugin's UI, so it is the default here.

The raw capture is a full-resolution PNG; an agent reading one of those spends
thousands of tokens per frame, so every result is downscaled and encoded as
JPEG, and the region a controller workflow is usually inspected in can be
cropped before scaling.

`--tile NAME` produces the other kind of result: a published screenshot rather
than evidence to read once. It stays a lossless PNG at the composited frame's
own scale, is cut to one plugin window instead of a fraction of the screen, and
lands in `docs/assets/screenshots/`. Steam lays Game Mode out in a fixed 1280 px
logical grid, so the window's width is geometry rather than a guess: the quick
access panel is the right-hand 256 logical px plus its own border, and a modal
is 460 logical px centred. Only the height is measured, by trimming to the
topmost and bottommost lit row inside that column range, which is what makes one
tile as tall as the screen it shows and no taller. Every tile then gets an equal
margin of the surface colour around the window, so a tile published on its own
still has air around it and the README strip only has to close that margin off.

`--remote USER@HOST` captures the other kind of target: the device across the
network rather than the one this is running on. Only the capture belongs to the
device, so only the capture goes there. The rest, cropping, scaling and cutting
a tile, is image work with no target in it and stays here, which is why this
needs no mirrored checkout on the far end and no `rsync` before a screenshot:
one SSH call runs the same capture the local route runs, resolving that device's
own display from that device's own `gamescope-environment`, and returns the PNG
on its stdout. The address is an argument and is never written into this tree.

This never mutates Steam, Decky, Cheat Engine or game state, and deliberately
offers no way to put a game back in focus. Installing a package restarts Steam
webhelper and drops a running game behind the library, but every way of raising
it again damages Steam Input: `steam://rungameid` on a running game is refused
with a modal and moves the input context off the game, `steam://forceinputappid`
then pins that context hard enough that the Steam UI itself stops answering the
controller, and releasing it with appid `0` does not recover. Only restarting
Steam does, which ends the game anyway. Do not install while someone is playing.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__:
    from . import host_platform
else:
    import host_platform

DEFAULT_OUTPUT_DIR = Path.home() / "Pictures/Screenshots"
RUNTIME_ROOT = Path("/run/user")
# Decky's Quick Access panel occupies the right edge of the composited output.
QAM_WIDTH_FRACTION = 0.30
CENTER_WIDTH_FRACTION = 0.62
CENTER_HEIGHT_FRACTION = 0.86
CAPTURE_TIMEOUT_SECONDS = 30.0
# Gamescope writes every debug-requested composite to this exact fixed path.
DEBUG_CAPTURE_PATH = Path("/tmp/gamescope.png")
DEBUG_REQUEST_PROPERTY = "GAMESCOPECTRL_DEBUG_REQUEST_SCREENSHOT"
ENCODE_TIMEOUT_SECONDS = 60.0
# Reaching a handheld over Wi-Fi is slower than reaching a desktop, and the
# failure worth having is a refusal rather than a command that never returns.
REMOTE_CONNECT_TIMEOUT_SECONDS = 15
# The first bytes of every PNG.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

TILE_DIR = Path(__file__).resolve().parent.parent / "docs" / "assets" / "screenshots"
# Steam Game Mode lays out in a fixed logical grid and scales the whole thing to
# the output, so every window position below is stated in those logical pixels
# and multiplied by the scale the captured frame turns out to have.
STEAM_LOGICAL_WIDTH = 1280
# The quick access panel and its one-pixel left border, right-anchored.
QAM_LOGICAL_WIDTH = 256
QAM_LOGICAL_BORDER = 1
# A Steam modal sheet, centred.
MODAL_LOGICAL_WIDTH = 460
# The button-hint bar Steam draws along the bottom. It is not part of the plugin
# and it crosses the modal column range, so it is kept out of the height search
# rather than trimmed to.
FOOTER_LOGICAL_HEIGHT = 35
# The air around the window inside a published tile.
TILE_LOGICAL_MARGIN = 16
# Steam's Game Mode surface colour, which is what both the quick access panel
# and a modal sheet are painted in, so the margin reads as part of the window
# and the trim can tell the window apart from what it is drawn over.
TILE_SURFACE_COLOR = "0x0e141b"
TILE_SURFACE_SIMILARITY = 0.02
# A modal row counts as part of the sheet when this much of it is bare surface,
# which the sheet's own top and bottom edge always are and a backdrop is not.
TILE_SHEET_ROW_FRACTION = 0.8
TILE_SHAPES = ("qam", "modal")


def resolve_gamescope_environment() -> dict[str, str]:
    """Read the active session's DISPLAY from the file gamescope publishes.

    `gamescopectl` talks to the running compositor, so it needs the session's
    own DISPLAY and runtime directory. Never hard-code either.
    """
    runtime_dir = RUNTIME_ROOT / str(os.getuid())
    environment_file = runtime_dir / "gamescope-environment"
    if not runtime_dir.is_dir() or not environment_file.is_file():
        raise SystemExit("no active gamescope session: run this in the Steam Game Mode session's user context")
    displays = [
        line.removeprefix("DISPLAY=")
        for line in environment_file.read_text(encoding="utf-8", errors="strict").splitlines()
        if line.startswith("DISPLAY=")
    ]
    if len(displays) != 1 or not displays[0].startswith(":") or not displays[0][1:].isdigit():
        raise SystemExit("the active gamescope display is missing or ambiguous")
    environment = dict(os.environ)
    environment["DISPLAY"] = displays[0]
    environment["XDG_RUNTIME_DIR"] = str(runtime_dir)
    return environment


def capture_composited_png(destination: Path, environment: dict[str, str]) -> None:
    """Ask gamescope for the whole composited frame, overlays included.

    Gamescope answers the property change by writing one fixed path, so the
    result is only accepted once that file is newer than the request itself.
    """
    xprop = shutil.which("xprop", path=environment.get("PATH", os.defpath))
    if xprop is None:
        raise SystemExit("xprop is not installed on this system")
    before = DEBUG_CAPTURE_PATH.stat().st_mtime_ns if DEBUG_CAPTURE_PATH.is_file() else 0
    requested = time.time_ns()
    subprocess.run(
        [xprop, "-root", "-f", DEBUG_REQUEST_PROPERTY, "8s",
         "-set", DEBUG_REQUEST_PROPERTY, str(requested)],
        env=environment, capture_output=True, text=True, check=True, timeout=CAPTURE_TIMEOUT_SECONDS,
    )
    deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if DEBUG_CAPTURE_PATH.is_file():
            info = DEBUG_CAPTURE_PATH.stat()
            if info.st_mtime_ns > before and info.st_size > 0:
                shutil.copyfile(DEBUG_CAPTURE_PATH, destination)
                return
        time.sleep(0.2)
    raise SystemExit("gamescope did not write a composited screenshot")


#: The capture, as a program the target runs on its own Python.
#:
#: It is the same sequence as the two local capture functions, and it lives here
#: rather than in a mirrored checkout because a screenshot should not need one:
#: the far end may hold no copy of this repository at all. Every value it turns
#: on is formatted in from the constants above, so the compositor property, the
#: fixed path gamescope writes and the timeout cannot drift between the two
#: routes. It writes the PNG to its own stdout and everything else to stderr, so
#: one SSH call returns one frame.
REMOTE_CAPTURE_PROGRAM = '''
import os, shutil, subprocess, sys, time
from pathlib import Path

base_plane = {base_plane}
runtime = Path("{runtime_root}") / str(os.getuid())
environment_file = runtime / "gamescope-environment"
if not runtime.is_dir() or not environment_file.is_file():
    sys.exit("no active gamescope session on the target: it is not in Game Mode")
displays = [
    line[len("DISPLAY="):]
    for line in environment_file.read_text(encoding="utf-8", errors="strict").splitlines()
    if line.startswith("DISPLAY=")
]
if len(displays) != 1 or not displays[0].startswith(":") or not displays[0][1:].isdigit():
    sys.exit("the target active gamescope display is missing or ambiguous")
environment = dict(os.environ)
environment["DISPLAY"] = displays[0]
environment["XDG_RUNTIME_DIR"] = str(runtime)
timeout = {timeout}
capture = Path("{capture_path}")

if base_plane:
    gamescopectl = shutil.which("gamescopectl", path=environment.get("PATH", os.defpath))
    if gamescopectl is None:
        sys.exit("gamescopectl is not installed on the target")
    written = Path("/tmp/ce-decky-remote-base-plane-%d.png" % os.getpid())
    result = subprocess.run([gamescopectl, "screenshot", str(written)],
                            env=environment, capture_output=True, text=True, timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if written.is_file() and written.stat().st_size > 0:
            break
        time.sleep(0.2)
    else:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        sys.exit("gamescope did not write a screenshot on the target: " + detail)
else:
    xprop = shutil.which("xprop", path=environment.get("PATH", os.defpath))
    if xprop is None:
        sys.exit("xprop is not installed on the target")
    before = capture.stat().st_mtime_ns if capture.is_file() else 0
    subprocess.run([xprop, "-root", "-f", "{property}", "8s",
                    "-set", "{property}", str(time.time_ns())],
                   env=environment, capture_output=True, text=True, check=True, timeout=timeout)
    deadline = time.monotonic() + timeout
    written = None
    while time.monotonic() < deadline:
        if capture.is_file():
            info = capture.stat()
            if info.st_mtime_ns > before and info.st_size > 0:
                written = capture
                break
        time.sleep(0.2)
    if written is None:
        sys.exit("gamescope did not write a composited screenshot on the target")

payload = written.read_bytes()
if written != capture:
    written.unlink()
product = Path("/sys/class/dmi/id/product_name")
device = product.read_text(encoding="utf-8", errors="replace").strip() if product.is_file() else ""
sys.stdout.buffer.write(payload)
sys.stdout.buffer.flush()
sys.stderr.write("bytes=%d\\n" % len(payload))
sys.stderr.write("device=%s\\n" % device)
'''


#: Valve's own DMI product names, as a name a directory of screenshots can be
#: read by. `host_platform` holds the same mapping for the machine a helper is
#: running on; this is the other case, where the device is at the far end of a
#: connection and answered with its own.
DEVICE_PREFIXES = {
    "fremont": "steam-machine",
    "jupiter": "steam-deck-lcd",
    "galileo": "steam-deck-oled",
}


def default_prefix(remote: str | None, device: str | None, host: "host_platform.Host | None" = None) -> str:
    """What a capture is called when nobody named it.

    The machine is the useful half of a screenshot's name: a directory holding
    frames from a handheld and from a television is unreadable if every file
    claims to be from "ce-decky". The address is not that name. It is a route,
    it changes with the network, and it is the one detail this repository keeps
    out of its own tree, so a device that did not identify itself is "remote"
    rather than an IP address written into a filename.
    """
    if device:
        known = DEVICE_PREFIXES.get(device.strip().lower())
        if known:
            return f"ce-decky-{known}"
        # An unknown Valve or non-Valve device still names itself, reduced to
        # what a filename may hold rather than dropped for not being on a list.
        cleaned = "".join(character if character.isalnum() else "-" for character in device.strip().lower())
        cleaned = "-".join(part for part in cleaned.split("-") if part)[:32]
        if cleaned:
            return f"ce-decky-{cleaned}"
    if remote:
        return "ce-decky-remote"
    # A local capture names its machine the same way, out of the same DMI field,
    # so one directory of frames from both devices reads as one set.
    local = (host or host_platform.describe()).device
    return f"ce-decky-{local.replace('_', '-')}" if local else "ce-decky"


def capture_remote_png(destination: Path, *, remote: str, base_plane: bool) -> str | None:
    """Run the capture on the device across the network and keep its frame here.

    The PNG comes back on the SSH channel's own stdout, so nothing is left on the
    target and no directory there has to be writable. `BatchMode` is what turns a
    missing key into a refusal rather than a password prompt nothing is watching.

    Returns the device the frame came from, as Valve's own DMI product name, so
    the file can be named after the machine rather than after the address it was
    reached at. An address is a route and changes with the network; it is also
    the one part of this that is deliberately not written down anywhere here.
    """
    ssh = shutil.which("ssh")
    if ssh is None:
        raise SystemExit("ssh is not installed on this system")
    program = REMOTE_CAPTURE_PROGRAM.format(
        base_plane=bool(base_plane),
        runtime_root=RUNTIME_ROOT,
        timeout=CAPTURE_TIMEOUT_SECONDS,
        capture_path=DEBUG_CAPTURE_PATH,
        property=DEBUG_REQUEST_PROPERTY,
    )
    result = subprocess.run(
        [ssh, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={REMOTE_CONNECT_TIMEOUT_SECONDS}",
         remote, "python3", "-"],
        input=program.encode("utf-8"), capture_output=True,
        timeout=CAPTURE_TIMEOUT_SECONDS + REMOTE_CONNECT_TIMEOUT_SECONDS + ENCODE_TIMEOUT_SECONDS,
    )
    detail = result.stderr.decode("utf-8", errors="replace").strip()[-400:]
    if result.returncode != 0:
        raise SystemExit(f"remote capture on {remote} failed: {detail or 'no output'}")
    # A remote shell can put a login banner or a warning on the channel, so the
    # frame is identified rather than assumed to be whatever came back.
    if not result.stdout.startswith(PNG_MAGIC):
        raise SystemExit(f"remote capture on {remote} returned no PNG: {detail or 'no output'}")
    destination.write_bytes(result.stdout)
    for line in detail.splitlines():
        if line.startswith("device="):
            return line.removeprefix("device=").strip() or None
    return None


def capture_base_plane_png(destination: Path, environment: dict[str, str]) -> None:
    """Capture the running game's own plane, without Steam or Decky over it."""
    gamescopectl = shutil.which("gamescopectl", path=environment.get("PATH", os.defpath))
    if gamescopectl is None:
        raise SystemExit("gamescopectl is not installed on this system")
    result = subprocess.run(
        [gamescopectl, "screenshot", str(destination)],
        env=environment, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT_SECONDS,
    )
    # gamescopectl reports success asynchronously, so the exit code alone does
    # not prove the compositor wrote the file.
    deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if destination.is_file() and destination.stat().st_size > 0:
            return
        time.sleep(0.2)
    detail = (result.stderr or result.stdout or "").strip()[:200]
    raise SystemExit(f"gamescope did not write a screenshot{f': {detail}' if detail else ''}")


def encode(source: Path, destination: Path, *, region: str, width: int, quality: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is not installed on this system")
    if region == "qam":
        crop = f"crop=iw*{QAM_WIDTH_FRACTION}:ih:iw*{1 - QAM_WIDTH_FRACTION}:0,"
    elif region == "center":
        crop = (
            f"crop=iw*{CENTER_WIDTH_FRACTION}:ih*{CENTER_HEIGHT_FRACTION}:"
            f"iw*{(1 - CENTER_WIDTH_FRACTION) / 2}:ih*{(1 - CENTER_HEIGHT_FRACTION) / 2},"
        )
    else:
        crop = ""
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
         "-vf", f"{crop}scale={width}:-2:flags=lanczos", "-q:v", str(quality), str(destination)],
        check=True, capture_output=True, text=True, timeout=ENCODE_TIMEOUT_SECONDS,
    )


def frame_size(path: Path) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise SystemExit("ffprobe is not installed on this system")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True, timeout=ENCODE_TIMEOUT_SECONDS,
    ).stdout.strip()
    width, height = out.split(",")[:2]
    return int(width), int(height)


def surface_mask(path: Path, width: int, height: int) -> bytes:
    """One byte per pixel: zero where the frame is bare Steam surface colour.

    The window a tile shows is painted in that one colour and whatever is behind
    it is not, so this is what separates the two. ffmpeg does the comparison
    because a per-pixel loop over a 4K frame in Python does not finish quickly.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is not installed on this system")
    mask = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path),
         "-vf", f"format=rgba,colorkey={TILE_SURFACE_COLOR}:{TILE_SURFACE_SIMILARITY}:0,alphaextract",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True, capture_output=True, timeout=ENCODE_TIMEOUT_SECONDS,
    ).stdout
    if len(mask) != width * height:
        raise SystemExit(f"decoded {len(mask)} mask bytes for a {width}x{height} frame")
    return mask


def window_columns(shape: str, width: int, scale: float) -> tuple[int, int, int]:
    """Where the named window is: its left edge, its right edge, and where the
    part of it worth measuring starts.

    The quick access panel is cut with the border line on its left, and that line
    is drawn on every row of the panel, so the height search has to start to the
    right of it or every row looks occupied.
    """
    if shape == "qam":
        panel = round(QAM_LOGICAL_WIDTH * scale)
        border = round(QAM_LOGICAL_BORDER * scale)
        left = max(0, width - panel - border)
        return left, width, left + border
    sheet = round(MODAL_LOGICAL_WIDTH * scale)
    left = (width - sheet) // 2
    return left, left + sheet, left


def panel_rows(mask: bytes, width: int, probe: int, right: int, limit: int) -> tuple[int, int]:
    """First and last row of the panel that has anything but bare surface on it.

    The panel is as tall as the screen whether it is full or nearly empty, so its
    own surface is the background here and only what is drawn on it counts.
    """
    top = bottom = None
    for y in range(limit):
        if max(mask[y * width + probe:y * width + right]) > 0:
            if top is None:
                top = y
            bottom = y
    if top is None:
        raise SystemExit("the quick access panel column range is empty: is the panel open?")
    return top, bottom


def sheet_rows(mask: bytes, width: int, probe: int, right: int, limit: int) -> tuple[int, int]:
    """First and last row of a modal sheet, found by the sheet's own surface.

    A sheet is drawn over a dimmed backdrop, and the backdrop is whatever was
    behind it, so it cannot be assumed dark. What it is not is a wide unbroken
    run of the surface colour, which the sheet's top and bottom edges both are.
    """
    span = right - probe
    needed = int(span * TILE_SHEET_ROW_FRACTION)
    top = bottom = None
    for y in range(limit):
        row = mask[y * width + probe:y * width + right]
        if row.count(0) >= needed:
            if top is None:
                top = y
            bottom = y
    if top is None:
        raise SystemExit("no modal sheet in the centre of the frame: is that screen actually open?")
    return top, bottom


def cut_tile(source: Path, destination: Path, *, shape: str, trim_bottom: int = 0) -> tuple[int, int, int, int]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is not installed on this system")
    width, height = frame_size(source)
    scale = width / STEAM_LOGICAL_WIDTH
    left, right, probe = window_columns(shape, width, scale)
    mask = surface_mask(source, width, height)
    limit = max(1, height - round(FOOTER_LOGICAL_HEIGHT * scale))
    rows = panel_rows if shape == "qam" else sheet_rows
    top, bottom = rows(mask, width, probe, right, limit)
    bottom -= round(trim_bottom * scale)
    if bottom <= top:
        raise SystemExit("--trim-bottom removed the whole window")
    margin = round(TILE_LOGICAL_MARGIN * scale)
    crop_w, crop_h = right - left, bottom - top + 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(source),
         "-vf", f"crop={crop_w}:{crop_h}:{left}:{top},"
                f"pad={crop_w + margin * 2}:{crop_h + margin * 2}:{margin}:{margin}"
                f":color={TILE_SURFACE_COLOR}",
         "-frames:v", "1", str(destination)],
        check=True, capture_output=True, text=True, timeout=ENCODE_TIMEOUT_SECONDS,
    )
    return left, top, crop_w, crop_h


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", choices=("full", "qam", "center"), default="full",
                        help="full screen, the right-edge Decky Quick Access panel, or a centered modal")
    parser.add_argument("--width", type=int, default=0,
                        help="output width in pixels; the default suits the selected region")
    parser.add_argument("--quality", type=int, default=4, help="JPEG quality, 2 (best) to 31 (worst)")
    parser.add_argument("--prefix", default=None, metavar="LABEL",
                        help="what this capture is of, added to the name. The machine the frame came from "
                             "always leads the name and the timestamp and region always close it, so this "
                             "is the one part a caller chooses: a symptom, a workflow or a screen")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--keep-png", action="store_true", help="also keep the raw full-resolution capture")
    parser.add_argument("--base-plane", action="store_true",
                        help="capture the game's own plane without the Steam and Decky overlays")
    parser.add_argument("--tile", metavar="NAME",
                        help="cut a published lossless tile to docs/assets/screenshots/NAME.png "
                             "instead of writing an evidence JPEG")
    parser.add_argument("--shape", choices=TILE_SHAPES, default="qam",
                        help="which plugin window --tile cuts: the quick access panel or a modal sheet")
    parser.add_argument("--source", type=Path,
                        help="recut --tile from an existing full-resolution PNG instead of capturing one")
    parser.add_argument("--remote", metavar="USER@HOST",
                        help="capture on a SteamOS device across the network over SSH instead of on this "
                             "machine; the cropping and scaling still happen here")
    parser.add_argument("--trim-bottom", type=int, default=0, metavar="LOGICAL",
                        help="cut this many Steam logical pixels off the bottom of the window, for a "
                             "screen that continues into content a published tile must not carry")
    args = parser.parse_args(argv)
    # Only the capture belongs to the device. With --remote the device is at the
    # other end of the connection and this machine does image work, which any
    # host can do, so requiring Valve hardware here would refuse the arrangement
    # this option exists for: a desktop or a Steam Machine driving a Deck.
    if args.remote is None:
        try:
            host_platform.require("The Gamescope screenshot helper", needs_valve_hardware=True)
        except host_platform.UnsupportedHost as exc:
            return host_platform.refuse(exc)

    if args.source is not None and not args.tile:
        parser.error("--source only applies to --tile")
    if args.source is not None and args.remote:
        parser.error("--source recuts a frame that is already here, so there is nothing to capture remotely")
    if args.remote is not None and not args.remote.strip():
        parser.error("--remote needs an SSH destination")
    if args.tile:
        if args.base_plane:
            parser.error("--tile needs the composited frame, not the base plane")
        if any(character in args.tile for character in "/\\\0") or len(args.tile) > 64:
            parser.error("--tile must be a short name without path separators")
        destination = TILE_DIR / f"{args.tile}.png"
        if args.source is not None:
            if not args.source.is_file():
                raise SystemExit(f"no such frame: {args.source}")
            left, top, crop_w, crop_h = cut_tile(
                args.source, destination, shape=args.shape, trim_bottom=args.trim_bottom)
        else:
            with tempfile.TemporaryDirectory(prefix="ce-decky-screenshot-") as directory:
                raw = Path(directory) / "capture.png"
                if args.remote:
                    capture_remote_png(raw, remote=args.remote, base_plane=False)
                else:
                    capture_composited_png(raw, resolve_gamescope_environment())
                left, top, crop_w, crop_h = cut_tile(
                    raw, destination, shape=args.shape, trim_bottom=args.trim_bottom)
                if args.keep_png:
                    args.output_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(raw, args.output_dir / f"{args.tile}-{time.strftime('%Y%m%dT%H%M%S')}.png")
        width, height = frame_size(destination)
        print(
            f"tile={destination} bytes={destination.stat().st_size} shape={args.shape} "
            f"size={width}x{height} window={crop_w}x{crop_h}+{left}+{top} "
            f"captured-on={args.source or args.remote or 'this machine'}"
        )
        return 0

    if not 2 <= args.quality <= 31:
        parser.error("--quality must be between 2 and 31")
    width = args.width or {"full": 1280, "qam": 560, "center": 900}[args.region]
    if not 320 <= width <= 3840:
        parser.error("--width must be between 320 and 3840")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    prefix = (args.prefix or "").strip()
    if prefix and (any(character in prefix for character in "/\\\0") or len(prefix) > 64):
        parser.error("--prefix must be a short name without path separators")

    with tempfile.TemporaryDirectory(prefix="ce-decky-screenshot-") as directory:
        raw = Path(directory) / "capture.png"
        if args.remote:
            device = capture_remote_png(raw, remote=args.remote, base_plane=args.base_plane)
        elif args.base_plane:
            device = None
            capture_base_plane_png(raw, resolve_gamescope_environment())
        else:
            device = None
            capture_composited_png(raw, resolve_gamescope_environment())
        # The name is settled here rather than above, because for a remote
        # capture the machine that answered is not known until it has answered.
        name = "-".join(part for part in (
            default_prefix(args.remote, device), prefix, time.strftime("%Y%m%dT%H%M%S"), args.region,
        ) if part)
        output = args.output_dir / f"{name}.jpg"
        encode(raw, output, region=args.region, width=width, quality=args.quality)
        if args.keep_png:
            shutil.copy2(raw, args.output_dir / f"{name}.png")

    print(
        f"screenshot={output} bytes={output.stat().st_size} region={args.region} "
        f"width={width} planes={'base' if args.base_plane else 'composited'} "
        f"captured-on={args.remote or 'this machine'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
