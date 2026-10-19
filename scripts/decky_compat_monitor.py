#!/usr/bin/env python3
"""Daily check that the published CE Decky release still works in current Decky.

The unit is a pair: one exact Decky runtime (tag and PluginLoader digest) and
one exact CE Decky release (version and ZIP digest). The current pairs are the
latest published CE Decky release against Decky's latest stable release and
its newest prerelease, so a new Decky release, a replaced Decky asset and a new
CE Decky release each make a new pair, and no pair is tested twice unless the
maintainer asks for it.

A pair is tested with the probe from that CE Decky release's own tag, because
the probe reaches into the plugin's internals and only the copy written with a
release knows that release. Beside it runs a control: the same probe in the
Decky runtime that release was pinned to, which is how a Decky regression is
told apart from a probe or runner that broke by itself.

Only `discover` and `record` hold a token, and they write only the ledger and
its status page on one branch. Candidate runtimes run in a separate read-only
job with no token.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "scripts" / "decky_compat_pin.json"
sys.path.insert(0, str(ROOT / "py_modules"))
from ce_decky.plugin_update import REPOSITORY as PLUGIN_REPO, digest_from_sums, parse_release
from ce_decky.atomic import _unique_json_object, _reject_json_constant
from ce_decky.activity_log import safe_log_text

UPSTREAM = "SteamDeckHomebrew/decky-loader"
BRANCH = "compatibility-monitor-state"
STATE_PATH = "decky-compatibility.json"
STATUS_PATH = "STATUS.md"
SIGNER_WORKFLOW = f"{PLUGIN_REPO}/.github/workflows/release.yml"
STALE_AFTER = 72 * 60 * 60
MAX_LEDGER = 512 * 1024
MAX_ASSET = 128 * 1024 * 1024
MAX_PLUGIN = 64 * 1024 * 1024
MAX_PROBE_OUTPUT = 64 * 1024
MAX_HISTORY = 5
PROBE_TIMEOUT = 240
DECKY_TAG = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?$")
PLUGIN_TAG = re.compile(r"^v([0-9]+\.[0-9]+\.[0-9]+)$")
SHA = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
PAIR_KEY = re.compile(r"^[a-z]+/[0-9.]+/[0-9a-f]{16}\+[A-Za-z0-9.-]+/(?:[0-9a-f]{16}|no-asset)$")
CHANNELS = {"github"}
# Every outcome but RETRY is terminal: the pair is not tested again unless asked.
OUTCOMES = {"COMPATIBLE", "BROKEN", "UNVERIFIABLE", "PROBE_BROKEN", "UNSUPPORTED"}
ALERTS = OUTCOMES - {"COMPATIBLE"}
STATES = OUTCOMES | {"PENDING"}
RUN_STATUSES = {"PASS", "FAIL", "INCOMPLETE"}


class MonitorError(RuntimeError):
    pass


class APIError(MonitorError):
    def __init__(self, status):
        super().__init__(f"GitHub API returned HTTP {status}")
        self.status = status


def encode(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()


def decode(value):
    return json.loads(value, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)


def _text(value, limit=500):
    return safe_log_text(value)[:limit]


# Identities ---------------------------------------------------------------

def valid_identity(value):
    return (isinstance(value, dict) and set(value) == {"run_id", "attempt"}
            and all(isinstance(v, str) and len(v) <= 24 and v.isascii() and v.isdigit()
                    and 0 < int(v) for v in value.values()))


def valid_plugin(value):
    if not isinstance(value, dict) or set(value) != {"channel", "version", "tag", "url", "sha256"}:
        return False
    if value["channel"] not in CHANNELS or not isinstance(value["tag"], str):
        return False
    match = PLUGIN_TAG.fullmatch(value["tag"])
    return (match is not None and value["version"] == match.group(1)
            and isinstance(value["sha256"], str) and SHA.fullmatch(value["sha256"]) is not None
            and value["url"] == f"https://github.com/{PLUGIN_REPO}/releases/download/{value['tag']}/CE-Decky-{value['tag']}.zip")


def valid_asset(value, tag):
    return (isinstance(value, dict) and set(value) == {"url", "sha256", "size"}
            and isinstance(value["sha256"], str) and SHA.fullmatch(value["sha256"]) is not None
            and type(value["size"]) is int and 0 < value["size"] <= MAX_ASSET
            and value["url"] == f"https://github.com/{UPSTREAM}/releases/download/{tag}/PluginLoader")


def valid_decky(value):
    return (isinstance(value, dict) and set(value) == {"tag", "channel", "release_id", "asset"}
            and isinstance(value["tag"], str) and len(value["tag"]) <= 128
            and DECKY_TAG.fullmatch(value["tag"]) is not None
            and value["channel"] in {"stable", "prerelease"}
            and type(value["release_id"]) is int and value["release_id"] > 0
            and (value["asset"] is None or valid_asset(value["asset"], value["tag"])))


def pair_key(plugin, decky):
    asset = decky["asset"]["sha256"][:16] if decky["asset"] else "no-asset"
    return f"{plugin['channel']}/{plugin['version']}/{plugin['sha256'][:16]}+{decky['tag']}/{asset}"


def valid_pair(value):
    return (isinstance(value, dict) and set(value) == {"key", "plugin", "decky"}
            and valid_plugin(value["plugin"]) and valid_decky(value["decky"])
            and value["decky"]["asset"] is not None
            and value["key"] == pair_key(value["plugin"], value["decky"]))


def valid_run(value):
    if value is None:
        return True
    if not isinstance(value, dict) or not {"status", "error"} <= set(value) or set(value) - {"status", "error", "python", "frozen", "checks"}:
        return False
    return (value["status"] in RUN_STATUSES and isinstance(value["error"], str) and len(value["error"]) <= 500
            and isinstance(value.get("python", ""), str) and isinstance(value.get("frozen", False), bool)
            and isinstance(value.get("checks", []), list) and all(isinstance(item, str) for item in value.get("checks", [])))


def valid_tested(value):
    return (isinstance(value, dict)
            and set(value) == {"at", "run", "probe_revision", "outcome", "detail", "control", "candidate"}
            and type(value["at"]) is int and value["at"] > 0 and valid_identity(value["run"])
            and isinstance(value["probe_revision"], str) and REVISION.fullmatch(value["probe_revision"]) is not None
            and value["outcome"] in OUTCOMES and isinstance(value["detail"], str) and len(value["detail"]) <= 1200
            and valid_run(value["control"]) and valid_run(value["candidate"]))


def validate_state(state):
    if (not isinstance(state, dict)
            or set(state) != {"schema", "upstream", "plugin_repository", "initialized", "current", "pairs"}
            or state["schema"] != 2 or isinstance(state["schema"], bool)
            or state["upstream"] != UPSTREAM or state["plugin_repository"] != PLUGIN_REPO
            or type(state["initialized"]) is not int or state["initialized"] <= 0
            or not isinstance(state["pairs"], dict) or not isinstance(state["current"], list)):
        raise MonitorError("monitor ledger shape is invalid; restore it, do not reset history")
    for key, row in state["pairs"].items():
        if (not isinstance(key, str) or PAIR_KEY.fullmatch(key) is None or not isinstance(row, dict)
                or not {"plugin", "decky", "state", "first_seen"} <= set(row)
                or set(row) - {"plugin", "decky", "state", "first_seen", "stale_alerted", "tested", "history"}):
            raise MonitorError("monitor ledger pair is invalid")
        if (not valid_plugin(row["plugin"]) or not valid_decky(row["decky"])
                or pair_key(row["plugin"], row["decky"]) != key):
            raise MonitorError("monitor ledger pair identity is invalid")
        if row["state"] not in STATES or type(row["first_seen"]) is not int or row["first_seen"] <= 0:
            raise MonitorError("monitor ledger pair outcome is invalid")
        if "stale_alerted" in row and (type(row["stale_alerted"]) is not int or row["stale_alerted"] <= 0):
            raise MonitorError("monitor ledger stale-alert time is invalid")
        tested = row.get("tested")
        if (row["state"] in OUTCOMES) != (tested is not None) or (tested is not None and (
                not valid_tested(tested) or tested["outcome"] != row["state"])):
            raise MonitorError("monitor ledger lost a terminal result")
        history = row.get("history", [])
        if not isinstance(history, list) or len(history) > MAX_HISTORY or not all(valid_tested(item) for item in history):
            raise MonitorError("monitor ledger history is invalid")
    if (len(set(state["current"])) != len(state["current"])
            or not all(isinstance(key, str) and key in state["pairs"] for key in state["current"])):
        raise MonitorError("monitor ledger current pairs are invalid")
    if len(encode(state)) > MAX_LEDGER:
        raise MonitorError("monitor ledger reached its size budget; preserve history before extending it")
    return state


def new_state(now):
    return validate_state({"schema": 2, "upstream": UPSTREAM, "plugin_repository": PLUGIN_REPO,
                           "initialized": now, "current": [], "pairs": {}})


# GitHub -------------------------------------------------------------------

class NoAPIRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a coordinator token through a redirect.


class AssetRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


BRANCH_README = """# CE Decky compatibility monitor state

