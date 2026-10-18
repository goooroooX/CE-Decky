#!/usr/bin/env python3
"""Three-day Decky release discovery, durable claims and native CI failure alerts.

Only prepare/finalize/status receive GITHUB_TOKEN. Candidate execution is in a
separate read-only job. State writes are restricted to one named ledger branch.
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from scripts.decky_compat_probe import PIN, REQUIRED_CHECKS, probe
except ModuleNotFoundError:
    from decky_compat_probe import PIN, REQUIRED_CHECKS, probe

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))
from ce_decky.plugin_update import REPOSITORY as PLUGIN_REPO, digest_from_sums, parse_release, parse_version
from ce_decky.atomic import _unique_json_object, _reject_json_constant
from ce_decky.activity_log import safe_log_text

UPSTREAM = "SteamDeckHomebrew/decky-loader"
BRANCH = "compatibility-monitor-state"
STATE_PATH = "decky-compatibility.json"
INTERVAL = 72 * 60 * 60
MAX_LEDGER = 512 * 1024
MAX_BATCH = 8
MAX_ASSET = 128 * 1024 * 1024
TAG = re.compile(r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?$")
SHA = re.compile(r"^[0-9a-f]{64}$")
STATES = {"BASELINE", "PENDING", "CLAIMED", "PASS", "FAIL", "INCOMPLETE"}


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


def valid_identity(value):
    return (isinstance(value, dict) and set(value) == {"run_id", "attempt"}
            and all(isinstance(v, str) and len(v) <= 24 and v.isascii() and v.isdigit()
                    and 0 < int(v) for v in value.values()))


def valid_plugin(value):
    if not isinstance(value, dict):
        return False
    if set(value) == {"error"}:
        return isinstance(value["error"], str) and 0 < len(value["error"]) <= 1024
    if (set(value) != {"version", "url", "sha256"} or not isinstance(value["sha256"], str)
            or not SHA.fullmatch(value["sha256"])):
        return False
    version = parse_version(value["version"])
    return (not version.is_prerelease and value["url"] ==
            f"https://github.com/{PLUGIN_REPO}/releases/download/v{version}/CE-Decky-v{version}.zip")


def validate_state(state):
    if (not isinstance(state, dict) or set(state) != {"schema", "upstream", "initialized", "next_discovery", "versions"}
            or state["schema"] != 1 or isinstance(state["schema"], bool)
            or state["upstream"] != UPSTREAM
            or not isinstance(state["versions"], dict)):
        raise MonitorError("monitor ledger shape is invalid; restore it, do not reset history")
    for key in ("initialized", "next_discovery"):
        if type(state[key]) is not int or state[key] <= 0:
            raise MonitorError("monitor ledger timestamp is invalid")
    for tag, row in state["versions"].items():
        if not isinstance(tag, str) or len(tag) > 128 or not TAG.fullmatch(tag):
            raise MonitorError("monitor ledger version identity is invalid")
        if not isinstance(row, dict) or row.get("state") not in STATES:
            raise MonitorError("monitor ledger outcome is invalid")
        required = {"state", "release_id", "channel", "asset"}
        if row["state"] not in {"BASELINE", "PENDING"}:
            required |= {"claim", "plugin", "probe_revision"}
        if row["state"] in {"PASS", "FAIL", "INCOMPLETE"}:
            required.add("result")
        if not required <= set(row) or set(row) - required - {"reported", "asset_changed"}:
            raise MonitorError("monitor ledger version fields are invalid")
        if type(row["release_id"]) is not int or row["release_id"] <= 0 or row["channel"] not in {"stable", "prerelease"}:
            raise MonitorError("monitor ledger release identity is invalid")
        if row["asset"] is not None:
            value = row["asset"]
            if (not isinstance(value, dict) or set(value) != {"url", "sha256", "size"}
                    or not isinstance(value["sha256"], str) or not SHA.fullmatch(value["sha256"])
                    or type(value["size"]) is not int or not 0 < value["size"] <= MAX_ASSET
                    or value["url"] != f"https://github.com/{UPSTREAM}/releases/download/{tag}/PluginLoader"):
                raise MonitorError("monitor ledger artifact identity is invalid")
        if "claim" in row and (not valid_identity(row["claim"]) or not valid_plugin(row["plugin"])):
            raise MonitorError("monitor ledger lost an execution claim")
        if "probe_revision" in row and (not isinstance(row["probe_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", row["probe_revision"])):
            raise MonitorError("monitor ledger lost the probe source identity")
        if "result" in row and (not isinstance(row["result"], dict) or row["result"].get("status") != row["state"]):
            raise MonitorError("monitor ledger lost a terminal result")
        if "reported" in row and (row["state"] not in {"FAIL", "INCOMPLETE"} or not valid_identity(row["reported"])):
            raise MonitorError("monitor ledger notification reservation is invalid")
        if "asset_changed" in row and row["asset_changed"] is not True:
            raise MonitorError("monitor ledger changed-asset marker is invalid")
    if len(encode(state)) > MAX_LEDGER:
        raise MonitorError("monitor ledger reached its size budget; preserve history before extending it")
    return state


class GitHub:
    def __init__(self, repository, token):
        if repository != PLUGIN_REPO or not token:
            raise MonitorError("coordinator requires this repository's workflow token")
        self.repository = repository
        self.token = token
        self.prefix = f"/repos/{repository}"

    def call(self, method, path, payload=None):
        if method != "GET":
            ledger_write = (method == "PUT" and path == f"{self.prefix}/contents/{STATE_PATH}"
                            and isinstance(payload, dict) and payload.get("branch") == BRANCH)
            branch_create = (method == "POST" and path == f"{self.prefix}/git/refs"
                             and isinstance(payload, dict) and payload.get("ref") == f"refs/heads/{BRANCH}")
            if not (ledger_write or branch_create):
                raise MonitorError("monitor writes are restricted to its ledger branch and file")
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

    def load(self):
        value = self.call("GET", f"{self.prefix}/contents/{STATE_PATH}?ref={BRANCH}")
        if value.get("type") != "file" or value.get("encoding") != "base64" or value.get("size", MAX_LEDGER + 1) > MAX_LEDGER:
            raise MonitorError("monitor ledger is not a bounded regular JSON file")
        state = decode(base64.b64decode("".join(value["content"].split()), validate=True))
        return validate_state(state), value["sha"]

    def save(self, state, expected_sha=None):
        validate_state(state)
        payload = {"message": "Record Decky compatibility monitor state", "branch": BRANCH,
                   "content": base64.b64encode(encode(state)).decode()}
        if expected_sha is not None:
            payload["sha"] = expected_sha
        # The expected blob SHA makes a lost race fail before candidate launch.
        # No retries: an ambiguous write is reconciled by a future ledger read.
        value = self.call("PUT", f"{self.prefix}/contents/{STATE_PATH}", payload)
        return value["content"]["sha"]

    def finished(self, claim):
        run_id, attempt = claim.get("run_id"), claim.get("attempt")
        if not str(run_id).isdigit() or not str(attempt).isdigit():
            raise MonitorError("monitor claim has invalid run identity")
        value = self.call("GET", f"{self.prefix}/actions/runs/{run_id}/attempts/{attempt}")
        return value.get("status") == "completed"


class NoAPIRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a coordinator token through a redirect.


class AssetRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_download_url(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in {
            "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
            or parts.username or parts.password or parts.port not in {None, 443}):
        raise MonitorError("asset URL escaped official GitHub HTTPS hosts")


def download(url, *, maximum, expected_sha=None, expected_size=None):
    check_download_url(url)
    opener = urllib.request.build_opener(AssetRedirects())
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "CE-Decky-compatibility"}), timeout=30) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum or (expected_size is not None and len(body) != expected_size):
        raise MonitorError("asset size differs from its bounded declared identity")
    if expected_sha is not None:
        import hashlib
        if not SHA.fullmatch(expected_sha) or hashlib.sha256(body).hexdigest() != expected_sha:
            raise MonitorError("asset SHA-256 mismatch")
    return body


def asset(release):
    matches = [a for a in release.get("assets", []) if a.get("name") == "PluginLoader"]
    if len(matches) != 1:
        return None
    value = matches[0]
    sha = str(value.get("digest", "")).removeprefix("sha256:")
    tag = release["tag_name"]
    expected = f"https://github.com/{UPSTREAM}/releases/download/{urllib.parse.quote(tag, safe='')}/PluginLoader"
    size = value.get("size")
    if value.get("browser_download_url") != expected or not SHA.fullmatch(sha) or type(size) is not int or not 0 < size <= MAX_ASSET:
        return None
    return {"url": expected, "sha256": sha, "size": size}


def releases(api):
    values = []
    for page in range(1, 21):
        rows = api.call("GET", f"/repos/{UPSTREAM}/releases?per_page=100&page={page}")
        if not isinstance(rows, list):
            raise MonitorError("upstream release feed is unreadable")
        values.extend(rows)
        if len(rows) < 100:
            return values
    raise MonitorError("upstream release feed exceeds discovery budget")


def new_state(rows, now):
    state = {"schema": 1, "upstream": UPSTREAM, "initialized": now, "next_discovery": now + INTERVAL, "versions": {}}
    discover(state, rows, baseline=True)
    return validate_state(state)


def discover(state, rows, *, baseline=False):
    for release in rows:
        tag = release.get("tag_name")
        if release.get("draft") or not isinstance(tag, str) or len(tag) > 128 or not TAG.fullmatch(tag):
            continue
        if tag in state["versions"]:
            row = state["versions"][tag]
            if row.get("asset") != asset(release):
                row["asset_changed"] = True
            continue
        state["versions"][tag] = {"state": "BASELINE" if baseline else "PENDING",
            "release_id": release.get("id"), "channel": "prerelease" if release.get("prerelease") else "stable",
            "asset": asset(release)}


def claim_pending(state, identity, plugin, *, probe_revision=None):
    claimed = []
    for tag, row in state["versions"].items():
        if row["state"] != "PENDING" or len(claimed) >= MAX_BATCH:
            continue
        revision = probe_revision or os.environ.get("GITHUB_SHA", "0" * 40)
        row.update(state="CLAIMED", claim=dict(identity), plugin=copy.deepcopy(plugin), probe_revision=revision)
        claimed.append({"tag": tag, **copy.deepcopy(row)})
    return claimed


def settle(state, identity, results):
    seen = {}
    for item in results:
        if (not isinstance(item, dict) or not isinstance(item.get("tag"), str) or item["tag"] in seen
                or item["tag"] not in state["versions"]
                or state["versions"][item["tag"]].get("claim") != identity):
            raise MonitorError("duplicate or invalid candidate result")
        seen[item.get("tag")] = item
    changed = []
    for tag, row in state["versions"].items():
        if row["state"] != "CLAIMED" or row["claim"] != identity:
            continue
        result = seen.get(tag, {}).get("result", {"status": "INCOMPLETE", "error": "candidate job produced no result"})
        if not isinstance(result, dict) or result.get("status") not in {"PASS", "FAIL", "INCOMPLETE"}:
            raise MonitorError("candidate returned an invalid outcome")
        if result["status"] == "PASS":
            if (not row.get("asset") or not row.get("plugin") or "error" in row["plugin"]
                    or result.get("decky_sha256") != row["asset"]["sha256"]
                    or result.get("plugin_sha256") != row["plugin"]["sha256"]):
                raise MonitorError("candidate PASS did not match its claimed artifact identities")
            checks = result.get("checks")
            if (not isinstance(checks, list) or not all(isinstance(check, str) for check in checks)
                    or len(checks) != len(REQUIRED_CHECKS) or set(checks) != REQUIRED_CHECKS
                    or result.get("frozen") is not True
                    or result.get("plugin_version") != row["plugin"]["version"]):
                raise MonitorError("candidate PASS omitted required runtime checks")
        row.update(state=result["status"], result=copy.deepcopy(result))
        changed.append(tag)
    return changed


def validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != {"schema", "identity", "candidates"}
            or plan["schema"] != 1 or not valid_identity(plan["identity"])
            or not isinstance(plan["candidates"], list) or len(plan["candidates"]) > MAX_BATCH):
        raise MonitorError("claimed execution plan is invalid")
    rows = {}
    for candidate in plan["candidates"]:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("tag"), str) or candidate["tag"] in rows:
            raise MonitorError("execution plan repeats or loses a version identity")
        row = {key: value for key, value in candidate.items() if key != "tag"}
        if row.get("state") != "CLAIMED" or row.get("claim") != plan["identity"]:
            raise MonitorError("execution plan lost its original claim")
        rows[candidate["tag"]] = row
    validate_state({"schema": 1, "upstream": UPSTREAM, "initialized": 1, "next_discovery": 1, "versions": rows})
    return plan


def reserve_attention(state, identity):
    tags = []
    for tag, row in state["versions"].items():
        if row["state"] in {"FAIL", "INCOMPLETE"} and not row.get("reported"):
            row["reported"] = dict(identity)
            tags.append(tag)
    return tags


def plugin_input(api):
    offer = parse_release(api.call("GET", f"/repos/{PLUGIN_REPO}/releases/latest"), current_version="0.0.0")
    if offer is None:
        raise MonitorError("no published stable plugin artifact")
    prefix = f"https://github.com/{PLUGIN_REPO}/releases/download/{offer.tag}/"
    if offer.archive_url != prefix + offer.archive_name or offer.sums_url != prefix + "SHA256SUMS":
        raise MonitorError("plugin release URLs are not canonical public artifact URLs")
    sums = download(offer.sums_url, maximum=64 * 1024)
    sha = digest_from_sums(sums, offer.archive_name)
    return {"version": offer.version, "url": offer.archive_url, "sha256": sha}


def prepare(api, identity, now):
    state, revision = api.load()
    original = copy.deepcopy(state)
    for row in state["versions"].values():
        if row["state"] == "CLAIMED" and row["claim"] != identity and api.finished(row["claim"]):
            row.update(state="INCOMPLETE", result={"status": "INCOMPLETE", "error": "original attempt ended without a result"})
    due = now >= state["next_discovery"]
    if due:
        state["next_discovery"] = now + INTERVAL
    if state != original:
        revision = api.save(state, revision)
    if not due:
        return {"schema": 1, "identity": identity, "candidates": []}
    discover(state, releases(api))
    if any(row["state"] == "PENDING" for row in state["versions"].values()):
        try:
            plugin = plugin_input(api)
        except Exception as exc:
            plugin = {"error": f"published plugin input unavailable: {type(exc).__name__}: {safe_log_text(exc)}"}
        candidates = claim_pending(state, identity, plugin)
    else:
        candidates = []
    api.save(state, revision)
    return {"schema": 1, "identity": identity, "candidates": candidates}


def execute_plan(plan, directory, identity):
    validate_plan(plan)
    if plan.get("identity") != identity:
        return []  # A rerun is not a second candidate execution attempt.
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    plugin_cache = {}
    for index, item in enumerate(plan["candidates"]):
        plugin, runtime = item.get("plugin"), item.get("asset")
        result = {"status": "INCOMPLETE", "error": "required released artifact identity is unavailable"}
        if plugin and "error" not in plugin and runtime:
            try:
                work = directory / str(index)
                work.mkdir()
                plugin_path = directory / ("plugin-" + plugin["sha256"] + ".zip")
                runtime_path = work / "PluginLoader"
                if plugin["sha256"] not in plugin_cache:
                    plugin_path.write_bytes(download(plugin["url"], maximum=64 * 1024 * 1024, expected_sha=plugin["sha256"]))
                    plugin_cache[plugin["sha256"]] = plugin_path
                runtime_path.write_bytes(download(runtime["url"], maximum=MAX_ASSET,
                    expected_sha=runtime["sha256"], expected_size=runtime["size"]))
                runtime_path.chmod(0o700)
                result = probe(plugin_path, runtime_path, expected_sha=runtime["sha256"], version=item["tag"])
            except Exception as exc:
                result = {"status": "INCOMPLETE", "error": f"candidate setup failed: {type(exc).__name__}: {safe_log_text(exc)}"}
        results.append({"tag": item["tag"], "result": result})
        (directory / "results.json").write_bytes(encode(results))
    return results


def summary(state):
    lines = ["# Decky compatibility", "", "Automated Linux x86-64 backend/artifact checks; Game Mode UI was not tested.", "",
             "| Decky | Result | Action |", "|---|---|---|"]
    for tag, row in state["versions"].items():
        if row["state"] in {"PASS", "FAIL", "INCOMPLETE"}:
            action = {"PASS": "No action indicated by these checks", "FAIL": "Investigate failing behavior", "INCOMPLETE": "Restore verification; compatibility unknown"}[row["state"]]
            lines.append(f"| {tag} | {row['state']} | {action} |")
    lines.extend(["", "These are persistent outcomes; a silent scan does not retest or repair an old failure.", ""])
    for tag, row in state["versions"].items():
        if "result" not in row:
            continue
        claim = row["claim"]
        run_url = f"https://github.com/{PLUGIN_REPO}/actions/runs/{claim['run_id']}/attempts/{claim['attempt']}"
        lines.extend([f"## {tag}", "", f"Decky release: https://github.com/{UPSTREAM}/releases/tag/{tag}", "",
                      f"Original attempt: {run_url}", "",
                      "```json", json.dumps({"channel": row["channel"], "probe_revision": row["probe_revision"],
                          "asset_changed_since_claim": row.get("asset_changed", False),
                          "artifacts": {"decky": row["asset"], "plugin": row["plugin"]},
                          "result": row["result"]}, indent=2, sort_keys=True).replace("`", "\\u0060"), "```", ""])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["initialize", "prepare", "execute", "finalize", "status", "fetch-pin"])
    parser.add_argument("--output", type=Path, default=Path("build/decky-monitor/plan.json"))
    parser.add_argument("--plan", type=Path, default=Path("build/decky-monitor/plan.json"))
    parser.add_argument("--results", type=Path, default=Path("build/decky-monitor/results.json"))
    parser.add_argument("--baseline", action="store_true", help="fetch the previous supported runtime, for CI baseline checks")
    args = parser.parse_args()
    identity = {"run_id": os.environ.get("GITHUB_RUN_ID", ""), "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")}
    try:
        if args.command == "fetch-pin":
            pin = json.loads(PIN.read_text(encoding="utf-8"))
            if args.baseline:
                pin = pin["previous_supported"]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(download(pin["url"], maximum=MAX_ASSET, expected_sha=pin["sha256"], expected_size=pin["size"]))
            args.output.chmod(0o700)
            return 0
        if args.command == "execute":
            plan = validate_plan(decode(args.plan.read_bytes()))
            results = execute_plan(plan, args.results.parent, identity)
            args.results.parent.mkdir(parents=True, exist_ok=True)
            args.results.write_bytes(encode(results))
            return 0
        if not all(str(value).isdigit() for value in identity.values()):
            raise MonitorError("coordinator requires a GitHub workflow run identity")
        if not re.fullmatch(r"[0-9a-f]{40}", os.environ.get("GITHUB_SHA", "")):
            raise MonitorError("coordinator requires the exact probe source commit")
        api = GitHub(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN"))
        if args.command == "initialize":
            # Explicit first activation only. An existing branch, even one whose
            # ledger is missing, must never be treated as a blank history.
            try:
                api.call("GET", f"{api.prefix}/git/ref/heads/{BRANCH}")
            except APIError as exc:
                if exc.status != 404:
                    raise
            else:
                raise MonitorError("state branch already exists; recover its ledger instead of reinitializing")
            minimum = json.loads(PIN.read_text())["minimum_plugin_version"]
            if parse_version(plugin_input(api)["version"]) < parse_version(minimum):
                raise MonitorError("publish the repaired plugin before activating compatibility monitoring")
            state = new_state(releases(api), int(time.time()))
            api.call("POST", f"{api.prefix}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": os.environ["GITHUB_SHA"]})
            api.save(state)
        elif args.command == "prepare":
            plan = prepare(api, identity, int(time.time()))
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(encode(plan))
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"has_work={'true' if plan['candidates'] else 'false'}\n")
                output.write(f"attempt={identity['attempt']}\n")
                state, _ = api.load()
                pending_attention = any(row["state"] in {"FAIL", "INCOMPLETE"} and not row.get("reported")
                                        for row in state["versions"].values())
                output.write(f"has_finalize={'true' if plan['candidates'] or pending_attention else 'false'}\n")
        elif args.command == "finalize":
            state, revision = api.load()
            plan = validate_plan(decode(args.plan.read_bytes()))
            if plan["identity"]["run_id"] != identity["run_id"] or int(plan["identity"]["attempt"]) > int(identity["attempt"]):
                raise MonitorError("finalizer received another workflow's claim")
            if args.results.is_file():
                if args.results.stat().st_size > 256 * 1024:
                    raise MonitorError("candidate result exceeds its budget")
                results = decode(args.results.read_bytes())
            else:
                results = []
            if settle(state, plan["identity"], results):
                api.save(state, revision)
            report = summary(state)
            Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(report, encoding="utf-8")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report, encoding="utf-8")
        else:
            state, revision = api.load()
            attention = reserve_attention(state, identity)
            if attention:
                api.save(state, revision)
                print("Action required for new Decky results: " + ", ".join(attention))
                return 1
        return 0
    except Exception as exc:
        print(f"Compatibility monitor infrastructure error: {type(exc).__name__}: {str(exc)[:1200]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
