from __future__ import annotations

from hashlib import sha256
from zlib import crc32
from pathlib import Path
import json
import os
import py_compile
import ast
import re
import shutil
import stat
import subprocess
import sys
from urllib.parse import unquote
import zipfile

try:
    from scripts.update_runtime_vendor import failures as runtime_vendor_failures
except ModuleNotFoundError:  # Direct `python scripts/verify_repo.py` execution.
    from update_runtime_vendor import failures as runtime_vendor_failures

ROOT = Path(__file__).resolve().parents[1]
IGNORED_LOCAL_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "artifacts",
    "build",
    "node_modules",
    "__pycache__",
}

REQUIRED = [
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    ".gitattributes",
    "LICENSE",
    "defaults/THIRD_PARTY_NOTICES.md",
    "defaults/licenses/LGPL-2.1.txt",
    "defaults/licenses/CPython-3.11.7.txt",
    "requirements-dev.txt",
    "requirements-runtime.lock",
    "package.json",
    "pnpm-lock.yaml",
    "plugin.json",
    "main.py",
    "src/index.tsx",
    "src/steam/client.ts",
    "src/steam/proton.ts",
    "py_modules/ce_decky/plugin.py",
    "py_modules/ce_decky/archive_import.py",
    "py_modules/ce_decky/game_identity.py",
    "py_modules/ce_decky/network.py",
    "py_modules/ce_decky/catalog.py",
    "py_modules/ce_decky/acquisition.py",
    "py_modules/ce_decky/managed_ce.py",
    "py_modules/ce_decky/managed_ce_manifest.json",
    "py_modules/ce_decky/text.py",
    "py_modules/ce_decky/ct_inspector.py",
    "py_modules/ce_decky/profiles.py",
    "py_modules/ce_decky/session_protocol.py",
    "py_modules/ce_decky/ce_decky_bridge.lua",
    "py_modules/stdlib_fallback/xml/etree/ElementTree.py",
    "py_modules/stdlib_fallback/html/__init__.py",
    "py_modules/stdlib_fallback/html/entities.py",
    "py_modules/stdlib_fallback/html/parser.py",
    "py_modules/stdlib_fallback/_markupbase.py",
    # Committed on purpose: the Store builder installs no Python dependencies,
    # so these are the shipped runtime rather than build output.
    "py_modules/vendor/httpx/__init__.py",
    "py_modules/vendor/bs4/__init__.py",
    "py_modules/vendor/defusedxml/__init__.py",
    "requirements-runtime.vendor.sha256",
    "docs/DESIGN.md",
    # The document `check_release.py` refuses a stable tag against. It is
    # required so that deleting it fails here rather than silently turning
    # that refusal off.
    "docs/FIELD_NOTES.md",
    "scripts/target_snapshot.py",
    "scripts/target_agent_preflight.py",
    # Every target helper asks this module which machine it is on before it
    # touches the device. Deleting it would leave each of them failing on its
    # first POSIX-only call instead of refusing the host by name.
    "scripts/host_platform.py",
    "scripts/target_state_probe.py",
    "scripts/test_ts_core.mjs",
    "scripts/check_release.py",
    "scripts/qa.py",
    "scripts/qa_baseline_check.py",
    "scripts/browser_harness.py",
    "scripts/browser_probe.py",
    "scripts/test_dist_fallback.mjs",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "dist/index.js",
]


def _verify_markdown_links() -> None:
    failures: list[str] = []
    link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
    markdown: list[Path] = []
    for directory, names, files in os.walk(ROOT, followlinks=False):
        names[:] = [name for name in names if name not in IGNORED_LOCAL_DIRS]
        markdown.extend(Path(directory) / name for name in files if name.endswith(".md"))
    for path in sorted(markdown):
        text = path.read_text(encoding="utf-8")
        for match in link_pattern.finditer(text):
            raw = match.group(1).strip()
            if raw.startswith("<") and ">" in raw:
                target = raw[1:raw.index(">")]
            else:
                target = raw.split(maxsplit=1)[0]
            if target.startswith(("#", "http://", "https://", "mailto:")):
                continue
            target = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not target:
                continue
            resolved = path.parent / target
            if not resolved.exists():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")
    if failures:
        raise SystemExit(f"broken local Markdown links: {failures}")


# An em dash is format in exactly two places, both of which this file,
# `check_release.py` parses: the AGENTS development
# version line and a CHANGELOG version heading. Everywhere else in the
# documents that survive to publication it is ordinary punctuation somebody
# typed, and this project does not use it in its own voice.
EM_DASH = "\u2014"
EM_DASH_STRUCTURAL = re.compile(
    r"^(?:## \S+ \u2014 \d{4}-\d{2}-\d{2}"
    r"|Current development version: \*\*[^*]+ \u2014 \d{4}-\d{2}-\d{2}\*\*)$"
)
# The documents the project ships or keeps as standing contracts. What is left
# of the campaign material is deliberately absent: it is deleted by the
# public-release cleanup rather than rewritten.
EM_DASH_ENFORCED = (
    "README.md",
    "AGENTS.md",
    "defaults/THIRD_PARTY_NOTICES.md",
    "docs/README.md",
    "docs/DESIGN.md",
    "docs/ARCHITECTURE.md",
    "docs/SECURITY.md",
    "docs/DEVELOPMENT.md",
    "docs/FIELD_NOTES.md",
    "docs/REMOTE_TARGET.md",
    "CHANGELOG.md",
)
# And the plugin's own text, which is the same voice read by the same people.
# The rule covered documents only, so a sentence a user actually reads on the
# panel was the one place it was not enforced, and one reached the Configure
# cheats screen: a note explaining why a cheat's address did not exist yet used
# an em dash where the rest of the product uses a colon. `dist/` is generated
# from these sources and carries whatever they do, so checking it as well would
# report the same string twice.
EM_DASH_ENFORCED_TREES = ("src",)
EM_DASH_ENFORCED_SUFFIXES = (".ts", ".tsx")
# In those sources the character is usually written as an escape, because the
# rest of the punctuation around it is too: the string that prompted this rule
# spells it `\u2014`. A check that looked for the character alone would have
# passed the exact line it was written for, so the escape counts as the thing
# it produces.
EM_DASH_ESCAPE = re.compile(r"\\u2014", re.IGNORECASE)