This branch is written by `.github/workflows/decky-compatibility.yml` on `main`,
and by nothing else. It shares no history with `main`: never merge it, rebase it
or open a pull request from it, and do not delete it, because its ledger is the
only record of which pairs were already tested.

- `STATUS.md` is the current answer.
- `decky-compatibility.json` is the ledger the workflow reads and writes.

Each commit says what changed. The procedure is in `docs/DEVELOPMENT.md` on
`main`, under **Decky runtime compatibility and upstream watch**.
"""
BRANCH_FILES = {"README.md", STATE_PATH, STATUS_PATH}


class GitHub:
    """The coordinator's only route to the repository.

    Every write is one commit on the monitor branch: a tree that touches only
    the branch's own files, a commit with at most one parent, and a
    fast-forward of that one ref. A tree or a commit nobody points at changes
    nothing anyone sees; moving a ref does, and only this branch's may move.
    """

    def __init__(self, repository, token):
        if repository != PLUGIN_REPO or not token:
            raise MonitorError("coordinator requires this repository's workflow token")
        self.token = token
        self.prefix = f"/repos/{repository}"

    def _allowed(self, method, path, payload):
        if method == "GET":
            return payload is None
        if not isinstance(payload, dict):
            return False
        if method == "POST" and path == f"{self.prefix}/git/trees":
            entries = payload.get("tree")
            return (set(payload) <= {"tree", "base_tree"} and isinstance(entries, list) and bool(entries)
                    and all(isinstance(entry, dict) and set(entry) == {"path", "mode", "type", "content"}
                            and entry["path"] in BRANCH_FILES and entry["mode"] == "100644"
                            and entry["type"] == "blob" and isinstance(entry["content"], str) for entry in entries))
        if method == "POST" and path == f"{self.prefix}/git/commits":
            return (set(payload) == {"message", "tree", "parents"} and isinstance(payload["parents"], list)
                    and len(payload["parents"]) <= 1)
        if method == "POST" and path == f"{self.prefix}/git/refs":
            return set(payload) == {"ref", "sha"} and payload["ref"] == f"refs/heads/{BRANCH}"
        if method == "PATCH" and path == f"{self.prefix}/git/refs/heads/{BRANCH}":
            return set(payload) == {"sha", "force"} and payload["force"] is False
        return False

    def call(self, method, path, payload=None):
        if not self._allowed(method, path, payload):
            raise MonitorError("monitor writes are restricted to its own files on its own branch")
        return self._send(method, path, payload)

    def _send(self, method, path, payload):
        request = urllib.request.Request("https://api.github.com" + path,
            data=encode(payload) if payload is not None else None, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "User-Agent": "CE-Decky-compatibility", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.build_opener(NoAPIRedirect()).open(request, timeout=25) as response:
                body = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code) from None
        if len(body) > 4 * 1024 * 1024:
            raise MonitorError("GitHub API response exceeded its budget")
        return decode(body)

    def _sha(self, value, what):
        sha = value.get("sha") if isinstance(value, dict) else None
        if not isinstance(sha, str) or REVISION.fullmatch(sha) is None:
            raise MonitorError(f"GitHub returned no {what}")
        return sha

    def exists(self):
        try:
            self.call("GET", f"{self.prefix}/git/ref/heads/{BRANCH}")
        except APIError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    def head(self):
        value = self.call("GET", f"{self.prefix}/git/ref/heads/{BRANCH}")
        target = value.get("object") if isinstance(value, dict) else None
        if not isinstance(target, dict) or target.get("type") != "commit":
            raise MonitorError("the monitor branch does not point at a commit")
        return self._sha(target, "monitor branch commit")

    def read(self, commit, path):
        value = self.call("GET", f"{self.prefix}/contents/{path}?ref={commit}")
        if (not isinstance(value, dict) or value.get("type") != "file" or value.get("encoding") != "base64"
                or type(value.get("size")) is not int or value["size"] > MAX_LEDGER):
            raise MonitorError(f"{path} on the monitor branch is not a bounded regular file")
        return base64.b64decode("".join(value["content"].split()), validate=True)

    def load(self):
        """The ledger and status page as of one exact commit, and that commit."""
        head = self.head()
        state = validate_state(decode(self.read(head, STATE_PATH)))
        try:
            status = self.read(head, STATUS_PATH).decode("utf-8")
        except APIError as exc:
            if exc.status != 404:
                raise
            status = None
        return state, head, status

    def _commit(self, files, message, parent, base_tree=None):
        entries = [{"path": name, "mode": "100644", "type": "blob", "content": content}
                   for name, content in sorted(files.items())]
        tree_payload = {"tree": entries} if base_tree is None else {"base_tree": base_tree, "tree": entries}
        tree = self._sha(self.call("POST", f"{self.prefix}/git/trees", tree_payload), "tree")
        parents = [] if parent is None else [parent]
        return self._sha(self.call("POST", f"{self.prefix}/git/commits",
                                   {"message": message, "tree": tree, "parents": parents}), "commit")

    def create(self, state, status):
        """The branch's first commit: its own files and no history shared with main."""
        validate_state(state)
        commit = self._commit({"README.md": BRANCH_README, STATE_PATH: encode(state).decode(), STATUS_PATH: status},
                              "Start the Decky compatibility monitor", None)
        self.call("POST", f"{self.prefix}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit})
        return commit

    def save(self, state, status, message, parent):
        """One commit holding the ledger and the page that describes it.

        The ref only fast-forwards from `parent`, so a writer that read an
        older ledger is refused rather than overwriting a newer one, and the
        page can never describe a ledger it was not written with.
        """
        validate_state(state)
        base = self.call("GET", f"{self.prefix}/git/commits/{parent}")
        base_tree = self._sha(base.get("tree") if isinstance(base, dict) else None, "base tree")
        commit = self._commit({STATE_PATH: encode(state).decode(), STATUS_PATH: status}, message, parent, base_tree)
        self.call("PATCH", f"{self.prefix}/git/refs/heads/{BRANCH}", {"sha": commit, "force": False})
        return commit


def check_download_url(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {
            "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
            or parts.username or parts.password or parts.port not in {None, 443}):
        raise MonitorError("asset URL escaped official GitHub HTTPS hosts")


def download(url, *, maximum, expected_sha=None, expected_size=None):
    check_download_url(url)
    opener = urllib.request.build_opener(AssetRedirects())
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "CE-Decky-compatibility"}), timeout=60) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum or (expected_size is not None and len(body) != expected_size):
        raise MonitorError("asset size differs from its bounded declared identity")
    if expected_sha is not None and (not SHA.fullmatch(expected_sha) or hashlib.sha256(body).hexdigest() != expected_sha):
        raise MonitorError("asset SHA-256 mismatch")
    return body


