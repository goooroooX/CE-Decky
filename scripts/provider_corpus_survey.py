#!/usr/bin/env python3
"""Measure what real cheat tables actually contain, across a sampled corpus.

Reading Cheat Engine's format and its Lua API says what a table *can* do. This
says how often real tables do it, which is the only thing that can rank the
hazards worth engineering against. It exists because the one that bit this
project - a table carrying its own Lua script, which Cheat Engine asks about in
a modal form nobody can answer over a running game - was missed for months
purely because every table tested happened not to have one.

Method, and why each part is the way it is:

* Sampling is by forum page stratum, not by game name. The FearLess index this
  plugin already maintains is the forum's own topic listing, newest first, so
  page 0 is this week and page 120 is a few years ago. Sampling strata gives a
  reproducible spread of fresh and older titles without guessing a single game
  name, and without going near a search route.
* Every request is the production one: the same route, allowed hosts, referer,
  byte ceiling and rate-limit handling the plugin uses. A survey that fetched
  differently would measure a different provider.
* Nothing the running plugin owns is touched. The index is read from a copy,
  tables are imported into a scratch store, and the user's library, cache,
  blocklist and profiles are never opened.
* The corpus is additive and content-addressed. Re-running adds to it and skips
  what it already has, so it can be grown over several sessions rather than
  refetched, and the same table posted in two topics counts once.

It prints one compact frequency block. The per-table detail stays in the JSONL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

# The one implementation of this, rather than a copy per helper: it is a
# refusal about an authority, and two copies of a refusal drift apart quietly.
if __package__:
    from .steam_user import steam_user_home
else:
    from steam_user import steam_user_home

from ce_decky.archive_import import ArchiveImportError, inspect_archive  # noqa: E402
from ce_decky.catalog import (  # noqa: E402
    MAX_HTML_BYTES,
    PROVIDER_HOSTS,
    decode_html,
    parse_phpbb_attachments,
)
from ce_decky.ct_inspector import TableContentError, inspect_table  # noqa: E402
from ce_decky.network import NetworkClient, NetworkError, ProviderRateLimited  # noqa: E402
from ce_decky.providers import parse_retry_after  # noqa: E402
from ce_decky.network_tls import build_verified_ssl_context  # noqa: E402
from ce_decky.table_store import TableStore  # noqa: E402

DEFAULT_CORPUS = ROOT / "build" / "corpus"
FORUM_URL = "https://fearlessrevolution.com/viewforum.php?f=4"
# The topic route, which is where attachments live. The forum route above is
# only the referer and the listing the index was built from.
TOPIC_URL = "https://fearlessrevolution.com/viewtopic.php?f=4&t={topic_id}"
TOPICS_PER_PAGE = 50

# Fresh, recent-but-settled, and a few years old. Expressed as forum page
# numbers so the same stratum means the same thing on every run.
STRATA: tuple[tuple[str, int, int], ...] = (
    ("fresh", 0, 6),
    ("recent", 20, 60),
    ("older", 90, 200),
)

# Politeness. One provider, one machine, no hurry: this is a survey, and the
# provider owes it nothing.
BETWEEN_TOPICS_SECONDS = 2.0
BETWEEN_DOWNLOADS_SECONDS = 1.5
MAX_RATE_LIMIT_WAITS = 4
DEFAULT_RATE_LIMIT_WAIT = 20.0
MAX_RATE_LIMIT_WAIT = 120.0

MAX_ARTIFACT_BYTES = 24 * 1024 * 1024
SUPPORTED_SUFFIXES = {".ct", ".zip", ".7z", ".7zip"}

# What a table's own Lua can do to an owned session, from `celua.txt`. Grouped
# by what it costs this project rather than by what it is: a call that asks
# something, a call that puts a window up, a call that takes the session, and a
# call that blocks are four different problems with four different answers.
HAZARD_CALLS: dict[str, tuple[str, ...]] = {
    "asks": ("showMessage", "messageDialog", "inputQuery", "showSelectionList"),
    "window": ("createForm", "createFormFromFile", "createFormFromStream", "createTrainerForm"),
    "takes_session": ("openProcess", "closeCE", "loadTable", "createProcess", "detachIfPossible"),
    "freezes_game": ("pause",),
    "blocks": ("sleep", "createThread", "synchronize"),
    "changes_speed": ("speedhack_setSpeed",),
    "loads_code": ("dofile", "loadlibrary", "executeCode", "autoAssemble", "registerSymbol"),
    "hotkeys": ("createHotkey", "registerHotkey"),
}
_CALL_RE = {
    name: re.compile(r"\b(?:" + "|".join(re.escape(call) for call in calls) + r")\s*\(")
    for name, calls in HAZARD_CALLS.items()
}
_LUA_SECTION_RE = re.compile(r"<LuaScript>(.*?)</LuaScript>", re.S | re.I)
_AA_SECTION_RE = re.compile(r"<AssemblerScript[^>]*>(.*?)</AssemblerScript>", re.S | re.I)
# Auto Assembler can embed Lua, which is a second route to everything above and
# is invisible to a scan that only reads `<LuaScript>`.
_AA_LUA_RE = re.compile(r"\{\$lua\}", re.I)


@dataclass(frozen=True)
class Sampled:
    stratum: str
    topic_id: str
    title: str


def load_index(index_path: Path) -> dict[int, list[tuple[str, str]]]:
    """The plugin's own FearLess topic index, read from a copy."""
    raw = json.loads(index_path.read_text(encoding="utf-8"))
    pages: dict[int, list[tuple[str, str]]] = {}
    for start, topics in (raw.get("pages") or {}).items():
        try:
            offset = int(start)
        except (TypeError, ValueError):
            continue
        rows = [
            (str(topic[0]), str(topic[1]))
            for topic in topics
            if isinstance(topic, (list, tuple)) and len(topic) >= 2
        ]
        if rows:
            pages[offset] = rows
    return pages