def _verify_no_prose_em_dashes() -> None:
    failures: list[str] = []
    # The third element says whether backticks delimit a code span. In Markdown
    # they do, and an em dash inside one is a format being quoted rather than
    # punctuation somebody typed. In TypeScript they delimit a template literal,
    # which is precisely where this plugin's user-facing sentences live, so
    # stripping them there hid the one line this rule was extended for.
    checked: list[tuple[str, list[tuple[int, str]], bool]] = []
    for name in EM_DASH_ENFORCED:
        path = ROOT / name
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        checked.append((name, [(index + 1, line) for index, line in enumerate(lines)], True))
    for tree in EM_DASH_ENFORCED_TREES:
        root = ROOT / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in EM_DASH_ENFORCED_SUFFIXES:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            checked.append((
                path.relative_to(ROOT).as_posix(),
                [(index + 1, line) for index, line in enumerate(lines)],
                False,
            ))
    for name, numbered, code_spans in checked:
        for number, line in numbered:
            if EM_DASH_STRUCTURAL.match(line):
                continue
            bare = re.sub(r"`[^`]*`", "", line) if code_spans else line
            if EM_DASH in bare or EM_DASH_ESCAPE.search(bare):
                failures.append(f"{name}:{number}")
    if failures:
        raise SystemExit(
            "em dash in prose; use a comma, a colon, a full stop or parentheses: "
            f"{failures}"
        )


# Every process this plugin starts, and the environment it may not inherit.
#
# Decky's loader is a PyInstaller bundle and points the dynamic loader at its
# own unpacked libraries. A system binary that inherits that resolves the
# bundle's copies: `/bin/sh` died on the bundle's readline before 7-Zip could
# run, and `journalctl` on the bundle's `libcrypto`, which cost a support bundle
# its whole system journal. `child_env.child_environment` undoes it, and the
# rule is that no child starts without an environment built from it, because
# this class has now been found three times, each time in the newest caller.
# Per module, because `run` belongs to both `subprocess` and `asyncio` and only
# one of them starts a process: `asyncio.run` is this project's own coroutine
# entry point and has no environment to take.
# --- public sanitation -------------------------------------------------------
#
# What a tree about to be published may not carry, and the honest limit of
# looking for it mechanically.
#
# Most of these classes have a shape and are found by it, whatever they turn out
# to say: an address on somebody's own network, an account on a device, a Steam
# user's own directory, the filename a table was downloaded as, a capture kept
# from a device session, the attribution trailer this project keeps out of its
# commits, and a payload file, which is looked for as a file rather than as a
# string.
#
# A game's title has no shape. Nothing tells one apart from `Steam Deck` or
# `Quick Access` without a list of titles, so this does not pretend to recognise
# a name it was never told about; what it does is make a removal stick. A title
# this project decided to take out is listed below as the first 16 hex
# characters of the SHA-256 of its lowercase form, and every one to three word
# phrase in the tree, whatever its case, is hashed and compared against it. The
# guard therefore never restates what it removed, which is the whole point of
# having removed it. It is obfuscation rather than secrecy - anyone holding a
# list of game titles can hash it against these - and it is enough for what it
# is for, which is a name coming back in an edit nobody looked at twice.
#
# Each name is one row holding two numbers, so neither can be added without the
# other. The second is the digest; the first is a CRC of the name's first word
# and exists only to make this affordable: the tree holds about a million words,
# hashing all three window sizes at every one of them is most of a stage meant
# to cost about a second, and the CRC is a fraction of that while letting only a
# couple of words through to be hashed at all. Add a row with:
#
#     python3 scripts/verify_repo.py --removed-name "A Name"
#
# which normalizes it exactly as the matcher does. Working the two values out by
# hand is how a row ends up never matching anything: a Title Cased name hashed
# as it is spelled is dead protection that reports nothing wrong.
#
# What this cannot do is tell a title that belongs in the tree from one that
# does not. A public catalog fact stays: the identity scorer is tested against
# real titles because telling `Resident Evil 3` from `Resident Evil 4` is its
# job, and Steam typing Half-Life 2's episodes as `Tool` is why the game filter
# reads the launch entries beside the type. What goes is a title that says what
# this device holds or what somebody played, which is a judgement made when it
# is removed and recorded here as one digest.
REMOVED_NAMES = (
    (0x226EF210, "d63a9fe0d21b8ba1", "a game this project's own device sessions were run against"),
    (0x23508517, "080ee3fb660fd9cf", "a game this project's own device sessions were run against"),
)
# The plugin's own storage name, and the one filename a document spells out on
# purpose as an example of a release token.
SANITATION_TABLE_FILENAMES = frozenset({"table.CT", "table.ct", "Table_1.0.5.CT"})
# The same documents the em dash rule covers, deliberately: they are the set
# this project ships or keeps as standing contracts, and a document added to one
# rule should not have to be remembered for the other.
SANITATION_DOCUMENTS = EM_DASH_ENFORCED
# Everything else this project owns and can publish, by exclusion rather than by
# a list of suffixes. A list was four of them, which left a manifest, a workflow,
# a package file and a page of provider HTML unread - and a string moved into
# one of those is published exactly as far as a string in a source file is.
#
# What is excluded is what this project does not write: vendored trees and the
# stdlib fallback, the license texts, the images, and `dist/`, which is generated
# from `src/` and would report every string in it a second time.
SANITATION_SKIPPED_DIRS = frozenset(IGNORED_LOCAL_DIRS | {
    "dist", "vendor", "stdlib_fallback", "licenses", "assets",
})
SANITATION_BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".zip", ".7z", ".rar", ".gz", ".woff", ".woff2", ".ttf", ".pdf", ".mp4", ".wasm",
)
# This rule and the fixture that proves it are read like every other file: a
# whole file left out of the scan is a blind spot in the one place a blind spot
# is least acceptable, and a real address pasted into either of them would have
# walked through. Neither spells out a sample as one string - the rules here are
# patterns, and the fixture builds its samples from halves so that allowing them
# by name is not needed and a sample it allowed could not prove anything.
#
# What is left is one invented account that belongs to another test, where it is
# an argument to the remote helper rather than an example of anything.
SANITATION_FIXTURES = frozenset({"deck@device"})
# `documents` is the shipped and contract prose, where a table's own filename is
# somebody's download and never an example. A test fixture legitimately names
# one, so that class is not asked of the code.
SANITATION_PATTERNS = (
    (
        "an address on somebody's own network",
        re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
        SANITATION_FIXTURES,
        "all",
    ),
    (
        "an account on a named device",
        re.compile(r"\b[a-z][a-z0-9_.-]*@(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9-]+\.(?:local|lan|home))\b"),
        SANITATION_FIXTURES,
        "all",
    ),
    (
        # The shape this project's own remote helpers take, which is a bare
        # `user@host` with no domain on it. On its own that shape is far too
        # ordinary to look for - a pinned GitHub action, a password in a test
        # URL and a lockfile entry all read as one - so what is looked for is
        # one handed to something that connects: the account is the part
        # compared, so an invented fixture can be allowed by name.
        "an account handed to a remote command",
        re.compile(
            r"(?:ssh|scp|rsync|--remote|remote\s*[=:])\s*[\"']?"
            r"(?P<hit>[a-z][a-z0-9_.-]*@[a-z][a-z0-9-]*)"
        ),
        SANITATION_FIXTURES,
        "all",
    ),
    (
        "a Steam user's own directory",
        re.compile(r"userdata/\d{4,}"),
        SANITATION_FIXTURES,
        "all",
    ),
    (
        "the filename a table was downloaded as",
        re.compile(r"\b[A-Za-z0-9_+-][A-Za-z0-9_.+-]*\.(?:CT|ct)\b"),
        SANITATION_TABLE_FILENAMES,
        "documents",
    ),
    (
        "a capture kept from a device session",
        re.compile(r"build/incidents/[A-Za-z0-9_-]+-\d{8}T\d{4}"),
        SANITATION_FIXTURES,
        "all",
    ),
    (
        # A trailer is a line, and anchoring to one is what lets this rule be
        # written down here and in its own fixture without either of them
        # tripping it: a pasted commit message carries the trailer at the start
        # of a line, while a sentence about it does not.
        "an agent attribution trailer",
        re.compile(r"^[ \t]*Co-Authored-By:|Generated with \[Claude Code\]", re.MULTILINE),
        frozenset(),
        "all",
    ),
)
# Every word, whatever its case and whatever sits between them. Capitalisation
# looked like a cheap way to find the proper nouns and was a way past the whole
# rule instead: the digests are of lowercased phrases, but a phrase only became
# a candidate if it started with a capital, so the same name written in lower
# case walked through. Words are also joined across whatever separates them, so
# a name broken over two lines by a comment wrap is still one phrase.
_WORD = re.compile(r"[A-Za-z0-9'\u2019]+")


