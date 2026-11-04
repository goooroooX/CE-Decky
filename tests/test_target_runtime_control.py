from copy import deepcopy

import pytest

from scripts import target_runtime_control as tool

SHA = "a" * 64


class Backend:
    def __init__(self, *, negative=False, wrong_record=False, changed_session=False):
        self.generation = 1
        self.pending = None
        self.waits = 0
        self.active = False
        self.results = []
        self.written = []
        self.negative, self.wrong_record, self.changed_session = negative, wrong_record, changed_session

    def envelope(self):
        identity = {"session_id": "changed" if self.changed_session and self.written else "one",
                    "table_sha256": SHA, "ce_sha256": "c" * 64, "descriptor_sha256": "d" * 64}
        status = {"app_id": 42, "attached": True, "opened_process_id": 12, "results": deepcopy(self.results), **identity}
        return {"connected": True, "session_current": True, "next_generation": self.generation,
                "prepared": {"app_id": 42, **identity}, "status": status}

    def __call__(self, method, args):
        if method == "get_runtime_status":
            if self.pending and self.waits:
                self.waits -= 1
            elif self.pending:
                for action in self.pending:
                    if action["kind"] == "set_active" and not self.negative:
                        self.active = action["value"] == "1"
                    self.results.append({"generation": action["generation"],
                        "record_id": action["record_id"] + int(self.wrong_record),
                        "ok": not self.negative, "active": self.active, "value": "2",
                        "error_code": "mutation_failed" if self.negative else None})
                self.pending = None
            return self.envelope()
        assert method == "write_runtime_commands"
        assert self.pending is None, "a query raced the outstanding mutation"
        self.pending = deepcopy(args[1])
        self.written.append(self.pending)
        self.waits = 2
        self.generation += len(self.pending)
        return {"ok": True, "count": len(self.pending), "next_generation": self.generation}


def test_control_waits_for_exact_ack_before_query_and_never_writes_profiles():
    backend = Backend()
    answer = tool.control(42, SHA, [{"record_id": 7, "kind": "set_active", "value": "1"}], backend, sleep=lambda _: None)
    assert answer["ok"] and answer["readback"]["results"][0]["active"]
    assert [batch[0]["kind"] for batch in backend.written] == ["set_active", "query"]
    assert answer["acknowledged"][0]["generation"] == 1
    assert answer["session_id"] == "one"


def test_busy_activation_can_temporarily_age_heartbeat_without_retrying_mutation():
    backend = Backend()
    stale_polls = []
    def rpc(method, args):
        answer = backend(method, args)
        if method == "get_runtime_status" and len(backend.written) == 1 and len(stale_polls) < 4:
            answer["connected"] = False
            stale_polls.append(True)
        return answer
    report = tool.control(42, SHA, [{"record_id": 7, "kind": "set_active", "value": "1"}], rpc, sleep=lambda _: None)
    assert report["ok"] and len(stale_polls) == 4
    assert [batch[0]["kind"] for batch in backend.written] == ["set_active", "query"]


def test_wrong_table_refuses_before_mutation():
    backend = Backend()
    with pytest.raises(tool.Refused, match="table SHA"):
        tool.control(42, "b" * 64, [{"record_id": 7, "kind": "set_active", "value": "1"}], backend)
    assert backend.written == []


def test_negative_ack_is_retained_and_not_retried():
    backend = Backend(negative=True)
    answer = tool.control(42, SHA, [{"record_id": 7, "kind": "set_active", "value": "1"}], backend, sleep=lambda _: None)
    assert not answer["ok"] and answer["acknowledged"][0]["error_code"] == "mutation_failed"
    assert answer["readback"] is None and len(backend.written) == 1


def test_timeout_preserves_partial_acks_without_assuming_other_actions_did_not_run(monkeypatch):
    backend = Backend()
    def rpc(method, args):
        answer = backend(method, args)
        if method == "get_runtime_status":
            answer["status"]["results"] = [r for r in answer["status"]["results"] if r["record_id"] != 8]
        return answer
    monkeypatch.setattr(tool.target_runtime_query, "ACK_POLL_ATTEMPTS", 5)
    progress = {}
    actions = [{"record_id": i, "kind": "set_active", "value": "1"} for i in (7, 8)]
    with pytest.raises(tool.Refused, match="ACK timed out"):
        tool.control(42, SHA, actions, rpc, sleep=lambda _: None, progress=progress)
    assert progress["phase"] == "awaiting_mutation_ack" and progress["mutation_may_have_happened"]
    assert [r["record_id"] for r in progress["observed_acknowledgements"]] == [7]
    assert len(backend.written) == 1 and len(backend.written[0]) == 2


