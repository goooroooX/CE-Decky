import base64
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

from scripts import decky_compat_monitor as monitor


NOW = 1_000_000
RUN = {"run_id": "100", "attempt": "1"}
BYTES = {"plugin": b"plugin archive", "candidate": b"candidate runtime", "baseline": b"baseline runtime"}
SHAS = {name: hashlib.sha256(data).hexdigest() for name, data in BYTES.items()}
DECKY_URL = f"https://github.com/{monitor.UPSTREAM}/releases/download/{{tag}}/PluginLoader"


def plugin(version="0.9.30", sha=SHAS["plugin"]):
    tag = f"v{version}"
    return {"channel": "github", "version": version, "tag": tag, "sha256": sha,
            "url": f"https://github.com/{monitor.PLUGIN_REPO}/releases/download/{tag}/CE-Decky-{tag}.zip"}


def decky(tag="v3.4.0", *, channel="stable", sha=SHAS["candidate"], size=len(BYTES["candidate"]), asset=True):
    return {"tag": tag, "channel": channel, "release_id": 7,
            "asset": {"url": DECKY_URL.format(tag=tag), "sha256": sha, "size": size} if asset else None}


def release(tag, *, prerelease=False, published="2026-10-01T00:00:00Z", sha="a" * 64, draft=False):
    return {"id": 7, "tag_name": tag, "draft": draft, "prerelease": prerelease, "published_at": published,
            "assets": [{"name": "PluginLoader", "size": 16, "digest": "sha256:" + sha,
                        "browser_download_url": DECKY_URL.format(tag=tag)}]}


def _settle(state, key, outcome="COMPATIBLE"):
    plan = [{"key": key, **{side: state["pairs"][key][side] for side in ("plugin", "decky")}}]
    result = {"key": key, "probe_revision": "c" * 40, "outcome": outcome, "detail": "fixture",
              "control": {"status": "PASS", "error": ""}, "candidate": {"status": "PASS", "error": ""}}
    return monitor.record(state, plan, [result], RUN, NOW)


# Which pairs exist ------------------------------------------------------------

def test_current_set_is_latest_stable_and_only_a_newer_prerelease():
    feed = [release("v3.5.0-pre2", prerelease=True, published="2026-10-09T00:00:00Z"),
            release("v3.5.0-pre1", prerelease=True, published="2026-10-05T00:00:00Z"),
            release("v3.6.0-pre1", prerelease=True, published="2026-10-10T00:00:00Z", draft=True),
            release("v3.4.0-pre9", prerelease=True, published="2026-09-01T00:00:00Z")]
    stable = release("v3.4.0", published="2026-10-01T00:00:00Z")

    class Feed:
        def call(self, method, path):
            return copy.deepcopy(stable if path.endswith("/releases/latest") else feed)

    found = monitor.current_decky(Feed())
    assert [(item["tag"], item["channel"]) for item in found] == [("v3.4.0", "stable"), ("v3.5.0-pre2", "prerelease")]
    feed[:] = [release("v3.4.0-pre9", prerelease=True, published="2026-09-01T00:00:00Z")]
    assert [item["tag"] for item in monitor.current_decky(Feed())] == ["v3.4.0"]
    # Published before its executable was attached, or with one under another URL.
    stable["assets"] = []
    assert monitor.current_decky(Feed())[0]["asset"] is None
    stable["assets"] = [{**release("v3.4.0")["assets"][0], "browser_download_url": "https://example.com/PluginLoader"}]
    assert monitor.current_decky(Feed())[0]["asset"] is None