# Discovery ----------------------------------------------------------------

def decky_identity(release):
    tag = release.get("tag_name") if isinstance(release, dict) else None
    if not isinstance(tag, str) or len(tag) > 128 or not DECKY_TAG.fullmatch(tag) or release.get("draft"):
        raise MonitorError("upstream Decky release identity is unreadable")
    asset = None
    matches = [a for a in release.get("assets", []) if isinstance(a, dict) and a.get("name") == "PluginLoader"]
    if len(matches) == 1:
        value = matches[0]
        candidate = {"url": f"https://github.com/{UPSTREAM}/releases/download/{tag}/PluginLoader",
                     "sha256": str(value.get("digest", "")).removeprefix("sha256:"), "size": value.get("size")}
        # A release can be published before its executable is attached; until
        # it is, the pair waits instead of being judged.
        if value.get("browser_download_url") == candidate["url"] and valid_asset(candidate, tag):
            asset = candidate
    identity = {"tag": tag, "channel": "prerelease" if release.get("prerelease") else "stable",
                "release_id": release.get("id"), "asset": asset}
    if not valid_decky(identity):
        raise MonitorError("upstream Decky release identity is invalid")
    return identity


def current_decky(api):
    """Decky's latest stable release, and its newest prerelease when that is newer."""
    stable = api.call("GET", f"/repos/{UPSTREAM}/releases/latest")
    rows = api.call("GET", f"/repos/{UPSTREAM}/releases?per_page=30")
    if not isinstance(stable, dict) or not isinstance(rows, list):
        raise MonitorError("upstream release feed is unreadable")
    published = stable.get("published_at")
    if not isinstance(published, str):
        raise MonitorError("upstream stable release has no publication time")
    found = [decky_identity(stable)]
    newer = [row for row in rows if isinstance(row, dict) and row.get("prerelease") and not row.get("draft")
             and isinstance(row.get("published_at"), str) and row["published_at"] > published]
    if newer:
        found.append(decky_identity(max(newer, key=lambda row: row["published_at"])))
    return found