def _sanitation_sources() -> list[tuple[str, bool]]:
    """Every file this rule reads, and whether it is one of the documents."""
    documents = set(SANITATION_DOCUMENTS)
    sources: list[tuple[str, bool]] = []
    for directory, names, files in os.walk(ROOT):
        names[:] = sorted(name for name in names if name not in SANITATION_SKIPPED_DIRS)
        for name in sorted(files):
            relative = (Path(directory) / name).relative_to(ROOT).as_posix()
            if name.lower().endswith(SANITATION_BINARY_SUFFIXES):
                continue
            sources.append((relative, relative in documents))
    return sources


def removed_name_entry(name: str) -> tuple[int, str]:
    """The two numbers `REMOVED_NAMES` holds for one name.

    One place produces them and one place consumes them, normalizing the same
    way: lowercased, split into words by the same expression, joined by single
    spaces. Written out by hand they can disagree with the matcher without
    saying so - a Title Cased name hashed as it is spelled never matches
    anything and the protection is dead on arrival, which is the worst kind of
    guard to have. `python scripts/verify_repo.py --removed-name "A Name"`
    prints the row to paste in.
    """
    words = [word.lower() for word in _WORD.findall(name)]
    if not words or len(words) > 3:
        raise SystemExit("a removed name is one to three words")
    return crc32(words[0].encode()), sha256(" ".join(words).encode()).hexdigest()[:16]


def _removed_names(text: str) -> set[str]:
    """Which listed digests this text spells out, as digests rather than names."""
    found: set[str] = set()
    if not REMOVED_NAMES:
        return found
    marks = {mark for mark, _, _ in REMOVED_NAMES}
    wanted = {digest for _, digest, _ in REMOVED_NAMES}
    words = [match.group().lower() for match in _WORD.finditer(text)]
    for start, word in enumerate(words):
        if crc32(word.encode()) not in marks:
            continue
        for size in (1, 2, 3):
            if start + size > len(words):
                break
            digest = sha256(" ".join(words[start:start + size]).encode()).hexdigest()[:16]
            if digest in wanted:
                found.add(digest)
    return found


# A payload is a file rather than a string, so it is looked for as one. The
# plugin ZIP already refuses these as members; this refuses them in the tree
# they are packaged from, where one arrives first - a table copied in to
# reproduce something, a Cheat Engine binary dropped beside the code.
SANITATION_PAYLOAD_SUFFIXES = (".ct", ".cetrainer", ".exe", ".7z", ".rar", ".msi", ".dll")


def _verify_public_sanitation() -> None:
    failures: list[str] = []
    for directory, names, files in os.walk(ROOT):
        # Pruned in place rather than filtered afterwards: walking `node_modules`
        # to throw it away costs three seconds of a stage that takes under two.
        names[:] = sorted(name for name in names if name not in IGNORED_LOCAL_DIRS)
        for name in sorted(files):
            if Path(name).suffix.lower() in SANITATION_PAYLOAD_SUFFIXES:
                relative = (Path(directory) / name).relative_to(ROOT).as_posix()
                failures.append(f"{relative} is a payload this repository does not carry")
    for relative, is_document in _sanitation_sources():
        path = ROOT / relative
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Not text this project wrote. Reading every file by exclusion is
            # what catches a string moved into a manifest or a workflow, and the
            # cost of that is meeting the occasional file that is not text at
            # all; one of those is nothing to scan rather than a reason for this
            # whole check to end in a traceback.
            continue
        for label, pattern, allowed, scope in SANITATION_PATTERNS:
            if scope == "documents" and not is_document:
                continue
            for match in pattern.finditer(text):
                # The whole match, except where a rule names the part that is
                # the finding: a rule that has to read what surrounds a string
                # still compares only the string.
                hit = match.groupdict().get("hit") or match.group()
                if hit in allowed:
                    continue
                line = text.count("\n", 0, match.start()) + 1
                failures.append(f"{relative}:{line} carries {label}: {hit!r}")
        reasons = {digest: why for _, digest, why in REMOVED_NAMES}
        for digest in sorted(_removed_names(text)):
            failures.append(
                f"{relative} names {reasons[digest]}, removed on purpose "
                f"(digest {digest}; the name is not repeated here)"
            )
    if failures:
        raise SystemExit(
            "publication sanitation: "
            + "; ".join(sorted(failures))
        )


SUBPROCESS_STARTERS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}