def test_each_new_side_makes_a_new_pair_and_a_tested_pair_is_not_repeated():
    state = monitor.new_state(NOW)
    first = monitor.discover(state, [plugin()], [decky()], NOW)
    assert len(first) == 1
    _settle(state, first[0]["key"])
    assert not monitor.discover(state, [plugin()], [decky()], NOW + 1)
    # A new CE Decky release is tested against the Decky that is already current.
    newer = monitor.discover(state, [plugin("0.9.31", "d" * 64)], [decky()], NOW + 2)
    assert [item["plugin"]["version"] for item in newer] == ["0.9.31"]
    # Decky replacing the executable of a tag it already published is a new runtime.
    replaced = monitor.discover(state, [plugin("0.9.31", "d" * 64)], [decky(sha="e" * 64)], NOW + 3)
    assert {item["key"] for item in replaced} == {key for key in state["current"]}
    assert len(state["current"]) == 1 and len(state["pairs"]) == 3


def test_release_without_executable_waits_and_is_overdue_once_every_three_days():
    state = monitor.new_state(NOW)
    assert not monitor.discover(state, [plugin()], [decky(asset=False)], NOW)
    key = state["current"][0]
    assert key.endswith("/no-asset") and state["pairs"][key]["state"] == "PENDING"
    assert not monitor.record(state, [], [], RUN, NOW + monitor.STALE_AFTER - 1)
    assert monitor.record(state, [], [], RUN, NOW + monitor.STALE_AFTER) == [key]
    assert not monitor.record(state, [], [], RUN, NOW + monitor.STALE_AFTER + 60)
    assert monitor.record(state, [], [], RUN, NOW + 2 * monitor.STALE_AFTER) == [key]
    # A pair that is no longer current says nothing, however long it waited.
    later = NOW + 3 * monitor.STALE_AFTER
    monitor.discover(state, [plugin()], [decky()], later)
    assert key not in state["current"]
    assert not monitor.record(state, [], [], RUN, later + monitor.STALE_AFTER - 1)


def test_retest_is_the_only_way_a_tested_pair_runs_again():
    state = monitor.new_state(NOW)
    key = monitor.discover(state, [plugin()], [decky()], NOW)[0]["key"]
    _settle(state, key, "BROKEN")
    assert [item["key"] for item in monitor.discover(state, [plugin()], [decky()], NOW, "current")] == [key]
    assert [item["key"] for item in monitor.discover(state, [plugin()], [decky()], NOW, key)] == [key]
    for bad in ("github/0.9.30/" + "0" * 16 + "+v3.4.0/" + "0" * 16, "current; rm -rf /", "$(id)"):
        with pytest.raises(monitor.MonitorError, match="retest"):
            monitor.discover(state, [plugin()], [decky()], NOW, bad)
    monitor.discover(state, [plugin()], [decky("v3.5.0", asset=False)], NOW)
    with pytest.raises(monitor.MonitorError, match="no executable"):
        monitor.discover(state, [plugin()], [decky("v3.5.0", asset=False)], NOW, "current")


# Which side broke -------------------------------------------------------------

@pytest.mark.parametrize("control, candidate, outcome", [
    ("PASS", "PASS", "COMPATIBLE"),
    ("PASS", "FAIL", "BROKEN"),
    ("PASS", "INCOMPLETE", "UNVERIFIABLE"),
    ("FAIL", "PASS", "PROBE_BROKEN"),
    ("INCOMPLETE", "FAIL", "PROBE_BROKEN"),
    (None, "PASS", "COMPATIBLE"),
    (None, "FAIL", "PROBE_BROKEN"),
    (None, "INCOMPLETE", "PROBE_BROKEN"),
])
def test_control_run_decides_which_side_broke(control, candidate, outcome):
    run = lambda status: None if status is None else {"status": status, "error": ""}
    assert monitor.classify(run(control), run(candidate)) == outcome


FAKE_PROBE = r'''
import argparse, hashlib, json, pathlib
parser = argparse.ArgumentParser()
for name in ("--plugin", "--decky", "--sha256", "--version", "--output"):
    parser.add_argument(name)
args = parser.parse_args()
here = pathlib.Path(__file__).parent
behaviour = json.loads((here / "behaviour.json").read_text())
with (here / "runs.log").open("a") as log:
    log.write(args.version + "\n")
status = behaviour.get(args.sha256, "PASS")
reported = behaviour.get("report_sha", args.sha256)
plugin_sha = hashlib.sha256(pathlib.Path(args.plugin).read_bytes()).hexdigest()
pathlib.Path(args.output).write_text(json.dumps({"status": status, "decky_sha256": reported,
    "plugin_sha256": plugin_sha, "error": "" if status == "PASS" else "fixture " + status,
    "python": "3.14.8", "frozen": True, "checks": ["entrypoint"]}))
'''