@pytest.mark.parametrize("options", [{"wrong_record": True}, {"changed_session": True}])
def test_changed_session_or_wrong_record_never_earns_success(options):
    backend = Backend(**options)
    with pytest.raises(tool.Refused):
        tool.control(42, SHA, [{"record_id": 7, "kind": "set_active", "value": "1"}], backend, sleep=lambda _: None)
    assert len(backend.written) == 1


@pytest.mark.parametrize("action", [
    {"record_id": True, "kind": "set_active", "value": "1"},
    {"record_id": 7, "kind": "set_active", "value": "true"},
    {"record_id": 7, "kind": "eval", "value": "print(1)"},
])
def test_only_bounded_explicit_record_mutations_are_accepted(action):
    backend = Backend()
    with pytest.raises(tool.Refused):
        tool.control(42, SHA, [action], backend)
    assert backend.written == []


def test_existing_output_refuses_before_any_rpc(tmp_path, monkeypatch):
    output = tmp_path / "result.json"
    output.write_text("keep")
    monkeypatch.setattr(tool.host_platform, "require", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(tool.target_plugin_rpc, "call", lambda *a: calls.append(a))
    result = tool.main(["--appid", "42", "--table-sha", SHA, "--commands",
        '[{"kind":"set_active","record_id":7,"value":"1"}]', "--out", str(output)])
    assert result == 2 and calls == [] and output.read_text() == "keep"


def test_changed_attached_pid_during_readback_never_earns_success():
    backend = Backend()
    def rpc(method, args):
        answer = backend(method, args)
        if method == "get_runtime_status" and len(backend.written) == 2:
            answer["status"]["opened_process_id"] = 99
        return answer
    with pytest.raises(tool.Refused, match="target changed"):
        tool.control(42, SHA, [{"record_id": 7, "kind": "set_active", "value": "1"}], rpc, sleep=lambda _: None)
    assert [batch[0]["kind"] for batch in backend.written] == ["set_active", "query"]


def test_readback_failure_preserves_successful_mutation_ack(tmp_path, monkeypatch):
    backend = Backend()
    def rpc(method, args, *rest):
        if method == "write_runtime_commands" and backend.written:
            raise RuntimeError("query transport failed")
        return backend(method, args)
    monkeypatch.setattr(tool.host_platform, "require", lambda *a, **k: None)
    monkeypatch.setattr(tool.target_plugin_rpc, "call", rpc)
    monkeypatch.setattr(tool.target_runtime_query, "ACK_POLL_INTERVAL_S", 0)
    out = tmp_path / "result.json"
    assert tool.main(["--appid", "42", "--table-sha", SHA, "--commands",
        '[{"kind":"set_active","record_id":7,"value":"1"}]', "--out", str(out)]) == 2
    import json
    report = json.loads(out.read_text())
    assert report["phase"] == "readback" and report["mutation_may_have_happened"]
    assert report["acknowledged"][0]["ok"] is True
    assert report["commands"][0]["generation"] == 1 and report["session_id"] == "one"


def test_cli_preflight_failure_does_not_claim_a_mutation(tmp_path, monkeypatch):
    backend = Backend()
    monkeypatch.setattr(tool.host_platform, "require", lambda *a, **k: None)
    monkeypatch.setattr(tool.target_plugin_rpc, "call", lambda method, args, *a: backend(method, args))
    out = tmp_path / "result.json"
    assert tool.main(["--appid", "42", "--table-sha", "b" * 64, "--commands",
        '[{"kind":"set_active","record_id":7,"value":"1"}]', "--out", str(out)]) == 2
    import json
    report = json.loads(out.read_text())
    assert report["phase"] == "preflight" and report["mutation_may_have_happened"] is False
    assert backend.written == []