def sample_topics(pages: dict[int, list[tuple[str, str]]], per_stratum: int, seed: int) -> list[Sampled]:
    """A reproducible spread across forum age, deduplicated by topic."""
    rng = random.Random(seed)
    chosen: list[Sampled] = []
    seen: set[str] = set()
    for name, first_page, last_page in STRATA:
        candidates: list[tuple[str, str]] = []
        for page in range(first_page, last_page):
            candidates.extend(pages.get(page * TOPICS_PER_PAGE, []))
        rng.shuffle(candidates)
        taken = 0
        for topic_id, title in candidates:
            if topic_id in seen:
                continue
            seen.add(topic_id)
            chosen.append(Sampled(name, topic_id, title))
            taken += 1
            if taken >= per_stratum:
                break
    return chosen


def classify(blob: Path, digest: str) -> dict[str, object]:
    """What this exact table contains, by the production inspector and a scan.

    The inspector is the authority for the facts the product acts on. The scan
    adds which Lua calls appear, which the product does not act on and which is
    the whole point of a corpus: it says which of the things a table *can* do
    are things tables *do* do.
    """
    facts: dict[str, object] = {"sha256": digest}
    try:
        inspection = inspect_table(blob, digest)
    except (TableContentError, ValueError) as exc:
        return {**facts, "usable": False, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"}
    text = blob.read_bytes().decode("utf-8", "replace")
    lua = "\n".join(_LUA_SECTION_RE.findall(text))
    assembler = "\n".join(_AA_SECTION_RE.findall(text))
    hazards = sorted(
        name for name, pattern in _CALL_RE.items()
        if pattern.search(lua) or pattern.search(assembler)
    )
    return {
        **facts,
        "usable": True,
        "entries": inspection.total_entries,
        "controls": len(inspection.controls),
        "table_version": inspection.table_version,
        "has_lua": inspection.has_lua,
        "has_forms": inspection.has_forms,
        "has_auto_assembler": inspection.has_auto_assembler,
        "embedded_files": inspection.embedded_files,
        "aa_embeds_lua": bool(_AA_LUA_RE.search(assembler)),
        "lua_bytes": len(lua),
        "hazards": hazards,
    }


class Survey:
    def __init__(self, network: NetworkClient, store: TableStore, scratch: Path, sevenzip: str | None):
        self.network = network
        self.store = store
        self.scratch = scratch
        self.sevenzip = sevenzip
        self.rate_limit_waits = 0

    async def _wait_out(self, exc: ProviderRateLimited) -> bool:
        """Honour one rate limit, or give up and say so.

        The provider's own `Retry-After` is the authority when it names one. A
        survey that ignored it would be exactly the automated traffic the site
        is defending itself against.
        """
        if self.rate_limit_waits >= MAX_RATE_LIMIT_WAITS:
            return False
        self.rate_limit_waits += 1
        # The provider carries its header verbatim and leaves the decision
        # here, which is the production contract; the production parser is what
        # turns it into seconds.
        try:
            wait = parse_retry_after(getattr(exc, "retry_after", None))
        except ValueError:
            wait = None
        if not isinstance(wait, (int, float)) or wait <= 0:
            wait = DEFAULT_RATE_LIMIT_WAIT * self.rate_limit_waits
        wait = min(float(wait), MAX_RATE_LIMIT_WAIT)
        print(f"    rate limited; waiting {wait:.0f}s ({self.rate_limit_waits}/{MAX_RATE_LIMIT_WAITS})", flush=True)
        await asyncio.sleep(wait)
        return True

    async def _get(self, url: str, referer: str):
        while True:
            try:
                response = await self.network.get(
                    url, allowed_hosts=PROVIDER_HOSTS["fearless"],
                    max_bytes=MAX_HTML_BYTES, referer=referer,
                )
            except ProviderRateLimited as exc:
                if await self._wait_out(exc):
                    continue
                raise
            if response.status == 429:
                if await self._wait_out(ProviderRateLimited(response.headers.get("retry-after"))):
                    continue
                raise NetworkError("rate limited past this survey's patience")
            return response

    async def topic_rows(self, sample: Sampled) -> list:
        page = TOPIC_URL.format(topic_id=sample.topic_id)
        response = await self._get(page, FORUM_URL)
        if response.status != 200:
            raise NetworkError(f"topic answered {response.status}")
        return list(parse_phpbb_attachments(
            decode_html(response.body),
            provider="fearless",
            display_name="FearLess Cheat Engine",
            topic_id=sample.topic_id,
            topic_title=sample.title,
            source_page=page,
            score=1.0,
            rank=100,
        ))

    async def fetch_table(self, record, sample: Sampled) -> dict[str, object]:
        """Download one artifact and import it into the scratch store."""
        url = record.direct_url
        filename = record.result.filename
        suffix = Path(filename).suffix.casefold()
        if not url:
            return {"skipped": "no direct link"}
        if suffix not in SUPPORTED_SUFFIXES:
            return {"skipped": f"unsupported {suffix or 'extension'}"}
        destination = self.scratch / f"{sample.topic_id}-{Path(filename).name}"
        while True:
            try:
                await self.network.download(
                    url, destination,
                    allowed_hosts=PROVIDER_HOSTS["fearless"],
                    max_bytes=MAX_ARTIFACT_BYTES,
                    referer=record.result.source_page,
                )
                break
            except ProviderRateLimited as exc:
                if await self._wait_out(exc):
                    continue
                return {"skipped": "rate limited"}
        try:
            if suffix == ".ct":
                artifact = self.store.import_ct(str(destination), display_filename=filename)
            else:
                members = inspect_archive(destination, self.sevenzip).as_dict()["members"]
                tables = [m for m in members if str(m["path"]).casefold().endswith(".ct") and not m["encrypted"]]
                if not tables:
                    return {"skipped": "archive holds no readable .CT"}
                artifact = self.store.import_selection(
                    str(destination), member_path=str(tables[0]["path"]), sevenzip=self.sevenzip,
                )
        except (ArchiveImportError, TableContentError, ValueError, OSError) as exc:
            return {"skipped": f"{type(exc).__name__}: {str(exc)[:100]}"}
        finally:
            destination.unlink(missing_ok=True)
        return {"artifact": artifact}


def summarise(rows: list[dict[str, object]]) -> str:
    tables = [row for row in rows if row.get("usable")]
    if not tables:
        return "no usable tables in the corpus yet"
    total = len(tables)

    def share(predicate) -> str:
        hits = sum(1 for row in tables if predicate(row))
        return f"{hits:4d}/{total}  {hits * 100 // total:3d}%"

    lines = [f"corpus: {total} usable table(s) of {len(rows)} fetched", "", "content:"]
    for label, predicate in (
        ("carries a Lua script", lambda r: r.get("has_lua")),
        ("carries its own window (<Forms>)", lambda r: r.get("has_forms")),
        ("Auto Assembler", lambda r: r.get("has_auto_assembler")),
        ("Auto Assembler embedding Lua", lambda r: r.get("aa_embeds_lua")),
        ("embedded files", lambda r: (r.get("embedded_files") or 0) > 0),
        ("no executable content at all", lambda r: not (
            r.get("has_lua") or r.get("has_forms") or r.get("has_auto_assembler") or (r.get("embedded_files") or 0)
        )),
    ):
        lines.append(f"  {label:34} {share(predicate)}")
    lines += ["", "what the Lua actually calls:"]
    for name in HAZARD_CALLS:
        lines.append(f"  {name:34} {share(lambda r, n=name: n in (r.get('hazards') or []))}")
    skipped: dict[str, int] = {}
    for row in rows:
        reason = row.get("reason") or row.get("skipped")
        if reason:
            key = str(reason).split(":")[0][:44]
            skipped[key] = skipped.get(key, 0) + 1
    if skipped:
        lines += ["", "not measured:"]
        for reason, count in sorted(skipped.items(), key=lambda item: -item[1]):
            lines.append(f"  {reason:44} {count:4d}")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    corpus = args.corpus
    corpus.mkdir(parents=True, exist_ok=True)
    results_path = corpus / "tables.jsonl"
    rows: list[dict[str, object]] = []
    known_digests: set[str] = set()
    known_topics: set[str] = set()
    if results_path.is_file():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            rows.append(row)
            if isinstance(row.get("sha256"), str):
                known_digests.add(row["sha256"])
            if isinstance(row.get("topic_id"), str):
                known_topics.add(row["topic_id"])

    if args.summarise_only:
        print(summarise(rows))
        return 0

    index_copy = corpus / "fearless-index.json"
    if not index_copy.is_file() or args.refresh_index:
        if not args.index.is_file():
            print(f"no FearLess index at {args.index}; run a table search once so the plugin builds it")
            return 1
        shutil.copy2(args.index, index_copy)
    pages = load_index(index_copy)
    if not pages:
        print("the FearLess index copy holds no pages")
        return 1

    samples = [s for s in sample_topics(pages, args.per_stratum, args.seed) if s.topic_id not in known_topics]
    if not samples:
        print("every sampled topic is already in the corpus; raise --per-stratum or change --seed")
        print()
        print(summarise(rows))
        return 0

    context, _ = build_verified_ssl_context()
    network = NetworkClient(context)
    scratch = Path(tempfile.mkdtemp(prefix="ce-decky-corpus-"))
    store = TableStore(corpus / "store")
    survey = Survey(network, store, scratch, shutil.which("7z") or shutil.which("7zz"))
    added = 0
    try:
        with results_path.open("a", encoding="utf-8") as sink:
            for position, sample in enumerate(samples, start=1):
                print(f"[{position}/{len(samples)}] {sample.stratum:7} t={sample.topic_id} {sample.title[:60]}", flush=True)
                base = {"topic_id": sample.topic_id, "stratum": sample.stratum, "title": sample.title}
                try:
                    records = await survey.topic_rows(sample)
                except (NetworkError, ValueError) as exc:
                    sink.write(json.dumps({**base, "skipped": f"topic: {type(exc).__name__}"}) + "\n")
                    sink.flush()
                    continue
                if not records:
                    sink.write(json.dumps({**base, "skipped": "no attachment"}) + "\n")
                    sink.flush()
                    rows.append({**base, "skipped": "no attachment"})
                    continue
                for record in records[: args.per_topic]:
                    outcome = await survey.fetch_table(record, sample)
                    artifact = outcome.get("artifact")
                    if artifact is None:
                        row = {**base, **outcome}
                    elif artifact.sha256 in known_digests:
                        row = {**base, "skipped": "already in corpus", "sha256": artifact.sha256}
                    else:
                        known_digests.add(artifact.sha256)
                        row = {
                            **base,
                            "filename": artifact.filename,
                            "size": artifact.size,
                            **classify(Path(artifact.blob_path), artifact.sha256),
                        }
                        added += 1
                    rows.append(row)
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                    sink.flush()
                    await asyncio.sleep(BETWEEN_DOWNLOADS_SECONDS)
                await asyncio.sleep(BETWEEN_TOPICS_SECONDS)
    finally:
        await network.aclose()
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\nadded {added} table(s) this run")
    print()
    print(summarise(rows))
    return 0



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user-home", help="exact DECKY_USER_HOME; `target_plugin_install.py authority` reports it as user_home")
    parser.add_argument("--index", type=Path, help="FearLess index the plugin maintains, derived from --user-home when omitted; copied, never written")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS, help="where the corpus and its scratch table store live")
    parser.add_argument("--per-stratum", type=int, default=12, help="topics sampled from each of fresh/recent/older")
    parser.add_argument("--per-topic", type=int, default=1, help="artifacts taken from one topic")
    parser.add_argument("--seed", type=int, default=int(time.time()) // 86400, help="sampling seed; the default changes daily")
    parser.add_argument("--refresh-index", action="store_true", help="copy the plugin's index again before sampling")
    parser.add_argument("--summarise-only", action="store_true", help="report the corpus already collected and fetch nothing")
    args = parser.parse_args(argv)
    # Summarising reads only the corpus already on this disk, so it never opens
    # the plugin's index and is not asked which home holds it.
    if args.index is None and not args.summarise_only:
        args.index = steam_user_home(args.user_home) / ".cheat-engine-decky/cache/fearless-index.json"
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