def verify_attestation(path, token):
    result = subprocess.run(
        ["gh", "attestation", "verify", str(path), "--repo", PLUGIN_REPO, "--signer-workflow", SIGNER_WORKFLOW],
        env={**os.environ, "GH_TOKEN": token}, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise MonitorError("published CE Decky archive has no valid release attestation: "
                           + _text((result.stderr or result.stdout).strip(), 400))


def plugin_input(api, token):
    """The latest published CE Decky release, proven to be this repository's own build."""
    offer = parse_release(api.call("GET", f"/repos/{PLUGIN_REPO}/releases/latest"), current_version="0.0.0")
    if offer is None:
        raise MonitorError("no published stable CE Decky release")
    prefix = f"https://github.com/{PLUGIN_REPO}/releases/download/{offer.tag}/"
    if offer.archive_url != prefix + offer.archive_name or offer.sums_url != prefix + "SHA256SUMS":
        raise MonitorError("CE Decky release URLs are not canonical public artifact URLs")
    sha = digest_from_sums(download(offer.sums_url, maximum=64 * 1024), offer.archive_name)
    with tempfile.TemporaryDirectory(prefix="ce-decky-monitor-") as directory:
        archive = Path(directory) / offer.archive_name
        archive.write_bytes(download(offer.archive_url, maximum=MAX_PLUGIN, expected_sha=sha))
        verify_attestation(archive, token)
    plugin = {"channel": "github", "version": offer.version, "tag": offer.tag, "url": offer.archive_url, "sha256": sha}
    if not valid_plugin(plugin):
        raise MonitorError("published CE Decky release identity is invalid")
    return plugin


def select_retest(value, state):
    value = (value or "").strip()
    if not value:
        return set()
    if value == "current":
        return set(state["current"])
    if PAIR_KEY.fullmatch(value) is None or value not in state["pairs"]:
        raise MonitorError("retest names no pair this monitor has recorded; copy a key from STATUS.md")
    return {value}


def discover(state, plugins, deckies, now, retest=""):
    """Record the current pairs and return the ones to test, without testing anything."""
    keys = []
    for plugin in plugins:
        for decky in deckies:
            key = pair_key(plugin, decky)
            keys.append(key)
            state["pairs"].setdefault(key, {"plugin": copy.deepcopy(plugin), "decky": copy.deepcopy(decky),
                                            "state": "PENDING", "first_seen": now})
    state["current"] = keys
    forced = select_retest(retest, state)
    untestable = sorted(key for key in forced if state["pairs"][key]["decky"]["asset"] is None)
    if untestable:
        raise MonitorError("a pair whose Decky release has no executable cannot be tested yet: " + ", ".join(untestable))
    work = [key for key in keys if state["pairs"][key]["state"] == "PENDING" and state["pairs"][key]["decky"]["asset"]]
    work += sorted(forced - set(work))
    return [{"key": key, "plugin": copy.deepcopy(state["pairs"][key]["plugin"]),
             "decky": copy.deepcopy(state["pairs"][key]["decky"])} for key in work]


# Probing ------------------------------------------------------------------

def tree_revision(tree):
    result = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    revision = result.stdout.strip()
    if result.returncode != 0 or REVISION.fullmatch(revision) is None:
        raise MonitorError("the release tree is not a checkout of one exact commit")
    return revision


def run_probe(script, tree, plugin_path, runtime_path, *, sha256, version, plugin_sha, output):
    """One run of the release's own probe, reduced to what the ledger keeps."""
    command = [sys.executable, str(script), "--plugin", str(plugin_path), "--decky", str(runtime_path),
               "--sha256", sha256, "--version", version, "--output", str(output)]
    try:
        completed = subprocess.run(command, cwd=tree, capture_output=True, timeout=PROBE_TIMEOUT)
        printed, ending = completed.stdout or b"", f"exit {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        printed, ending = exc.output or b"", "timed out"
    # The ledger keeps one line. The run log keeps what the probe printed,
    # its diagnostic tail included, which is what a failure is diagnosed from.
    print(f"--- probe in Decky {version} ({ending}) ---", file=sys.stderr)
    print(printed.decode("utf-8", "replace")[-6000:], file=sys.stderr)
    if ending == "timed out":
        return {"status": "INCOMPLETE", "error": "the release's probe did not finish in time"}
    try:
        if not output.is_file() or output.stat().st_size > MAX_PROBE_OUTPUT:
            raise ValueError("the release's probe wrote no bounded result")
        raw = decode(output.read_bytes())
        if not isinstance(raw, dict) or raw.get("status") not in RUN_STATUSES:
            raise ValueError("the release's probe result has no outcome")
    except ValueError as exc:
        return {"status": "INCOMPLETE", "error": _text(exc)}
    if raw.get("decky_sha256", sha256) != sha256 or raw.get("plugin_sha256", plugin_sha) != plugin_sha:
        return {"status": "INCOMPLETE", "error": "the release's probe reported other bytes than it was given"}
    if raw["status"] == "PASS" and (raw.get("decky_sha256") != sha256 or raw.get("plugin_sha256") != plugin_sha):
        return {"status": "INCOMPLETE", "error": "the release's probe passed without naming the bytes it tested"}
    run = {"status": raw["status"], "error": _text(raw.get("error", ""))}
    if isinstance(raw.get("python"), str):
        run["python"] = _text(raw["python"], 32)
    if isinstance(raw.get("frozen"), bool):
        run["frozen"] = raw["frozen"]
    if raw["status"] == "PASS" and isinstance(raw.get("checks"), list):
        run["checks"] = sorted(_text(item, 64) for item in raw["checks"] if isinstance(item, str))[:32]
    return run


def classify(control, candidate):
    """Which side broke. `control` is None when the candidate is the release's own pin."""
    if control is None:
        return "COMPATIBLE" if candidate["status"] == "PASS" else "PROBE_BROKEN"
    if control["status"] != "PASS":
        return "PROBE_BROKEN"
    return {"PASS": "COMPATIBLE", "FAIL": "BROKEN"}.get(candidate["status"], "UNVERIFIABLE")


def describe(outcome, pair, pin_version, control, candidate):
    plugin, decky = pair["plugin"]["version"], pair["decky"]["tag"]
    error = (candidate or {}).get("error") or (control or {}).get("error") or "no detail"
    return {
        "COMPATIBLE": f"CE Decky {plugin} passed in Decky {decky}.",
        "BROKEN": f"Decky {decky} breaks CE Decky {plugin}: it passes in its pinned Decky {pin_version} and fails here: {error}",
        "UNVERIFIABLE": f"The probe cannot run in Decky {decky}, so whether CE Decky {plugin} works there is unknown: {error}",
        "PROBE_BROKEN": (f"The probe of CE Decky {plugin} no longer passes in its own pinned Decky {pin_version}, "
                         f"so this run cannot judge Decky {decky}: {(control or candidate or {}).get('error') or 'no detail'}"),
        "UNSUPPORTED": f"CE Decky {plugin} has no compatibility probe at its tag; publish a release that has one.",
    }[outcome][:1200]


def run_pair(pair, tree, work):
    """Test one pair. Returns a result whose outcome is RETRY when inputs could not be fetched."""
    if not valid_pair(pair):
        raise MonitorError("the pair to test is invalid")
    # The release's probe runs in the release tree, so every path handed to it
    # has to mean the same thing from there: a relative one named a file inside
    # the release checkout, which the first run on GitHub reported as missing.
    work = work.resolve()
    result = {"key": pair["key"], "probe_revision": tree_revision(tree), "control": None, "candidate": None}
    script = tree / "scripts" / "decky_compat_probe.py"
    pin_path = tree / "scripts" / "decky_compat_pin.json"
    if not script.is_file() or not pin_path.is_file():
        return {**result, "outcome": "UNSUPPORTED", "detail": describe("UNSUPPORTED", pair, "", None, None)}
    try:
        pin = decode(pin_path.read_bytes())
        baseline = {key: pin[key] for key in ("version", "url", "sha256", "size")}
        if (not isinstance(baseline["version"], str) or not valid_asset({k: baseline[k] for k in ("url", "sha256", "size")}, baseline["version"])):
            raise ValueError("pin identity is invalid")
    except (ValueError, KeyError, TypeError):
        return {**result, "outcome": "UNSUPPORTED",
                "detail": f"CE Decky {pair['plugin']['version']} has no readable Decky pin at its tag."}
    work.mkdir(parents=True, exist_ok=True)
    plugin_path, candidate_path, baseline_path = work / "plugin.zip", work / "candidate-PluginLoader", work / "baseline-PluginLoader"
    asset = pair["decky"]["asset"]
    try:
        plugin_path.write_bytes(download(pair["plugin"]["url"], maximum=MAX_PLUGIN, expected_sha=pair["plugin"]["sha256"]))
        candidate_path.write_bytes(download(asset["url"], maximum=MAX_ASSET, expected_sha=asset["sha256"], expected_size=asset["size"]))
        candidate_path.chmod(0o700)
        if baseline["sha256"] != asset["sha256"]:
            baseline_path.write_bytes(download(baseline["url"], maximum=MAX_ASSET,
                                               expected_sha=baseline["sha256"], expected_size=baseline["size"]))
            baseline_path.chmod(0o700)
    except Exception as exc:
        return {**result, "outcome": "RETRY", "detail": f"inputs could not be fetched: {type(exc).__name__}: {_text(exc, 300)}"}
    common = {"plugin_sha": pair["plugin"]["sha256"]}
    candidate = run_probe(script, tree, plugin_path, candidate_path, sha256=asset["sha256"],
                          version=pair["decky"]["tag"], output=work / "candidate.json", **common)
    control = None
    if baseline["sha256"] != asset["sha256"]:
        control = run_probe(script, tree, plugin_path, baseline_path, sha256=baseline["sha256"],
                            version=baseline["version"], output=work / "control.json", **common)
    outcome = classify(control, candidate)
    return {**result, "outcome": outcome, "control": control, "candidate": candidate,
            "detail": describe(outcome, pair, baseline["version"], control, candidate)}


def valid_result(value):
    return (isinstance(value, dict)
            and set(value) == {"key", "probe_revision", "outcome", "detail", "control", "candidate"}
            and isinstance(value["key"], str) and PAIR_KEY.fullmatch(value["key"]) is not None
            and value["outcome"] in OUTCOMES | {"RETRY"}
            and isinstance(value["probe_revision"], str) and REVISION.fullmatch(value["probe_revision"]) is not None
            and isinstance(value["detail"], str) and len(value["detail"]) <= 1200
            and valid_run(value["control"]) and valid_run(value["candidate"]))


# Recording ----------------------------------------------------------------

def record(state, plan, results, identity, now):
    """Apply results to the ledger. Returns the current pairs newly overdue for a test."""
    planned = {item["key"] for item in plan}
    seen = {}
    for item in results:
        if not valid_result(item) or item["key"] not in planned or item["key"] in seen:
            raise MonitorError("a probe result is invalid, unplanned or repeated")
        seen[item["key"]] = item
    for key, item in seen.items():
        row = state["pairs"].get(key)
        if row is None:
            raise MonitorError("a probe result names a pair the ledger does not hold")
        if item["outcome"] == "RETRY":
            continue
        tested = {"at": now, "run": dict(identity), "probe_revision": item["probe_revision"], "outcome": item["outcome"],
                  "detail": item["detail"], "control": item["control"], "candidate": item["candidate"]}
        if "tested" in row:
            row["history"] = ([row["tested"]] + row.get("history", []))[:MAX_HISTORY]
        row.update(state=item["outcome"], tested=tested)
        row.pop("stale_alerted", None)
    # A pair that waits is silent at first: a download that failed today is
    # usually there tomorrow. One still untested after three days is a
    # monitor that cannot see, and that is said once every three days.
    overdue = []
    for key in state["current"]:
        row = state["pairs"][key]
        if (row["state"] == "PENDING" and now - row["first_seen"] >= STALE_AFTER
                and now - row.get("stale_alerted", 0) >= STALE_AFTER):
            row["stale_alerted"] = now
            overdue.append(key)
    return overdue


def _run_link(run):
    return f"https://github.com/{PLUGIN_REPO}/actions/runs/{run['run_id']}/attempts/{run['attempt']}"


def status_page(state):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("`", "'")

    def when(stamp):
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(stamp))

    lines = ["# Decky compatibility", "",
             "Whether the latest published CE Decky release still works in Decky's latest stable release and its newest prerelease.",
             "Each pair runs the probe from that release's own tag, beside a control run in the Decky the release was pinned to.",
             "These are backend and package checks on Linux x86-64; the Quick Access Menu, controller navigation and Proton are not tested.",
             "", "## Now", "", "| CE Decky | Decky | Result | Checked | Detail | Key |", "|---|---|---|---|---|---|"]
    for key in state["current"]:
        row = state["pairs"][key]
        tested = row.get("tested")
        checked = f"[{when(tested['at'])}]({_run_link(tested['run'])})" if tested else f"waiting since {when(row['first_seen'])}"
        decky = f"[{row['decky']['tag']}](https://github.com/{UPSTREAM}/releases/tag/{row['decky']['tag']}) ({row['decky']['channel']})"
        detail = tested["detail"] if tested else ("Decky has not attached its executable yet." if row["decky"]["asset"] is None else "Not tested yet.")
        lines.append(f"| {row['plugin']['version']} | {decky} | {row['state']} | {checked} | {cell(detail)} | `{key}` |")
    earlier = sorted(((row["tested"]["at"], key, row) for key, row in state["pairs"].items()
                      if key not in state["current"] and "tested" in row), reverse=True)[:30]
    if earlier:
        lines.extend(["", "## Earlier pairs", "", "| CE Decky | Decky | Result | Checked |", "|---|---|---|---|"])
        for at, _, row in earlier:
            lines.append(f"| {row['plugin']['version']} | {row['decky']['tag']} | {row['state']} | [{when(at)}]({_run_link(row['tested']['run'])}) |")
    lines.extend(["", "To test a pair again, run the workflow by hand with `retest` set to its key, or to `current`.", ""])
    return "\n".join(lines)


