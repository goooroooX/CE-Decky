"""The panel's inline mascot must stay derived from the tracked master.

The Decky bundle has no asset pipeline, so the artwork exists twice: the master
under `docs/assets/` that the README shows, and a small palettised data URI
compiled into `dist/index.js` that the panel draws. Replacing the master without
rerunning the generator leaves the panel on the previous artwork with nothing
saying so, which is the one failure mode of keeping two copies.
"""

from __future__ import annotations

from pathlib import Path
import base64
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
MASTER = ROOT / "docs" / "assets" / "hexpaw.png"
MODULE = ROOT / "src" / "assets" / "hexpaw.ts"


def test_master_artwork_and_generated_module_are_both_present():
    assert MASTER.is_file(), "the README's mascot master is missing"
    assert MODULE.is_file(), "the panel's inline mascot module is missing"


def test_inline_mascot_is_a_small_binary_alpha_png_the_panel_can_draw():
    """Decoded here rather than trusted, because it ships inside the bundle."""
    source = MODULE.read_text(encoding="utf-8")
    chunks = re.findall(r'\+ "([A-Za-z0-9+/=]+)"', source)
    assert chunks, "no base64 payload found in the generated module"
    payload = base64.b64decode("".join(chunks))

    assert payload.startswith(b"\x89PNG\r\n\x1a\n"), "inline mascot is not a PNG"
    # Inlining is only reasonable while it costs the bundle a few kilobytes.
    assert len(payload) < 64 * 1024, f"inline mascot has grown to {len(payload)} bytes"
    width, height = int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")
    # Twice the 112px the panel draws it at, at the artwork's own 2:1 ratio.
    assert (width, height) == (224, 112), f"inline mascot is {width}x{height}"
    assert 'HEXPAW_ASPECT_RATIO = 2' in source


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required to derive the mascot")
def test_inline_mascot_still_matches_the_master_it_was_derived_from():
    """Guards the drift that replacing the artwork by hand introduces."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_mascot_asset.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, (
        "src/assets/hexpaw.ts no longer matches docs/assets/hexpaw.png; "
        "rerun `python scripts/build_mascot_asset.py`\n"
        f"{result.stdout}{result.stderr}"
    )