@pytest.fixture
def release_tree(tmp_path, monkeypatch):
    tree = tmp_path / "release"
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts" / "decky_compat_probe.py").write_text(FAKE_PROBE)
    (tree / "scripts" / "behaviour.json").write_text("{}")
    (tree / "scripts" / "decky_compat_pin.json").write_text(json.dumps({
        "version": "v3.3.0", "url": DECKY_URL.format(tag="v3.3.0"),
        "sha256": SHAS["baseline"], "size": len(BYTES["baseline"])}))
    by_sha = {sha: BYTES[name] for name, sha in SHAS.items()}
    monkeypatch.setattr(monitor, "download", lambda url, *, maximum, expected_sha=None, expected_size=None: by_sha[expected_sha])
    monkeypatch.setattr(monitor, "tree_revision", lambda tree: "c" * 40)
    return tree


def _pair(decky_identity=None):
    plugin_identity, decky_identity = plugin(), decky_identity or decky()
    return {"key": monitor.pair_key(plugin_identity, decky_identity), "plugin": plugin_identity, "decky": decky_identity}


def _behave(tree, **values):
    (tree / "scripts" / "behaviour.json").write_text(json.dumps(values))


def test_pair_runs_the_release_trees_own_probe_beside_its_pinned_control(release_tree, tmp_path):
    _behave(release_tree, **{SHAS["candidate"]: "FAIL"})
    result = monitor.run_pair(_pair(), release_tree, tmp_path / "work")
    assert result["outcome"] == "BROKEN"
    assert "Decky v3.4.0 breaks CE Decky 0.9.30" in result["detail"] and "v3.3.0" in result["detail"]
    assert result["control"]["status"] == "PASS" and result["candidate"]["status"] == "FAIL"
    assert (release_tree / "scripts" / "runs.log").read_text().split() == ["v3.4.0", "v3.3.0"]
    assert monitor.valid_result(result)


def test_candidate_that_is_the_release_pin_runs_once(release_tree, tmp_path):
    pinned = decky("v3.3.0", sha=SHAS["baseline"], size=len(BYTES["baseline"]))
    result = monitor.run_pair(_pair(pinned), release_tree, tmp_path / "work")
    assert result["outcome"] == "COMPATIBLE" and result["control"] is None
    assert (release_tree / "scripts" / "runs.log").read_text().split() == ["v3.3.0"]


def test_probe_reporting_other_bytes_is_never_a_pass(release_tree, tmp_path):
    _behave(release_tree, report_sha="f" * 64)
    result = monitor.run_pair(_pair(), release_tree, tmp_path / "work")
    assert result["candidate"]["status"] == "INCOMPLETE" and "other bytes" in result["candidate"]["error"]
    assert result["outcome"] == "PROBE_BROKEN"


def test_release_without_a_probe_is_unsupported_before_any_download(release_tree, tmp_path, monkeypatch):
    (release_tree / "scripts" / "decky_compat_probe.py").unlink()
    monkeypatch.setattr(monitor, "download", lambda *a, **k: pytest.fail("downloaded for an unsupported release"))
    assert monitor.run_pair(_pair(), release_tree, tmp_path / "work")["outcome"] == "UNSUPPORTED"


