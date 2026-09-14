#!/usr/bin/env python3
"""Report what a table search returns for the games installed on this machine.

Search quality is only visible across a real library: one game proves nothing,
and the failures worth fixing are the ones a whole shelf of titles exposes at
once - a name the user abbreviated, a repack folder that carries the real title,
a provider that answers with a different game entirely.

This runs the production search for each selected game and prints one compact
block per game. It is read-only with respect to the plugin: the provider result
cache is copied to a scratch directory first, so a survey never disturbs the
cache the running backend owns, and the warm FearLess index is reused rather
than rebuilt.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

# The one implementation of this, rather than a copy per helper: it is a
# refusal about an authority, and two copies of a refusal drift apart quietly.
if __package__:
    from .steam_user import steam_user_home
else:
    from steam_user import steam_user_home

from ce_decky.catalog import CatalogService  # noqa: E402
from ce_decky.network import NetworkClient  # noqa: E402
from ce_decky.network_tls import build_verified_ssl_context  # noqa: E402
from ce_decky.shortcuts_vdf import parse_binary  # noqa: E402

# Only results CE Decky can download itself are what a controller user can use.
AUTOMATIC_DOWNLOAD_MODES = {"direct_https"}
BETWEEN_SEARCHES_SECONDS = 1.5


def read_library(userdata: Path) -> list[dict[str, str]]:
    """Every non-Steam shortcut Steam has recorded for this machine's users."""
    games: list[dict[str, str]] = []
    if not userdata.is_dir():
        return games
    for user in sorted(userdata.iterdir()):
        shortcuts = user / "config/shortcuts.vdf"
        if not shortcuts.is_file():
            continue
        for top in parse_binary(shortcuts.read_bytes()):
            if top.key != "shortcuts" or not isinstance(top.value, list):
                continue
            for entry in top.value:
                fields = {node.key: node.value for node in entry.value if hasattr(node, "key")}
                name, executable = fields.get("appname"), fields.get("exe")
                if isinstance(name, str) and name and isinstance(executable, str):
                    games.append({"name": name, "executable": executable})
    return games


async def survey(games: list[dict[str, str]], cache_root: Path, limit: int) -> int:
    context, _ = build_verified_ssl_context()
    network = NetworkClient(context)
    service = CatalogService(network, cache_root / "provider-results.json")
    empty = 0
    try:
        for game in games:
            try:
                outcome = await service.search(game["name"], game["executable"])
            except Exception as exc:  # noqa: BLE001 - a survey reports, never fails
                print(f"\n=== {game['name']}\n    ERROR {type(exc).__name__}: {str(exc)[:100]}")
                continue
            usable = [row for row in outcome["results"] if row.get("download_mode") in AUTOMATIC_DOWNLOAD_MODES]
            if not usable:
                empty += 1
            print(f"\n=== {game['name']}   ({len(usable)} usable of {len(outcome['results'])})")
            for row in usable[:limit]:
                version = row.get("version")
                print(
                    f"    {row['match_score']:.3f} {str(row['provider'])[:10]:11} "
                    f"{('v' + str(version))[:10] if version else '-':11} "
                    f"{str(row['filename'])[:34]:36} {str(row['table_title'])[:46]}"
                )
            for failure in outcome.get("failures", []):
                reason = str(failure.get("error") or "").strip() or "(no reason reported)"
                print(f"    ! {failure['provider']}: {reason[:70]}")
            await asyncio.sleep(BETWEEN_SEARCHES_SECONDS)
    finally:
        await service.close()
        await network.aclose()
    print(f"\nsurveyed {len(games)} game(s); {empty} returned nothing usable")
    return 0



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="exact library names to survey; default is every shortcut")
    parser.add_argument("--user-home", help="exact DECKY_USER_HOME; `target_plugin_install.py authority` reports it as user_home")
    parser.add_argument("--userdata", type=Path, help="exact Steam userdata; derived from --user-home when omitted")
    parser.add_argument("--cache", type=Path,
                        help="provider cache to copy, derived from --user-home when omitted; the original is never written")
    parser.add_argument("--limit", type=int, default=5, help="results printed per game")
    parser.add_argument("--list", action="store_true", help="print the library and exit without searching")
    args = parser.parse_args(argv)
    if args.userdata is None:
        args.userdata = steam_user_home(args.user_home) / ".local/share/Steam/userdata"

    games = read_library(args.userdata)
    if args.names:
        wanted = set(args.names)
        games = [game for game in games if game["name"] in wanted]
        missing = wanted - {game["name"] for game in games}
        if missing:
            parser.error(f"not installed: {', '.join(sorted(missing))}")
    if not games:
        parser.error("no non-Steam shortcuts found")
    if args.list:
        for game in games:
            print(f"{game['name']}\t{game['executable']}")
        return 0
    if not 1 <= args.limit <= 50:
        parser.error("--limit must be between 1 and 50")
    # Only a real search copies the provider cache, so listing the library does
    # not have to name a home for a path it will never open.
    if args.cache is None:
        args.cache = steam_user_home(args.user_home) / ".cheat-engine-decky/cache"

    with tempfile.TemporaryDirectory(prefix="ce-decky-search-survey-") as directory:
        cache_root = Path(directory) / "cache"
        if args.cache.is_dir():
            shutil.copytree(args.cache, cache_root)
        else:
            cache_root.mkdir(parents=True)
        return asyncio.run(survey(games, cache_root, args.limit))


if __name__ == "__main__":
    sys.exit(main())