def _verify_child_processes_take_an_environment() -> None:
    failures: list[str] = []
    missing_helper: list[str] = []
    for path in sorted((ROOT / "py_modules").glob("**/*.py")):
        if "__pycache__" in path.as_posix():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        relative = path.relative_to(ROOT).as_posix()
        starts_a_child = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            root = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", "")
            if node.func.attr not in SUBPROCESS_STARTERS.get(root, ()):
                continue
            starts_a_child = True
            if not any(keyword.arg == "env" for keyword in node.keywords):
                failures.append(f"{relative}:{node.lineno}")
        # An `env=` built from anything at all satisfies the check above, so the
        # module that starts a child must also be one that knows where an
        # environment comes from. Not the call site, because a caller may build
        # it once and add to it before passing it on.
        if starts_a_child and "child_environment" not in path.read_text(encoding="utf-8"):
            missing_helper.append(relative)
    if failures or missing_helper:
        raise SystemExit(
            "a child process would inherit the loader bundle's library path; pass "
            f"env=child_environment(...): {failures + missing_helper}"
        )


# The lifecycle claim this project withdrew, and the reason it is guarded.
#
# For a while the repository said Decky calls `_main`, every RPC and `_unload`
# through separate `asyncio.run()` calls, in a test name and in a comment beside
# the unload drain. It is false: the sandboxed plugin process makes one event
# loop and runs it forever, which `docs/FIELD_NOTES.md` records with the
# upstream source and the device reading. Two static reviews read those words,
# believed them, and reported defects the host does not have; a third would have
# done the same. So the phrasing cannot come back except where the notes are
# explaining that it was wrong.
WITHDRAWN_LIFECYCLE = re.compile(
    r"separate\s+`?asyncio\.run|loop of its own|Decky[- ]contract lifecycle",
    re.IGNORECASE,
)
# The notes are where the correction lives, the changelog is where it was
# announced, and this file is the rule itself.
WITHDRAWN_LIFECYCLE_ALLOWED = ("docs/FIELD_NOTES.md", "CHANGELOG.md", "scripts/verify_repo.py")


def _verify_withdrawn_lifecycle_claim_stays_withdrawn() -> None:
    failures: list[str] = []
    # The places this project writes its own words in, rather than a walk of the
    # whole tree: generated bundles, dependencies and build output are neither
    # authored here nor read by anybody looking for the contract.
    candidates: list[Path] = sorted(ROOT.glob("*.md"))
    for directory in ("py_modules", "src", "tests", "scripts", "docs"):
        root = ROOT / directory
        if root.is_dir():
            candidates.extend(sorted(root.glob("**/*.py")))
            candidates.extend(sorted(root.glob("**/*.md")))
            candidates.extend(sorted(root.glob("**/*.ts")))
            candidates.extend(sorted(root.glob("**/*.tsx")))
    for path in candidates:
        relative = path.relative_to(ROOT).as_posix()
        if relative in WITHDRAWN_LIFECYCLE_ALLOWED or "__pycache__" in relative:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            if WITHDRAWN_LIFECYCLE.search(line):
                failures.append(f"{relative}:{number}")
    if failures:
        raise SystemExit(
            "the withdrawn lifecycle claim is back; Decky keeps one event loop for the plugin "
            f"process, as docs/FIELD_NOTES.md records: {failures}"
        )


# The other claim this project has had to withdraw, for the same reason.
#
# An install ends by reading the panel's own durable record to say how many
# CE Decky rows the reloaded frontend ended up holding, and that record is
# allowed not to arrive: the helper reports `panel import unobserved` and the
# install is still an install. For a while the operating contract described that
# wait as the thing that "proves there is exactly one", which is true only when
# the record was actually read, and a contract that overstates it is read by the
# next agent as a deployment that settled a question it did not settle.
#
# So the unconditional phrasing cannot come back. What the helper actually
# proves is in `docs/DEVELOPMENT.md`, and the measurement that used to be
# embedded beside it lives in `docs/FIELD_NOTES.md`.
UNCONDITIONAL_PANEL_PROOF = re.compile(
    r"(?:proves|proving|proof)\s+(?:that\s+)?there\s+is\s+exactly\s+one"
    r"|panel\s+cardinality\s+is\s+proven",
    re.IGNORECASE,
)
UNCONDITIONAL_PANEL_PROOF_ALLOWED = ("CHANGELOG.md", "scripts/verify_repo.py")


def _verify_panel_count_is_not_claimed_unconditionally() -> None:
    failures: list[str] = []
    candidates: list[Path] = sorted(ROOT.glob("*.md"))
    for directory in ("docs", "scripts", "py_modules", "src"):
        root = ROOT / directory
        if root.is_dir():
            candidates.extend(sorted(root.glob("**/*.md")))
            candidates.extend(sorted(root.glob("**/*.py")))
    for path in candidates:
        relative = path.relative_to(ROOT).as_posix()
        if relative in UNCONDITIONAL_PANEL_PROOF_ALLOWED or "__pycache__" in relative:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, start=1):
            if UNCONDITIONAL_PANEL_PROOF.search(line):
                failures.append(f"{relative}:{number}")
    if failures:
        raise SystemExit(
            "an install is described as proving the panel holds exactly one CE Decky; it proves that "
            "only when the panel's own record arrived, and `panel import unobserved` means it did not: "
            f"{failures}"
        )


FOCUSABLE_FLOWS = {
    "column",
    "column-reverse",
    "geometric",
    "grid",
    "row",
    "row-reverse",
}
FOCUSABLE_FLOW_LITERAL = re.compile(
    r"<Focusable\b[^>]*?\bflow-children\s*=\s*[\"']([^\"']+)[\"']",
    re.DOTALL,
)
FOCUSABLE_FLOW_OWNER = Path("src/components/PanelDensity.tsx")

# Steam's own section title renders its text inside a shrink-to-fit element
# whose class is a bare content hash on the shipped client, so the rule that
# separates one section from the next cannot be attached to it. CE Decky renders
# `SectionHeading` as the section's first child instead, and the two looked
# different on screen for as long as both were in use.
STEAM_SECTION_TITLE = re.compile(r"<PanelSection\b[^>]*?\btitle\s*=", re.DOTALL)

# Fields the public-release cleanup removed from the API, wherever an object is
# built. Removing a production field and leaving the fixtures emitting it is not
# a harmless leftover: the fixture stops modelling the contract, so a regression
# suite goes on proving a payload the plugin no longer sends, and nothing says
# so. The ordinary type-check cannot catch it either, because `tsconfig.json`
# covers `src` and not `tests`. The key is refused in every tracked tree that
# builds one, production and test alike.
REMOVED_API_FIELDS = ("gate",)
REMOVED_FIELD_TREES = ("src", "tests", "py_modules", "scripts")
REMOVED_FIELD_SUFFIXES = (".ts", ".tsx", ".js", ".mjs", ".py", ".json")


