from __future__ import annotations

import logging

import pytest

from ce_decky.activity_log import activity_message, configure_dependency_logging, safe_log_text
from ce_decky.operations import OperationRegistry


def test_activity_log_redacts_provider_authority_and_bounds_untrusted_text():
    raw = "https://provider.example/file?lock_hash=secret&other=value\n" + "x" * 1000
    cleaned = safe_log_text(raw)

    assert "secret" not in cleaned
    assert "other=value" not in cleaned
    assert "?<redacted>" in cleaned
    assert "\n" not in cleaned
    assert len(cleaned) <= 512
    message = activity_message("archive", password="do-not-log", password_supplied=True)
    assert "do-not-log" not in message
    assert "password=<redacted>" in message
    assert "password_supplied=true" in message


def test_activity_log_suppresses_dependency_request_transcripts():
    httpx = logging.getLogger("httpx")
    httpcore = logging.getLogger("httpcore")
    prior = (httpx.level, httpcore.level)
    try:
        configure_dependency_logging()
        assert httpx.level == logging.WARNING
        assert httpcore.level == logging.WARNING
    finally:
        httpx.setLevel(prior[0])
        httpcore.setLevel(prior[1])


@pytest.mark.asyncio
async def test_operation_registry_logs_lifecycle_expected_failure_and_safe_unexpected_trace(caplog):
    logger = logging.getLogger("ce-decky-activity-test")
    caplog.set_level(logging.DEBUG, logger=logger.name)
    registry = OperationRegistry(logger)

    def success() -> str:
        return "ok"

    def expected_failure() -> None:
        raise ValueError("bad selection")

    def unexpected_failure() -> None:
        raise TypeError("https://provider.example/file?token=do-not-log")

    assert await registry.run_blocking(success) == "ok"
    with pytest.raises(ValueError, match="bad selection"):
        await registry.run_blocking(expected_failure)
    with pytest.raises(TypeError):
        await registry.run_blocking(unexpected_failure)
    await registry.close()
    with pytest.raises(RuntimeError, match="plugin is unloading"):
        await registry.run_blocking(success)

    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=operation.started" in output
    assert "event=operation.completed" in output
    assert "error_type=ValueError" in output
    assert "error_type=TypeError" in output
    assert "event=operation.rejected" in output
    assert "trace=" in output
    assert "do-not-log" not in output


def test_state_transition_log_writes_only_when_the_observed_state_changes(caplog):
    """The runtime is polled several times a second; the log must not be.

    Logging every poll buries the moment the state changed, and logging nothing
    was what left "the cheat did nothing" with no evidence at all.
    """
    from ce_decky.activity_log import StateTransitionLog

    logger = logging.getLogger("transition-test")
    journal = StateTransitionLog(logger, "runtime.state_changed")
    with caplog.at_level(logging.INFO, logger="transition-test"):
        assert journal.observe(10, ("attached", 4321), attached=True, pid=4321) is True
        assert journal.observe(10, ("attached", 4321), attached=True, pid=4321) is False
        assert journal.observe(10, ("detached", 0), attached=False, pid=0) is True
        # A different key is a different state, not a repeat of this one.
        assert journal.observe(20, ("attached", 4321), attached=True, pid=4321) is True

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert "event=runtime.state_changed" in messages[0]
    assert "first=true" in messages[0]
    assert "first=false" in messages[1]
    assert "pid=4321" in messages[0]


def test_state_transition_log_reports_a_key_again_after_it_is_forgotten(caplog):
    """A deleted profile must not make the next game with that AppID look unchanged."""
    from ce_decky.activity_log import StateTransitionLog

    logger = logging.getLogger("transition-forget-test")
    journal = StateTransitionLog(logger, "runtime.state_changed")
    with caplog.at_level(logging.INFO, logger="transition-forget-test"):
        assert journal.observe(10, ("a",), state="a") is True
        journal.forget(10)
        assert journal.observe(10, ("a",), state="a") is True
