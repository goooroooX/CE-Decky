from __future__ import annotations

import pytest

from scripts import target_runtime_query
from scripts.target_runtime_query import QueryRefused, query

IDENTITY = {"session_id": "s1", "table_sha256": "t" * 64, "ce_sha256": "c" * 64, "descriptor_sha256": "d" * 64}


def _envelope(results=(), **overrides):
    envelope = {
        "connected": True, "session_current": True, "next_generation": 7,
        "prepared": {"app_id": 10, **IDENTITY},
        "status": {"app_id": 10, **IDENTITY, "results": list(results)},
    }
    envelope.update(overrides)
    return envelope


class FakeBackend:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, args):
        self.calls.append((method, args))
        if method == "write_runtime_commands":
            commands = args[1]
            return {"ok": True, "count": len(commands), "next_generation": commands[-1]["generation"] + 1}
        return self.answers.pop(0)


def test_it_sends_only_queries_and_returns_each_records_answer():
    acknowledged = [
        {"generation": 7, "record_id": 51, "ok": True, "active": False, "value": "??", "error_code": None, "error": None},
        {"generation": 8, "record_id": 19, "ok": True, "active": False, "value": "999", "error_code": None, "error": None},
    ]
    backend = FakeBackend([_envelope(), _envelope(), _envelope(acknowledged)])

    report = query(10, [51, 19], backend, sleep=lambda _: None)

    written = [args[1] for method, args in backend.calls if method == "write_runtime_commands"]
    assert written == [[
        {"generation": 7, "kind": "query", "record_id": 51},
        {"generation": 8, "kind": "query", "record_id": 19},
    ]]
    assert [(item["record_id"], item["value"]) for item in report["results"]] == [(51, "??"), (19, "999")]
    assert report["session_id"] == "s1"


def test_it_refuses_a_disconnected_or_mismatched_session_before_writing_anything():
    for envelope in (
        _envelope(connected=False),
        _envelope(session_current=False),
        {**_envelope(), "status": {**_envelope()["status"], "session_id": "other"}},
    ):
        backend = FakeBackend([envelope])
        with pytest.raises(QueryRefused):
            query(10, [51], backend, sleep=lambda _: None)
        assert all(method != "write_runtime_commands" for method, _ in backend.calls)


def test_it_refuses_an_answer_from_another_session_or_for_another_record():
    changed = {**_envelope(), "prepared": {"app_id": 10, **IDENTITY, "session_id": "s2"}}
    with pytest.raises(QueryRefused, match="session changed"):
        query(10, [51], FakeBackend([_envelope(), changed]), sleep=lambda _: None)

    wrong = [{"generation": 7, "record_id": 52, "ok": True, "active": False, "value": "0"}]
    with pytest.raises(QueryRefused, match="another record"):
        query(10, [51], FakeBackend([_envelope(), _envelope(wrong)]), sleep=lambda _: None)


def test_it_gives_up_after_the_panels_own_wait():
    backend = FakeBackend([_envelope()] * (target_runtime_query.ACK_POLL_ATTEMPTS + 1))
    with pytest.raises(QueryRefused, match="not acknowledged"):
        query(10, [51], backend, sleep=lambda _: None)


def test_it_bounds_and_deduplicates_the_records_it_is_asked_for():
    for records in ([], [1, 1], list(range(target_runtime_query.MAX_RECORDS + 1))):
        with pytest.raises(QueryRefused, match="distinct record IDs"):
            query(10, records, FakeBackend([]), sleep=lambda _: None)