# Command line -------------------------------------------------------------

def _coordinator():
    identity = {"run_id": os.environ.get("GITHUB_RUN_ID", ""), "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")}
    if not valid_identity(identity):
        raise MonitorError("coordinator requires a GitHub workflow run identity")
    token = os.environ.get("GITHUB_TOKEN", "")
    return GitHub(os.environ.get("GITHUB_REPOSITORY"), token), token, identity


def _pair_name(row):
    return f"Decky {row['decky']['tag']} × CE Decky {row['plugin']['version']}"


def change_message(original, state):
    """What a commit on the monitor branch changed, so its history reads as a log."""
    parts = []
    for key, row in state["pairs"].items():
        before = original["pairs"].get(key)
        if before is None:
            parts.append(f"Track {_pair_name(row)}")
        elif row.get("tested") != before.get("tested"):
            parts.append(f"Record {row['state']} for {_pair_name(row)}")
        elif row.get("stale_alerted") != before.get("stale_alerted") and "stale_alerted" in row:
            parts.append(f"Still untested after three days: {_pair_name(row)}")
    if not parts and state["current"] != original["current"]:
        parts.append("Update the current pairs")
    message = "; ".join(parts) or "Update the status page"
    return message if len(message) <= 300 else message[:297] + "..."


def persist(api, original, state, head, status):
    """Commit the ledger and its page together, and only when either changed."""
    page = status_page(state)
    if state != original or page != status:
        api.save(state, page, change_message(original, state), head)
    return page