# The interpreter this plugin actually runs on is Decky's bundled CPython
# 3.11.7, and CI pins 3.11 to match it. A development device on 3.13 accepts
# syntax that neither of those can parse, and the failure it produces is a
# whole file that will not import rather than one test that fails, so it is
# worth refusing here rather than discovering on the target.
MIN_PYTHON = (3, 11)
# PEP 701 relaxed f-strings in 3.12: an escape inside a replacement field, and
# a quote inside one that matches the quote around the string, are both syntax
# errors before that. `ast.parse(feature_version=...)` does not see either,
# because f-strings are tokenized before the grammar is chosen, so they are
# read out of the token stream instead.
def _fstring_is_312_only(source: str) -> list[int]:
    """Lines whose f-string only a 3.12 or later tokenizer accepts."""
    import io
    import tokenize

    if not hasattr(tokenize, "FSTRING_START"):
        # This interpreter is the one being checked for. It has no separate
        # f-string tokens because it parses an f-string in one piece, and it
        # raises its own SyntaxError on exactly these forms, which the parse
        # around this call already reports with a line number. There is nothing
        # left for an outside reader to find.
        return []

    lines: list[int] = []
    quotes: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.FSTRING_START:
                if quotes and token.string[-1] in quotes[-1]:
                    lines.append(token.start[0])
                quotes.append(token.string.lstrip("fFrRbB"))
                continue
            if token.type == tokenize.FSTRING_END:
                if quotes:
                    quotes.pop()
                continue
            if not quotes:
                continue
            if token.type == tokenize.FSTRING_MIDDLE:
                continue
            # Inside a replacement field: an escape is 3.12 only, and so is a
            # nested string closed by the quote the f-string itself uses.
            if "\\" in token.string:
                lines.append(token.start[0])
            elif token.type == tokenize.STRING and token.string.lstrip("rRbBuU")[:1] in quotes[-1]:
                lines.append(token.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Whatever this is, the parse below reports it with a line number.
        return []
    return sorted(set(lines))


def _verify_ci_source_snapshot() -> None:
    """Keep the exact head CI validated readable by a review that cannot clone.

    Each of these is load-bearing rather than stylistic, and each of them is
    easy to lose to a change that looks like a tidy-up:

    - the merge commit `pull_request` checks out is not the head anybody
      reviewed, so archiving `github.sha` there archives a commit that exists
      nowhere else;
    - `fetch-depth: 2` is what puts the real head in the object store beside
      that merge commit, so a shallower checkout leaves nothing to archive;
    - packing before the dependency restore is what makes the snapshot survive
      a run that fails afterwards, which is exactly the run somebody wants to
      read the source of;
    - `git archive` takes the commit rather than the working tree, so no stage
      can write into what is published;
    - `if-no-files-found: error` turns a snapshot that was not built into a
      failed run rather than an empty artifact;
    - `retention-days` plus GitHub's own expiry is the whole of the cleanup,
      which is why this workflow needs no permission to delete anything.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    failures: list[str] = []
    if "github.event.pull_request.head.sha" not in workflow:
        failures.append("archives the merge commit rather than the pull request head")
    if "git archive" not in workflow:
        failures.append("does not build the snapshot with git archive")
    if "fetch-depth: 2" not in workflow:
        failures.append("checkout is too shallow to hold the head being archived")
    if "if-no-files-found: error" not in workflow:
        failures.append("a snapshot that was not built would upload empty")
    if "retention-days:" not in workflow:
        failures.append("the snapshot has no expiry")
    if re.search(r"^\s*actions:\s*write", workflow, re.MULTILINE):
        failures.append("grants artifact deletion permission; expiry is the cleanup")
    archive_at = workflow.find("git archive")
    # Whichever of the two starts the dependency work, because either one being
    # first means the snapshot is not built until something else has succeeded.
    restores = [at for at in (workflow.find("actions/setup-python"), workflow.find("requirements-dev.txt")) if at >= 0]
    if archive_at >= 0 and restores and min(restores) < archive_at:
        failures.append("packs the snapshot after the dependency restore, so a failed run loses it")
    if failures:
        raise SystemExit(f"CI source snapshot contract broken: {failures}")


# Helpers that are never run by a person, so `docs/DEVELOPMENT.md` has no
# command to carry for them. Each entry is a reason rather than a waiver, and
# the reason has to be that something already documented is what runs it.
UNDOCUMENTED_HELPERS = {
    "browser_probe.py": "started only by browser_harness.py, which is the documented entrypoint",
    "frontend_source_digest.py": "run by package_plugin.py, whose own command is documented",
    "steam_user.py": "a module the provider surveys import; it has no command of its own",
    "test_ts_core.mjs": "a stage runner qa.py invokes; it has no command of its own",
    "test_dist_fallback.mjs": "a stage runner qa.py invokes; it has no command of its own",
    # The two enforcement gates, which `AGENTS.md` places deliberately outside
    # the tracked-helper table for the same reason: they are reached through a
    # QA profile and through the release procedure rather than run to answer a
    # question. `docs/DEVELOPMENT.md` and `docs/README.md` say what each refuses.
    "verify_repo.py": "a QA profile runs it; it is a gate rather than a helper",
    "check_release.py": "the release procedure runs it; it is a gate rather than a helper",
}


def _verify_helpers_are_documented() -> None:
    """Every helper under `scripts/` is named in `docs/DEVELOPMENT.md`.

    `AGENTS.md` sends an agent to the tracked helpers before it writes a command
    of its own, and the whole value of that table is that the helper it needs is
    in it. A helper nobody documented is one nobody finds: the next agent writes
    the `find` or the `python3 -c` it was meant to replace, and the fact it
    produces is one that cannot be reproduced.

    So the rule is the one the contract already states, enforced rather than
    trusted. It was trusted until `qa_durations.py` was added, when the question
    "is the helper registered, and does the build fail if it is not" turned out
    to have the answers yes and no: the tree was honest because the contract had
    been followed, which is not the same as being checked.

    Naming the file anywhere in the document satisfies this. It cannot check
    that what is written is any good, and it does not try to: what it refuses is
    a helper the document has never heard of. The name has to stand on its own
    rather than inside a longer one, so a helper cannot be counted as documented
    because another helper's name happens to contain it.
    """
    development = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
    missing: list[str] = []
    for path in sorted((ROOT / "scripts").glob("*.py")) + sorted((ROOT / "scripts").glob("*.mjs")):
        name = path.name
        if name.startswith("_") or name in UNDOCUMENTED_HELPERS:
            continue
        if not re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w])", development):
            missing.append(f"scripts/{name}")
    stale = [name for name in UNDOCUMENTED_HELPERS if not (ROOT / "scripts" / name).is_file()]
    if stale:
        raise SystemExit(
            f"UNDOCUMENTED_HELPERS names helpers that are gone: {stale}. "
            "Remove the entry with the helper it was written for."
        )
    if missing:
        raise SystemExit(
            f"helpers are not documented in docs/DEVELOPMENT.md: {missing}. "
            "Give each one a command and what it guarantees, or add it to "
            "UNDOCUMENTED_HELPERS with the documented helper that runs it."
        )


def _verify_agents_last_section() -> None:
    """Keep the marker that says whether `AGENTS.md` was read whole honest.

    Step 1 of **Start here** tells a reader that the file ends with a named
    section, so that a truncated read is something they can notice for
    themselves rather than a contract they silently never saw. Appending a
    section below it would make that instruction a lie, and the reader it lies
    to is the one who already cannot see the end of the file.
    """
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", agents, re.MULTILINE)
    if not headings:
        raise SystemExit("AGENTS.md declares no sections")
    last = headings[-1].strip()
    if f"ends with **{last}**" not in agents:
        raise SystemExit(
            f"AGENTS.md ends with '{last}'; Start here names a different section as its last. "
            "Update step 1 so a truncated read is still detectable."
        )


def _verify_python_runtime_syntax() -> None:
    """Every tracked module must parse on the oldest interpreter that runs it."""
    import ast

    failures: list[str] = []
    for directory, names, files in os.walk(ROOT, followlinks=False):
        names[:] = [name for name in names if name not in IGNORED_LOCAL_DIRS]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            relative = path.relative_to(ROOT)
            source = path.read_text(encoding="utf-8")
            try:
                ast.parse(source, filename=str(relative), feature_version=MIN_PYTHON)
            except SyntaxError as exc:
                failures.append(f"{relative}:{exc.lineno}: {exc.msg}")
                continue
            for line in _fstring_is_312_only(source):
                failures.append(f"{relative}:{line}: f-string needs Python 3.12")
    if failures:
        raise SystemExit(
            "Python syntax newer than the "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} runtime this plugin ships on: {failures}"
        )


def _verify_focusable_flows() -> None:
    """Hold directional groups to Steam's vocabulary and enabled-control guard."""
    failures: list[str] = []
    source_root = ROOT / "src"
    if not source_root.is_dir():
        return
    for path in sorted(source_root.rglob("*.ts*")):
        source = path.read_text(encoding="utf-8")
        for match in FOCUSABLE_FLOW_LITERAL.finditer(source):
            value = match.group(1)
            line = source.count("\n", 0, match.start(1)) + 1
            relative = path.relative_to(ROOT)
            if value not in FOCUSABLE_FLOWS:
                failures.append(f"{relative}:{line}={value!r}")
            elif relative != FOCUSABLE_FLOW_OWNER:
                failures.append(f"{relative}:{line}=bypass ActionGroup")
    if failures:
        raise SystemExit(f"unsafe Steam Focusable flow-children use: {failures}")


_JSX_TAG_OPEN = re.compile(r"<[A-Za-z][A-Za-z0-9_.]*")


def _comments_between_props(source: str) -> list[int]:
    """Line numbers of comments written between one element's props.

    The scan is a small state machine rather than a pattern, because the thing
    that matters is context: the same `//` line is ordinary everywhere else in
    the file. It tracks quoted and templated spans so a brace inside one cannot
    move the depth, and it counts depth inside a tag so that a comment in an
    attribute's own expression - which is plain JavaScript and survives the
    build like any other - is left alone.
    """
    hits: list[int] = []
    index = 0
    length = len(source)
    line = 1
    in_tag = False
    depth = 0
    while index < length:
        char = source[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if char in "\"'`":
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == "\n":
                    line += 1
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length if end < 0 else end + 2
            if in_tag and depth == 0:
                hits.append(line)
            line += source.count("\n", index, end)
            index = end
            continue
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            if in_tag and depth == 0:
                hits.append(line)
            index = end
            continue
        if in_tag:
            if char in "{([":
                depth += 1
            elif char in "})]":
                depth -= 1
            elif char == ">" and depth == 0:
                in_tag = False
            index += 1
            continue
        match = _JSX_TAG_OPEN.match(source, index)
        if match is not None and (index == 0 or source[index - 1] not in "=<"):
            in_tag = True
            depth = 0
            index = match.end()
            continue
        index += 1
    return hits


def _verify_no_comments_between_props() -> None:
    """Keep the tracked bundle free of the trailing whitespace one of these leaves.

    A comment between two props is ordinary TypeScript and reads well in the
    source, and the pinned Rollup build turns it into a line of `dist/index.js`
    that ends in a space. The bundle is tracked, so `git diff --check` refuses
    the commit - at the end of a release run, after the backend suite, the
    component suite and the build have all been paid for. This is the same
    finding in the first second of the run instead. The comment belongs above
    the element, where it costs the bundle nothing.
    """
    failures: list[str] = []
    source_root = ROOT / "src"
    if not source_root.is_dir():
        return
    # `.tsx` only, which is where every element in this tree is written. The
    # scan decides it is inside a tag from a `<` followed by a name, and in a
    # plain `.ts` file that is also what an unspaced comparison or a generic
    # looks like: `count<limit` would leave it reading the lines after it as
    # props, and the next ordinary comment as a violation. Scanning a file that
    # cannot hold the defect to begin with buys nothing and risks exactly that.
    for path in sorted(source_root.rglob("*.tsx")):
        for line in _comments_between_props(path.read_text(encoding="utf-8")):
            failures.append(f"{path.relative_to(ROOT)}:{line}")
    if failures:
        raise SystemExit(
            "a comment between an element's props makes the pinned Rollup build emit "
            f"trailing whitespace into the tracked bundle; move it above the element: {failures}"
        )


def _verify_section_headings() -> None:
    """Hold every section to CE Decky's own heading rather than Steam's title."""
    failures: list[str] = []
    source_root = ROOT / "src"
    if not source_root.is_dir():
        return
    for path in sorted(source_root.rglob("*.ts*")):
        source = path.read_text(encoding="utf-8")
        for match in STEAM_SECTION_TITLE.finditer(source):
            line = source.count("\n", 0, match.start()) + 1
            failures.append(f"{path.relative_to(ROOT)}:{line}")
    if failures:
        raise SystemExit(
            "use <SectionHeading> as the section's first child instead of PanelSection title: "
            f"{failures}"
        )


def _verify_removed_api_fields() -> None:
    """Refuse a field the cleanup deleted from the API coming back in a fixture."""
    patterns = {
        name: re.compile(rf"(?:(?<=[{{,])|^)\s*[\"']?{re.escape(name)}[\"']?\s*:", re.MULTILINE)
        for name in REMOVED_API_FIELDS
    }
    failures: list[str] = []
    for tree in REMOVED_FIELD_TREES:
        root = ROOT / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in REMOVED_FIELD_SUFFIXES:
                continue
            if any(part in IGNORED_LOCAL_DIRS for part in path.parts):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            for name, pattern in patterns.items():
                for match in pattern.finditer(source):
                    line = source.count("\n", 0, match.start()) + 1
                    failures.append(f"{path.relative_to(ROOT)}:{line}={name}")
    if failures:
        raise SystemExit(
            "the public-release cleanup removed these API fields; a fixture that still "
            f"emits one no longer models the contract: {failures}"
        )


# The one required file that is built rather than written, and the one whose
# absence almost never means what this check's own wording says. A frontend
# build that fails removes it, so the next run's very first stage reports a
# missing tracked file and the reader goes looking for a deleted file instead of
# at the build that just failed. Naming the cause here costs a line and saved
# two calls the one time it happened.
BUILT_BUNDLE = "dist/index.js"


def _verify_runtime_vendor() -> None:
    """The committed runtime payload is the one the lock produced.

    `scripts/update_runtime_vendor.py` owns both the rebuild and this
    comparison; restating either here would give the tree two authorities that
    can disagree. What is enforced is that they were run: a lock bumped without
    a rebuild, and a tree edited after one, are each a failure here rather than
    a backend that imports something nobody pinned.
    """
    found = runtime_vendor_failures()
    if found:
        raise SystemExit("; ".join(found))


def main() -> None:
    # One argument, and it is not a mode this check runs in: it prints the row
    # to paste into `REMOVED_NAMES`, normalized the way the matcher normalizes.
    if len(sys.argv) == 3 and sys.argv[1] == "--removed-name":
        mark, digest = removed_name_entry(sys.argv[2])
        print(f'    (0x{mark:08X}, "{digest}", "why this name was removed"),')
        return
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        rebuild = (
            f". {BUILT_BUNDLE} is built rather than written: a frontend build that failed removes it, "
            "so rebuild with `python scripts/qa.py --profile frontend --stage frontend-build` and read "
            "that stage's failure rather than this one"
            if BUILT_BUNDLE in missing else ""
        )
        raise SystemExit(f"missing required files: {missing}{rebuild}")
    _verify_markdown_links()
    _verify_no_prose_em_dashes()
    _verify_withdrawn_lifecycle_claim_stays_withdrawn()
    _verify_panel_count_is_not_claimed_unconditionally()
    _verify_child_processes_take_an_environment()
    _verify_ci_source_snapshot()
    _verify_agents_last_section()
    _verify_helpers_are_documented()
    _verify_python_runtime_syntax()
    _verify_focusable_flows()
    _verify_no_comments_between_props()
    _verify_section_headings()
    _verify_removed_api_fields()
    _verify_runtime_vendor()
    _verify_public_sanitation()

    package = json.loads((ROOT / "package.json").read_text())
    plugin = json.loads((ROOT / "plugin.json").read_text())
    if not re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        package["version"],
    ):
        raise SystemExit("package.json version is not valid SemVer")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    agent_version = re.search(
        r"^Current development version: \*\*([^*]+) — (\d{4}-\d{2}-\d{2})\*\*$",
        agents,
        re.MULTILINE,
    )
    changelog_version = re.search(
        r"^## ([^ ]+) — (\d{4}-\d{2}-\d{2})$",
        changelog,
        re.MULTILINE,
    )
    if agent_version is None or changelog_version is None:
        raise SystemExit("development version/date metadata is missing from AGENTS.md or CHANGELOG.md")
    expected = (package["version"], agent_version.group(2))
    if (agent_version.group(1), agent_version.group(2)) != expected or changelog_version.groups() != expected:
        raise SystemExit("package.json, AGENTS.md, and newest CHANGELOG.md version/date are not synchronized")
    assert plugin["api_version"] == 1
    assert "root" not in plugin.get("flags", [])

    required_packages = {
        "@decky/api": "1.1.3",
        "tslib": "2.7.0",
    }
    required_dev_packages = {
        "@decky/rollup": "1.0.2",
        "@decky/ui": "4.12.0",
        "@rollup/rollup-linux-x64-musl": "4.53.3",
        "@types/react": "19.1.1",
        "@types/react-dom": "19.1.1",
        "@types/react-router": "5.1.20",
        "rollup": "4.53.3",
        "typescript": "5.6.2",
    }
    for name, version in required_packages.items():
        if package.get("dependencies", {}).get(name) != version:
            raise SystemExit(f"unexpected dependency pin for {name}")
    for name, version in required_dev_packages.items():
        if package.get("devDependencies", {}).get(name) != version:
            raise SystemExit(f"unexpected devDependency pin for {name}")
    if (ROOT / "LICENSE").stat().st_size < 30_000:
        raise SystemExit("LICENSE does not contain the full GPL-3.0 text")
    notices = (ROOT / "defaults" / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    if "CPython 3.11.7" not in notices or "fa7a6f23036537567592647d15f043722c7144ad" not in notices:
        raise SystemExit("Decky stdlib fallback attribution missing")
    steam_client_source = (ROOT / "src" / "steam" / "client.ts").read_text(encoding="utf-8")
    for token in ("readAppDetails", "listInstalledGames", "listRunningGames"):
        if token not in steam_client_source:
            raise SystemExit(f"client.ts missing required Steam API surface: {token}")
    # CE Decky reads Steam identity and never writes Steam state. These setters
    # must stay absent from the frontend, not merely go uncalled.
    for path in (ROOT / "src").rglob("*.ts*"):
        source = path.read_text(encoding="utf-8")
        for token in ("SetAppLaunchOptions", "SetShortcutLaunchOptions"):
            if token in source:
                raise SystemExit(f"frontend must not reference the Steam launch-option setter {token}: {path.relative_to(ROOT)}")
    service_source = (ROOT / "py_modules" / "ce_decky" / "service.py").read_text(encoding="utf-8")
    session_source = (ROOT / "py_modules" / "ce_decky" / "session_protocol.py").read_text(encoding="utf-8")
    for token in ("session_current", "session_stale_reason", "config_state_reason", "table_state_reason"):
        if token not in service_source:
            raise SystemExit(f"service.py missing current defensive status surface: {token}")
    for token in ("acknowledges a generation absent", "retry_attach"):
        if token not in session_source:
            raise SystemExit(f"session_protocol.py missing command-log integrity surface: {token}")

    frontend = (ROOT / "src" / "index.tsx").read_text(encoding="utf-8")
    # The picker resolves one path and routes it by extension: a .exe registers
    # an existing installation, a .zip is a packed installation directory. Each
    # import call must appear exactly once so a second, unreviewed call site
    # cannot import from somewhere the picker never validated.
    if frontend.count("await importCE(source);") != 1:
        raise SystemExit("CE picker must invoke importCE exactly once")
    if frontend.count("await importCEArchive(source);") != 1:
        raise SystemExit("CE picker must invoke importCEArchive exactly once")
    # Decky's last openFilePicker argument is the page size, not a selection
    # limit. Passing a small number there renders that many entries per
    # directory, which reads as a broken picker rather than as a bad argument.
    if re.search(r"openFilePicker\((?:[^()]|\([^()]*\))*,\s*(?:[1-9]\d{0,2})\s*,?\s*\)", frontend):
        raise SystemExit("openFilePicker must not pass a small page size")
    if "profile.table_library" not in frontend or "localArtifactMap" not in frontend:
        raise SystemExit("frontend missing exact-SHA per-game local table reuse")

    frontend_api = (ROOT / "src" / "api.ts").read_text(encoding="utf-8")
    for token in (
        "plan_provider_search", "evaluate_provider_candidates", "get_managed_ce_capability",
        "start_managed_ce_install", "poll_managed_ce_install", "complete_managed_ce_install", "cancel_managed_ce_install",
        "search_tables", "start_table_acquisition", "poll_table_acquisition", "complete_table_acquisition", "cancel_table_acquisition",
    ):
        if token not in frontend_api:
            raise SystemExit(f"frontend API missing production/stub capability: {token}")

    for path in [
        ROOT / "main.py",
        *sorted((ROOT / "py_modules" / "ce_decky").glob("*.py")),
        *sorted((ROOT / "scripts").glob("*.py")),
    ]:
        py_compile.compile(str(path), doraise=True)

    forbidden = []
    for path in list((ROOT / "py_modules").rglob("*.py")) + list((ROOT / "src").rglob("*.ts*")):
        text = path.read_text(encoding="utf-8")
        # The prototype tree is deleted. The guard stays: production importing
        # a spike is the defect, and it costs nothing to keep refusing a name
        # that can only reappear by somebody restoring one.
        if "research.prototypes" in text or "ce_decky_proto" in text:
            forbidden.append(str(path.relative_to(ROOT)))
    if forbidden:
        raise SystemExit(f"production imports research code: {forbidden}")


    bridge = (ROOT / "py_modules" / "ce_decky" / "ce_decky_bridge.lua").read_text(encoding="utf-8")
    for token in (
        "dofile(", "loadfile(", "loadstring(", "load(",
        'f:read("*a")', "enableDRM", "activateProtection",
    ):
        if token in bridge:
            raise SystemExit(f"Lua bridge contains forbidden construct: {token}")
    # No third-party executable/table payload belongs in production/distribution inputs.
    for base in (ROOT / "py_modules", ROOT / "src", ROOT / "dist"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".exe", ".ct"}:
                raise SystemExit(f"vendored CE/table payload in production tree: {path.relative_to(ROOT)}")

    node = shutil.which("node")
    if node:
        subprocess.run([node, "--check", str(ROOT / "dist" / "index.js")], check=True)
        dist = (ROOT / "dist" / "index.js").read_text(encoding="utf-8")
        for token in (
            "inspect_table_source",
            "write_runtime_commands",
            "set_pinned_control",
            "associate_table",
            "set_autoload",
            "set_remembered_cheats",
            # Controller-only repair paths that must stay reachable from Advanced:
            # a Game Mode user cannot undo these from a terminal.
            "clear_ce_import",
            "clear_startup_preference",
            "get_removal_readiness",
            "plan_provider_search",
            "evaluate_provider_candidates",
            "get_managed_ce_capability",
            "start_managed_ce_install",
            "poll_managed_ce_install",
            "complete_managed_ce_install",
            "cancel_managed_ce_install",
            "search_tables",
            "start_table_acquisition",
            "complete_table_acquisition",
        ):
            if token not in dist:
                raise SystemExit(f"fallback dist missing current production surface: {token}")

    artifact = ROOT / "artifacts" / f"CE-Decky-v{package['version']}.zip"
    if artifact.exists():
        with zipfile.ZipFile(artifact) as zf:
            bad = zf.testzip()
            if bad:
                raise SystemExit(f"corrupt packaged member: {bad}")
            names = set(zf.namelist())
            required_members = {
                "CE-Decky/dist/index.js",
                "CE-Decky/main.py",
                "CE-Decky/package.json",
                "CE-Decky/plugin.json",
                "CE-Decky/LICENSE",
                "CE-Decky/THIRD_PARTY_NOTICES.md",
                "CE-Decky/licenses/LGPL-2.1.txt",
                "CE-Decky/licenses/CPython-3.11.7.txt",
                "CE-Decky/py_modules/ce_decky/plugin.py",
                "CE-Decky/py_modules/ce_decky/game_identity.py",
                "CE-Decky/py_modules/ce_decky/managed_ce.py",
                "CE-Decky/py_modules/ce_decky/managed_ce_manifest.json",
                "CE-Decky/py_modules/ce_decky/ce_decky_bridge.lua",
                "CE-Decky/py_modules/vendor/httpx/__init__.py",
                "CE-Decky/py_modules/vendor/bs4/__init__.py",
                "CE-Decky/py_modules/vendor/defusedxml/__init__.py",
                "CE-Decky/py_modules/stdlib_fallback/xml/etree/ElementTree.py",
                "CE-Decky/py_modules/stdlib_fallback/html/__init__.py",
                "CE-Decky/py_modules/stdlib_fallback/html/entities.py",
                "CE-Decky/py_modules/stdlib_fallback/html/parser.py",
                "CE-Decky/py_modules/stdlib_fallback/_markupbase.py",
            }
            missing_members = required_members - names
            if missing_members:
                raise SystemExit(f"plugin ZIP missing members: {sorted(missing_members)}")
            forbidden_members = [name for name in names if name.lower().endswith((".exe", ".ct"))]
            if forbidden_members:
                raise SystemExit(f"plugin ZIP vendors forbidden payloads: {forbidden_members}")
            if len(names) != len(zf.infolist()):
                raise SystemExit("plugin ZIP contains duplicate member names")
            expected_timestamp = (2026, 8, 15, 0, 0, 0)
            for info in zf.infolist():
                if info.date_time != expected_timestamp:
                    raise SystemExit(f"plugin ZIP member has non-reproducible timestamp: {info.filename}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if mode and not stat.S_ISREG(mode):
                    raise SystemExit(f"plugin ZIP contains a non-regular member: {info.filename}")

    print("repository verification: PASS")


if __name__ == "__main__":
    main()