def test_input_that_cannot_be_fetched_is_retried_not_judged(release_tree, tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("network is unreachable")
    monkeypatch.setattr(monitor, "download", unavailable)
    result = monitor.run_pair(_pair(), release_tree, tmp_path / "work")
    assert result["outcome"] == "RETRY" and "unreachable" in result["detail"]
    state = monitor.new_state(NOW)
    plan = monitor.discover(state, [plugin()], [decky()], NOW)
    monitor.record(state, plan, [result], RUN, NOW)
    assert state["pairs"][result["key"]]["state"] == "PENDING"


# Recording --------------------------------------------------------------------

def test_record_keeps_history_and_refuses_unplanned_or_repeated_results():
    state = monitor.new_state(NOW)
    plan = monitor.discover(state, [plugin()], [decky()], NOW)
    key = plan[0]["key"]
    for outcome in ["BROKEN", "COMPATIBLE"] + ["COMPATIBLE"] * monitor.MAX_HISTORY:
        _settle(state, key, outcome)
    row = state["pairs"][key]
    assert row["state"] == "COMPATIBLE" and len(row["history"]) == monitor.MAX_HISTORY
    monitor.validate_state(state)
    result = {"key": key, "probe_revision": "c" * 40, "outcome": "BROKEN", "detail": "x", "control": None,
              "candidate": {"status": "FAIL", "error": "x"}}
    with pytest.raises(monitor.MonitorError, match="unplanned or repeated"):
        monitor.record(state, plan, [result, result], RUN, NOW)
    with pytest.raises(monitor.MonitorError, match="unplanned or repeated"):
        monitor.record(state, [], [result], RUN, NOW)
    # A rerun that produced no result for a pair leaves its answer alone.
    monitor.record(state, plan, [], RUN, NOW)
    assert state["pairs"][key]["state"] == "COMPATIBLE"


def test_status_page_names_every_current_pair_and_how_to_retest_it():
    state = monitor.new_state(NOW)
    key = monitor.discover(state, [plugin()], [decky()], NOW)[0]["key"]
    plan = [{"key": key, "plugin": plugin(), "decky": decky()}]
    monitor.record(state, plan, [{"key": key, "probe_revision": "c" * 40, "outcome": "BROKEN",
                                  "detail": "fails | with a pipe", "control": {"status": "PASS", "error": ""},
                                  "candidate": {"status": "FAIL", "error": "boom"}}], RUN, NOW)
    monitor.discover(state, [plugin()], [decky(), decky("v3.5.0-pre1", channel="prerelease", asset=False)], NOW)
    page = monitor.status_page(state)
    assert f"`{key}`" in page and "| 0.9.30 |" in page and "BROKEN" in page
    assert "fails \\| with a pipe" in page and "/actions/runs/100/attempts/1" in page
    assert "has not attached its executable" in page and "retest" in page


# Trust boundaries ---------------------------------------------------------------

@pytest.mark.parametrize("bad", [{}, None, {"schema": 1}, {"schema": True}])
def test_unreadable_or_missing_ledger_is_not_a_new_baseline(bad):
    with pytest.raises(monitor.MonitorError):
        monitor.validate_state(bad)


def test_ledger_rejects_a_pair_whose_key_does_not_name_its_identity():
    state = monitor.new_state(NOW)
    key = monitor.discover(state, [plugin()], [decky()], NOW)[0]["key"]
    state["pairs"][key]["plugin"]["sha256"] = "9" * 64
    with pytest.raises(monitor.MonitorError, match="identity"):
        monitor.validate_state(state)


@pytest.mark.parametrize("text", ['{"pairs": {}, "pairs": {}}', '{"value": NaN}'])
def test_ledger_decoder_never_silently_discards_history(text):
    with pytest.raises(ValueError):
        monitor.decode(text)


def test_published_plugin_must_carry_this_repositorys_release_attestation(monkeypatch):
    tag = "v0.9.30"
    prefix = f"https://github.com/{monitor.PLUGIN_REPO}/releases/download/{tag}/"
    latest = {"tag_name": tag, "draft": False, "prerelease": False, "html_url": f"https://github.com/{monitor.PLUGIN_REPO}/releases/tag/{tag}",
              "assets": [{"name": f"CE-Decky-{tag}.zip", "browser_download_url": prefix + f"CE-Decky-{tag}.zip"},
                         {"name": "SHA256SUMS", "browser_download_url": prefix + "SHA256SUMS"}]}

    class Api:
        def call(self, method, path):
            return copy.deepcopy(latest)

    sums = f"{SHAS['plugin']}  CE-Decky-{tag}.zip\n".encode()
    monkeypatch.setattr(monitor, "download", lambda url, **k: sums if url.endswith("SHA256SUMS") else BYTES["plugin"])
    verified = []
    monkeypatch.setattr(monitor, "verify_attestation", lambda path, token: verified.append(path.read_bytes()))
    assert monitor.plugin_input(Api(), "token") == plugin()
    assert verified == [BYTES["plugin"]]

    def forged(path, token):
        raise monitor.MonitorError("published CE Decky archive has no valid release attestation")
    monkeypatch.setattr(monitor, "verify_attestation", forged)
    with pytest.raises(monitor.MonitorError, match="attestation"):
        monitor.plugin_input(Api(), "token")


def test_workflow_keeps_candidate_execution_read_only_and_version_matched():
    source = (Path(__file__).parents[1] / ".github/workflows/decky-compatibility.yml").read_text()
    probe_job = source.split("  probe:\n", 1)[1].split("  record:\n", 1)[0]
    assert "contents: read" in probe_job and "contents: write" not in probe_job
    assert "GITHUB_TOKEN" not in probe_job and "github.token" not in probe_job
    assert "ref: ${{ matrix.pair.plugin.tag }}" in probe_job and "fail-fast: false" in probe_job
    assert source.count("persist-credentials: false") == source.count("uses: actions/checkout@")
    record_job = source.split("  record:\n", 1)[1]
    assert "!cancelled()" in record_job and "always()" not in record_job and "github.run_attempt" in record_job
    assert "issues: write" not in source and "cancel-in-progress: false" in source
    # The timer waits for the switch; a run started by hand never does.
    discover_job = source.split("  discover:\n", 1)[1].split("  probe:\n", 1)[0]
    assert "github.event_name == 'workflow_dispatch' || vars.DECKY_COMPAT_MONITOR_ENABLED == 'true'" in discover_job


# The monitor branch ----------------------------------------------------------------

class Repository:
    """Just enough of GitHub's Git Data and contents APIs to hold the monitor branch."""

    def __init__(self):
        self.objects = {}
        self.refs = {}
        self.sent = []

    def _store(self, value):
        sha = hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()
        self.objects[sha] = copy.deepcopy(value)
        return sha

    def files(self, commit=None):
        commit = commit or self.refs[monitor.BRANCH]
        return self.objects[self.objects[commit]["tree"]]["files"]

    def log(self):
        commits, sha = [], self.refs.get(monitor.BRANCH)
        while sha:
            commits.append(self.objects[sha])
            sha = (self.objects[sha]["parents"] or [None])[0]
        return commits

    def send(self, api, method, path, payload):
        self.sent.append((method, path))
        route = path.removeprefix(api.prefix)
        branch = f"/git/refs/heads/{monitor.BRANCH}"
        if method == "GET" and route == f"/git/ref/heads/{monitor.BRANCH}":
            if monitor.BRANCH not in self.refs:
                raise monitor.APIError(404)
            return {"object": {"type": "commit", "sha": self.refs[monitor.BRANCH]}}
        if method == "GET" and route.startswith("/git/commits/"):
            return {"tree": {"sha": self.objects[route.rsplit("/", 1)[1]]["tree"]}}
        if method == "GET" and route.startswith("/contents/"):
            name, ref = route.removeprefix("/contents/").split("?ref=")
            if name not in self.files(ref):
                raise monitor.APIError(404)
            body = self.files(ref)[name].encode()
            return {"type": "file", "encoding": "base64", "size": len(body), "content": base64.b64encode(body).decode()}
        if method == "POST" and route == "/git/trees":
            files = dict(self.objects[payload["base_tree"]]["files"]) if "base_tree" in payload else {}
            files.update({entry["path"]: entry["content"] for entry in payload["tree"]})
            return {"sha": self._store({"files": files})}
        if method == "POST" and route == "/git/commits":
            return {"sha": self._store(payload)}
        if method == "POST" and route == "/git/refs":
            if monitor.BRANCH in self.refs:
                raise monitor.APIError(422)
            self.refs[monitor.BRANCH] = payload["sha"]
            return {}
        if method == "PATCH" and route == branch:
            # GitHub refuses a ref update that is not a fast-forward.
            if self.objects[payload["sha"]]["parents"] != [self.refs[monitor.BRANCH]]:
                raise monitor.APIError(422)
            self.refs[monitor.BRANCH] = payload["sha"]
            return {}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def repository(monkeypatch):
    repo = Repository()
    monkeypatch.setattr(monitor.GitHub, "_send", lambda self, method, path, payload: repo.send(self, method, path, payload))
    return repo


def _api():
    return monitor.GitHub(monitor.PLUGIN_REPO, "fixture-not-a-real-token")


def test_branch_starts_with_no_history_shared_with_main_and_only_its_own_files(repository):
    state = monitor.new_state(NOW)
    _api().create(state, monitor.status_page(state))
    (first,) = repository.log()
    assert first["parents"] == [] and first["message"] == "Start the Decky compatibility monitor"
    files = repository.files()
    assert set(files) == {"README.md", monitor.STATE_PATH, monitor.STATUS_PATH}
    assert "never merge it" in files["README.md"]
    assert monitor.validate_state(monitor.decode(files[monitor.STATE_PATH])) == state


def test_ledger_and_page_move_together_and_only_by_fast_forward(repository):
    api = _api()
    state = monitor.new_state(NOW)
    api.create(state, monitor.status_page(state))
    loaded, head, status = api.load()
    assert loaded == state and status == monitor.status_page(state)
    monitor.discover(loaded, [plugin()], [decky()], NOW)
    api.save(loaded, monitor.status_page(loaded), "first", head)
    files = repository.files()
    assert monitor.decode(files[monitor.STATE_PATH]) == loaded and files[monitor.STATUS_PATH] == monitor.status_page(loaded)
    assert "README.md" in files and len(repository.log()) == 2
    # A writer that read the older ledger is refused, not merged over the newer one.
    with pytest.raises(monitor.APIError) as refused:
        api.save(state, monitor.status_page(state), "stale", head)
    assert refused.value.status == 422
    assert monitor.decode(repository.files()[monitor.STATE_PATH]) == loaded


@pytest.mark.parametrize("method, route, payload", [
    ("PUT", "/contents/decky-compatibility.json", {"branch": monitor.BRANCH}),
    ("PATCH", "/git/refs/heads/main", {"sha": "a" * 40, "force": False}),
    ("PATCH", f"/git/refs/heads/{monitor.BRANCH}", {"sha": "a" * 40, "force": True}),
    ("POST", "/git/refs", {"ref": "refs/heads/main", "sha": "a" * 40}),
    ("POST", "/git/refs", {"ref": "refs/tags/v9.9.9", "sha": "a" * 40}),
    ("POST", "/git/trees", {"tree": [{"path": "main.py", "mode": "100644", "type": "blob", "content": ""}]}),
    ("POST", "/git/trees", {"tree": [{"path": "STATUS.md", "mode": "100755", "type": "blob", "content": ""}]}),
    ("POST", "/git/commits", {"message": "m", "tree": "a" * 40, "parents": ["a" * 40, "b" * 40]}),
    ("DELETE", f"/git/refs/heads/{monitor.BRANCH}", {}),
])
def test_coordinator_refuses_every_write_but_its_own_branch_files(repository, method, route, payload):
    api = _api()
    with pytest.raises(monitor.MonitorError, match="restricted"):
        api.call(method, api.prefix + route, payload)
    assert not repository.sent
    assert monitor.NoAPIRedirect().redirect_request(None, None, 302, "", {}, "https://example.com/") is None


def test_commit_messages_read_as_a_log_of_what_changed():
    before = monitor.new_state(NOW)
    after = copy.deepcopy(before)
    key = monitor.discover(after, [plugin()], [decky()], NOW)[0]["key"]
    assert monitor.change_message(before, after) == "Track Decky v3.4.0 × CE Decky 0.9.30"
    settled = copy.deepcopy(after)
    _settle(settled, key, "BROKEN")
    assert monitor.change_message(after, settled) == "Record BROKEN for Decky v3.4.0 × CE Decky 0.9.30"
    assert monitor.change_message(settled, settled) == "Update the status page"


# Command line ---------------------------------------------------------------------

def _environment(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor.time, "time", lambda: NOW)
    for key, value in {"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_TOKEN": "fixture", "GITHUB_REPOSITORY": monitor.PLUGIN_REPO,
                       "GITHUB_OUTPUT": str(tmp_path / "outputs"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(monitor, "plugin_input", lambda api, token: plugin())
    monkeypatch.setattr(monitor, "current_decky", lambda api: [decky()])


def _command(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["monitor", *argv])
    return monitor.main()


def test_initialize_runs_once_and_never_resets_history(monkeypatch, tmp_path, repository):
    _environment(monkeypatch, tmp_path)
    assert _command(monkeypatch, "initialize") == 0
    head = repository.refs[monitor.BRANCH]
    assert _command(monkeypatch, "initialize") == 2
    assert repository.refs[monitor.BRANCH] == head


def test_complete_cycle_fails_only_for_a_new_problem(monkeypatch, tmp_path, release_tree, repository):
    _environment(monkeypatch, tmp_path)
    assert _command(monkeypatch, "initialize") == 0
    assert _command(monkeypatch, "discover") == 0
    outputs = dict(line.split("=", 1) for line in (tmp_path / "outputs").read_text().splitlines())
    assert outputs["has_work"] == "true"
    (pair,) = json.loads(outputs["matrix"])
    assert "Not tested yet." in repository.files()[monitor.STATUS_PATH]
    _behave(release_tree, **{SHAS["candidate"]: "FAIL"})
    monkeypatch.setenv("PAIR", json.dumps(pair))
    monkeypatch.setattr(monitor, "GitHub", lambda *a: pytest.fail("the probe command must never hold a token"))
    result = tmp_path / "results" / "0" / "result.json"
    assert _command(monkeypatch, "probe", "--release-tree", str(release_tree), "--output", str(result)) == 1
    monkeypatch.undo()
    _environment(monkeypatch, tmp_path)
    monkeypatch.setattr(monitor.GitHub, "_send", lambda self, method, path, payload: repository.send(self, method, path, payload))
    monkeypatch.setenv("PLAN", outputs["matrix"])
    assert _command(monkeypatch, "record", "--results", str(tmp_path / "results")) == 0
    ledger = monitor.decode(repository.files()[monitor.STATE_PATH])
    assert ledger["pairs"][pair["key"]]["state"] == "BROKEN" and "BROKEN" in repository.files()[monitor.STATUS_PATH]
    assert [commit["message"] for commit in reversed(repository.log())] == [
        "Start the Decky compatibility monitor",
        "Track Decky v3.4.0 × CE Decky 0.9.30",
        "Record BROKEN for Decky v3.4.0 × CE Decky 0.9.30",
    ]
    (tmp_path / "outputs").write_text("")
    assert _command(monkeypatch, "discover") == 0
    assert "has_work=false" in (tmp_path / "outputs").read_text()
    assert len(repository.log()) == 3, "a run that changed nothing must not commit"


def test_failed_read_during_discovery_writes_nothing(monkeypatch, tmp_path, repository):
    _environment(monkeypatch, tmp_path)
    assert _command(monkeypatch, "initialize") == 0

    def outage(api):
        raise monitor.APIError(503)
    monkeypatch.setattr(monitor, "current_decky", outage)
    assert _command(monkeypatch, "discover") == 2
    assert len(repository.log()) == 1