def _output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(text + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["initialize", "discover", "probe", "record", "fetch-pin"])
    parser.add_argument("--output", type=Path, default=Path("build/decky-monitor/result.json"))
    parser.add_argument("--release-tree", type=Path, default=Path("release"))
    parser.add_argument("--results", type=Path, default=Path("build/decky-monitor/results"))
    parser.add_argument("--baseline", action="store_true", help="fetch the previous supported runtime, for CI baseline checks")
    args = parser.parse_args()
    try:
        if args.command == "fetch-pin":
            pin = json.loads(PIN.read_text(encoding="utf-8"))
            if args.baseline:
                pin = pin["previous_supported"]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(download(pin["url"], maximum=MAX_ASSET, expected_sha=pin["sha256"], expected_size=pin["size"]))
            args.output.chmod(0o700)
            return 0
        if args.command == "probe":
            # No token reaches this command: it runs unreviewed Decky executables.
            pair = decode(os.environ.get("PAIR", ""))
            result = run_pair(pair, args.release_tree.resolve(), args.output.parent / "work")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(encode(result))
            _summary(f"**{result['outcome']}** - {result['detail']}")
            print(f"{result['outcome']}: {result['detail']}")
            return 1 if result["outcome"] in ALERTS else 0
        api, token, identity = _coordinator()
        now = int(time.time())
        if args.command == "initialize":
            # Explicit first activation only. An existing branch, even one whose
            # ledger is missing, must never be treated as a blank history.
            if api.exists():
                raise MonitorError("state branch already exists; recover its ledger instead of reinitializing")
            state = new_state(now)
            api.create(state, status_page(state))
            return 0
        if args.command == "discover":
            state, head, status = api.load()
            original = copy.deepcopy(state)
            # Everything is read before anything is written, so a failed read
            # leaves the ledger as it was and the next run simply asks again.
            plugins = [plugin_input(api, token)]
            plan = discover(state, plugins, current_decky(api), now, os.environ.get("RETEST", ""))
            persist(api, original, state, head, status)
            _output(matrix=json.dumps(plan, sort_keys=True, separators=(",", ":")), has_work="true" if plan else "false")
            return 0
        state, head, status = api.load()
        original = copy.deepcopy(state)
        plan = decode(os.environ.get("PLAN") or "[]")
        if not isinstance(plan, list) or not all(valid_pair(item) for item in plan):
            raise MonitorError("the discovered plan is invalid")
        results = []
        paths = sorted(args.results.rglob("result.json")) if args.results.is_dir() else []
        if len(paths) > len(plan):
            raise MonitorError("there are more probe results than planned pairs")
        for path in paths:
            if path.stat().st_size > MAX_PROBE_OUTPUT:
                raise MonitorError("a probe result exceeds its budget")
            results.append(decode(path.read_bytes()))
        overdue = record(state, plan, results, identity, now)
        _summary(persist(api, original, state, head, status))
        if overdue:
            print("Decky compatibility could not be tested for three days: " + ", ".join(overdue))
            return 1
        return 0
    except Exception as exc:
        print(f"Compatibility monitor error: {type(exc).__name__}: {str(exc)[:1200]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
