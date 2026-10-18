import copy
import json
from pathlib import Path
import sys

import pytest

from scripts import decky_compat_monitor as monitor


NOW = 1_000_000
RUN = {"run_id": "100", "attempt": "1"}
PLUGIN = {"version": "0.9.30", "url": f"https://github.com/{monitor.PLUGIN_REPO}/releases/download/v0.9.30/CE-Decky-v0.9.30.zip", "sha256": "b" * 64}


def release(tag="v3.3.1", *, prerelease=False, sha="a" * 64):
    return {"id": 1, "tag_name": tag, "draft": False, "prerelease": prerelease, "assets": [{
        "name": "PluginLoader", "size": 16, "digest": "sha256:" + sha,
        "browser_download_url": f"https://github.com/{monitor.UPSTREAM}/releases/download/{tag}/PluginLoader"}]}


class Store:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or monitor.new_state([], NOW))
        self.revision = 0
        self.writes = []
        self.completed = True

    def load(self):
        return copy.deepcopy(self.state), self.revision

    def save(self, state, revision=None):
        if revision != self.revision:
            raise monitor.MonitorError("ledger conflict")
        monitor.validate_state(state)
        self.state = copy.deepcopy(state)
        self.revision += 1
        self.writes.append(copy.deepcopy(state))
        return self.revision

    def finished(self, claim):
        return self.completed


def setup_feed(monkeypatch, rows):
    monkeypatch.setattr(monitor, "releases", lambda api: copy.deepcopy(rows))
    monkeypatch.setattr(monitor, "plugin_input", lambda api: copy.deepcopy(PLUGIN))


def test_initialization_baselines_all_existing_channels_without_work():
    state = monitor.new_state([release(), release("v3.4.0-pre1", prerelease=True)], NOW)
    assert all(row["state"] == "BASELINE" for row in state["versions"].values())
    assert not monitor.claim_pending(state, RUN, PLUGIN)
    assert not monitor.reserve_attention(state, RUN)


def test_72_hour_deadline_is_claimed_before_feed_and_execution(monkeypatch):
    api = Store()
    setup_feed(monkeypatch, [release(), release("v3.4.0-pre1", prerelease=True)])
    assert not monitor.prepare(api, RUN, NOW + monitor.INTERVAL - 1)["candidates"]
    assert not api.writes
    plan = monitor.prepare(api, RUN, NOW + monitor.INTERVAL)
    assert len(plan["candidates"]) == 2
    assert api.writes[0]["next_discovery"] == NOW + 2 * monitor.INTERVAL
    assert not api.writes[0]["versions"]
    assert api.state["versions"]["v3.3.1"]["claim"] == RUN
    assert not monitor.prepare(api, RUN, NOW + monitor.INTERVAL)["candidates"]


def test_promotion_recreation_and_asset_changes_never_repeat_a_tag(monkeypatch):
    state = monitor.new_state([], NOW)
    first = release("v3.4.0-pre1", prerelease=True)
    monitor.discover(state, [first])
    monitor.claim_pending(state, RUN, PLUGIN)
    monitor.settle(state, RUN, [{"tag": first["tag_name"], "result": {"status": "FAIL", "error": "fixture"}}])
    assert monitor.reserve_attention(state, RUN) == [first["tag_name"]]
    replaced = release(first["tag_name"], prerelease=False, sha="c" * 64)
    replaced["id"] = 999
    monitor.discover(state, [replaced])
    assert not monitor.claim_pending(state, RUN, {**PLUGIN, "version": "0.9.31"})
    assert not monitor.reserve_attention(state, {"run_id": "101", "attempt": "1"})
    assert state["versions"][first["tag_name"]]["asset_changed"]
    assert state["versions"][first["tag_name"]]["asset"]["sha256"] == "a" * 64
    monitor.discover(state, [release("v3.4.0")])
    assert [row["tag"] for row in monitor.claim_pending(state, RUN, PLUGIN)] == ["v3.4.0"]


def test_manual_rerun_of_probe_does_not_download_or_execute(monkeypatch, tmp_path):
    monkeypatch.setattr(monitor, "download", lambda *a, **k: pytest.fail("replayed download"))
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release()])
    plan = {"schema": 1, "identity": RUN, "candidates": monitor.claim_pending(state, RUN, PLUGIN)}
    assert monitor.execute_plan(plan, tmp_path, {**RUN, "attempt": "2"}) == []


def test_interrupted_original_attempt_is_not_reexecuted(monkeypatch):
    api = Store()
    setup_feed(monkeypatch, [release()])
    monitor.prepare(api, RUN, NOW + monitor.INTERVAL)
    second = {"run_id": "101", "attempt": "1"}
    api.completed = False
    assert not monitor.prepare(api, second, NOW + monitor.INTERVAL + 1)["candidates"]
    assert api.state["versions"]["v3.3.1"]["state"] == "CLAIMED"
    api.completed = True
    assert not monitor.prepare(api, second, NOW + monitor.INTERVAL + 2)["candidates"]
    row = api.state["versions"]["v3.3.1"]
    assert row["state"] == "INCOMPLETE"
    assert monitor.reserve_attention(api.state, second) == ["v3.3.1"]
    assert not monitor.reserve_attention(api.state, {**second, "attempt": "2"})


def test_result_saved_then_reporting_crash_cannot_repeat_alert_or_probe():
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release()])
    monitor.claim_pending(state, RUN, PLUGIN)
    monitor.settle(state, RUN, [{"tag": "v3.3.1", "result": {"status": "FAIL"}}])
    assert monitor.reserve_attention(state, RUN) == ["v3.3.1"]
    restored = copy.deepcopy(state)
    assert not monitor.reserve_attention(restored, {**RUN, "attempt": "2"})
    assert not monitor.claim_pending(restored, RUN, PLUGIN)


def test_missing_result_is_incomplete_and_cannot_be_reported_as_pass():
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release()])
    monitor.claim_pending(state, RUN, PLUGIN)
    monitor.settle(state, RUN, [])
    assert state["versions"]["v3.3.1"]["state"] == "INCOMPLETE"
    assert monitor.reserve_attention(state, RUN)


def test_pass_requires_exact_claimed_bytes_and_never_requests_an_alert():
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release()])
    monitor.claim_pending(state, RUN, PLUGIN)
    result = {"status": "PASS", "decky_sha256": "a" * 64, "plugin_sha256": "c" * 64,
              "checks": sorted(monitor.REQUIRED_CHECKS), "frozen": True, "plugin_version": PLUGIN["version"]}
    with pytest.raises(monitor.MonitorError, match="identities"):
        monitor.settle(state, RUN, [{"tag": "v3.3.1", "result": result}])
    result["plugin_sha256"] = "b" * 64
    monitor.settle(state, RUN, [{"tag": "v3.3.1", "result": result}])
    assert not monitor.reserve_attention(state, RUN)


def test_batch_limit_preserves_pending_versions():
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release(f"v3.3.{index}") for index in range(monitor.MAX_BATCH + 2)])
    assert len(monitor.claim_pending(state, RUN, PLUGIN)) == monitor.MAX_BATCH
    assert sum(row["state"] == "PENDING" for row in state["versions"].values()) == 2


def test_conflicting_write_never_returns_an_executable_plan(monkeypatch):
    api = Store()
    setup_feed(monkeypatch, [release()])
    def conflict(*args):
        raise monitor.MonitorError("ledger conflict")
    api.save = conflict
    with pytest.raises(monitor.MonitorError, match="conflict"):
        monitor.prepare(api, RUN, NOW + monitor.INTERVAL)
    assert not api.state["versions"]


@pytest.mark.parametrize("bad", [{}, None, {"schema": 1}, {"schema": True}])
def test_unreadable_or_missing_ledger_is_not_a_new_baseline(bad):
    with pytest.raises(monitor.MonitorError):
        monitor.validate_state(bad)


def test_contents_update_is_scoped_to_the_ledger_and_uses_expected_sha():
    api = monitor.GitHub(monitor.PLUGIN_REPO, "fixture-not-a-real-token")
    calls = []
    api.call = lambda method, path, payload: (calls.append((method, path, payload)) or {"content": {"sha": "next"}})
    assert api.save(monitor.new_state([], NOW), "expected") == "next"
    method, path, payload = calls[0]
    assert method == "PUT" and path.endswith("/contents/" + monitor.STATE_PATH)
    assert payload["branch"] == monitor.BRANCH and payload["sha"] == "expected"
    assert "token" not in str(payload)


def test_missing_published_plugin_and_missing_asset_are_incomplete(monkeypatch, tmp_path):
    api = Store()
    setup_feed(monkeypatch, [release()])
    monkeypatch.setattr(monitor, "plugin_input", lambda api: (_ for _ in ()).throw(ValueError("no release")))
    plan = monitor.prepare(api, RUN, NOW + monitor.INTERVAL)
    assert "error" in plan["candidates"][0]["plugin"]
    result = monitor.execute_plan(plan, tmp_path, RUN)
    assert result[0]["result"]["status"] == "INCOMPLETE"


def test_workflows_keep_candidate_execution_read_only_and_fixed_artifact_identity():
    source = (Path(__file__).parents[1] / ".github/workflows/decky-compatibility.yml").read_text()
    probe_job = source.split("  probe:\n", 1)[1].split("  finalize:\n", 1)[0]
    assert "contents: read" in probe_job and "contents: write" not in probe_job
    assert "GITHUB_TOKEN:" not in probe_job and "persist-credentials: false" in probe_job
    assert "needs.prepare.outputs.attempt" in probe_job
    assert "merge-multiple" not in source
    assert "issues: write" not in source and "cancel-in-progress: false" in source


def test_complete_cli_cycle_saves_report_before_failure_and_reruns_stay_silent(monkeypatch, tmp_path):
    api = Store()
    setup_feed(monkeypatch, [release()])
    monkeypatch.setattr(monitor, "GitHub", lambda *args: api)
    monkeypatch.setattr(monitor.time, "time", lambda: NOW + monitor.INTERVAL)
    for key, value in {"GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "d" * 40,
                       "GITHUB_OUTPUT": str(tmp_path / "outputs"), "GITHUB_STEP_SUMMARY": str(tmp_path / "step-summary")}.items():
        monkeypatch.setenv(key, value)
    plan = tmp_path / "plan.json"
    results = tmp_path / "results.json"
    report = tmp_path / "report.md"

    def command(name):
        monkeypatch.setattr(sys, "argv", ["monitor", name, "--plan", str(plan), "--results", str(results),
                                         "--output", str(plan if name == "prepare" else report)])
        return monitor.main()

    assert command("prepare") == 0
    assert json.loads(plan.read_text())["candidates"][0]["probe_revision"] == "d" * 40
    results.write_text(json.dumps([{"tag": "v3.3.1", "result": {"status": "FAIL", "error": "fixture import failed"}}]))
    assert command("finalize") == 0
    assert "fixture import failed" in report.read_text() and "Investigate failing behavior" in report.read_text()
    assert command("status") == 1
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert command("finalize") == 0  # old producer output, not a new result
    assert command("status") == 0
    monkeypatch.setenv("GITHUB_RUN_ID", "101")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(monitor.time, "time", lambda: NOW + 2 * monitor.INTERVAL)
    assert command("prepare") == 0
    assert not json.loads(plan.read_text())["candidates"]
    assert (tmp_path / "outputs").read_text().endswith("has_finalize=false\n")
    assert api.state["versions"]["v3.3.1"]["state"] == "FAIL"


def test_duplicate_execution_plan_is_refused_before_any_candidate_runs(monkeypatch, tmp_path):
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release()])
    candidates = monitor.claim_pending(state, RUN, PLUGIN)
    plan = {"schema": 1, "identity": RUN, "candidates": candidates + candidates}
    monkeypatch.setattr(monitor, "download", lambda *args, **kwargs: pytest.fail("invalid plan contacted network"))
    with pytest.raises(monitor.MonitorError, match="repeats"):
        monitor.execute_plan(plan, tmp_path, RUN)


@pytest.mark.parametrize("text", ['{"versions": {}, "versions": {}}', '{"value": NaN}'])
def test_ledger_decoder_never_silently_discards_history(text):
    with pytest.raises(ValueError):
        monitor.decode(text)


def test_coordinator_cannot_write_production_files_or_follow_token_redirects():
    api = monitor.GitHub(monitor.PLUGIN_REPO, "fixture-not-a-real-token")
    with pytest.raises(monitor.MonitorError, match="restricted"):
        api.call("PUT", api.prefix + "/contents/main.py", {"branch": "main"})
    assert monitor.NoAPIRedirect().redirect_request(None, None, 302, "", {}, "https://example.com/") is None


def test_pass_without_actual_checks_is_refused():
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release()])
    monitor.claim_pending(state, RUN, PLUGIN)
    result = {"status": "PASS", "decky_sha256": "a" * 64, "plugin_sha256": "b" * 64}
    with pytest.raises(monitor.MonitorError, match="checks"):
        monitor.settle(state, RUN, [{"tag": "v3.3.1", "result": result}])


def test_mixed_batch_retains_each_outcome_and_original_attempt_evidence():
    state = monitor.new_state([], NOW)
    monitor.discover(state, [release(), release("v3.4.0-pre1", prerelease=True), release("v3.4.0")])
    monitor.claim_pending(state, RUN, PLUGIN, probe_revision="d" * 40)
    passed = {"status": "PASS", "decky_sha256": "a" * 64, "plugin_sha256": "b" * 64,
              "checks": sorted(monitor.REQUIRED_CHECKS), "frozen": True, "plugin_version": PLUGIN["version"]}
    monitor.settle(state, RUN, [{"tag": "v3.3.1", "result": passed},
                              {"tag": "v3.4.0-pre1", "result": {"status": "FAIL", "error": "broken import"}}])
    monitor.discover(state, [release(sha="c" * 64)])
    report = monitor.summary(state)
    assert "| v3.3.1 | PASS |" in report and "| v3.4.0 | INCOMPLETE |" in report
    assert '"asset_changed_since_claim": true' in report
    assert '"sha256": "' + "a" * 64 + '"' in report
    assert "/actions/runs/100/attempts/1" in report and "d" * 40 in report
    assert monitor.reserve_attention(state, RUN) == ["v3.4.0-pre1", "v3.4.0"]
    assert not monitor.reserve_attention(state, {**RUN, "attempt": "2"})
