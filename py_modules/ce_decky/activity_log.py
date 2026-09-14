from __future__ import annotations

import logging
import re
import threading
import traceback
import unicodedata
from pathlib import Path


MAX_ACTIVITY_FIELD_CHARS = 512
MAX_ACTIVITY_TRACE_FRAMES = 12
# What says a record is this plugin's, in the record itself.
#
# Decky runs every plugin under one loader, so every plugin's output reaches the
# system journal under the same identifier, and a support bundle collects a
# window of that journal and is attached to public issues. `activity event=` is
# a shape any plugin could write, and one that did would have been published on
# its behalf. This prefix is the ownership statement the filter in
# `journal_records.py` matches on, anchored at the start of the message, so the
# question is answered by the line rather than by a guess about its author.
ACTIVITY_PREFIX = "ce-decky"
_SENSITIVE_FIELD = re.compile(r"(?i)(?:password|passwd|token|secret|cookie|authorization|lock_hash|signature|api_key)")
_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:password|passwd|token|secret|cookie|authorization|lock_hash|signature|sig|api[_-]?key)=)[^&#\s]+"
)
_URL_QUERY = re.compile(r"(https?://[^?\s]+)\?[^\s]+", re.IGNORECASE)
_BIDI_CONTROLS = {
    "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI",
}


def configure_dependency_logging() -> None:
    """Keep dependency request-per-line noise out of the plugin activity log.

    Decky runs this backend in its plugin process. CE Decky emits bounded
    provider summaries and failure phases itself, so httpx's full signed URLs
    are both redundant and liable to expose transient provider credentials.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def safe_log_text(value: object, max_chars: int = MAX_ACTIVITY_FIELD_CHARS) -> str:
    try:
        text = str(value)
    except Exception:
        text = f"<unprintable {type(value).__name__}>"
    text = "".join(
        character
        if unicodedata.category(character) not in {"Cc", "Cf"}
        and unicodedata.bidirectional(character) not in _BIDI_CONTROLS
        else " "
        for character in text
    )
    text = _QUERY_VALUE.sub(r"\1<redacted>", text)
    # Unknown query parameters can still be short-lived signed authority. Keep
    # the useful origin/path while dropping the complete query string.
    text = _URL_QUERY.sub(r"\1?<redacted>", text)
    text = " ".join(text.split())
    return text[:max_chars]


def is_sensitive_field(name: str) -> bool:
    """Whether a field name means the value is a secret, not the value itself.

    Exported because the same rule has to hold for anything that reaches a file
    a user attaches in public, not only for lines this module formats.
    """
    return bool(_SENSITIVE_FIELD.search(name))


def _field(name: str, value: object) -> str:
    if is_sensitive_field(name):
        if isinstance(value, bool):
            return "true" if value else "false"
        return "<redacted>"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    return safe_log_text(value, 192)


def activity_message(event: str, **fields: object) -> str:
    parts = [f"{ACTIVITY_PREFIX} activity event={safe_log_text(event, 64)}"]
    for name in sorted(fields):
        parts.append(f"{safe_log_text(name, 48)}={_field(name, fields[name])}")
    return " ".join(parts)


def log_activity(logger, level: str, event: str, **fields: object) -> None:
    if logger is None:
        return
    method = getattr(logger, level, logger.info)
    method(activity_message(event, **fields))


def log_failure(
    logger,
    event: str,
    exc: BaseException,
    *,
    expected: bool,
    **fields: object,
) -> None:
    if logger is None:
        return
    values = {
        **fields,
        "error_type": type(exc).__name__,
        "error": safe_log_text(exc),
    }
    if expected:
        logger.warning(activity_message(event, **values))
        return
    frames = traceback.extract_tb(exc.__traceback__, limit=MAX_ACTIVITY_TRACE_FRAMES)
    trace = "|".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{safe_log_text(frame.name, 80)}"
        for frame in frames
    )
    logger.error(activity_message(event, **values, trace=trace or "unavailable"))


class StateTransitionLog:
    """Log a repeatedly observed state only when it actually changes.

    The panel polls runtime status continuously, so logging what the bridge
    reports on every read would bury the plugin log in identical lines and lose
    the one moment the state changed. Logging nothing, which is what happened
    before, loses it too: a support bundle could say a Cheat Engine was attached
    now and say nothing about when it stopped being attached, or why.

    Callers pass the fields they want recorded; a stable signature is derived
    from the subset that identifies the state, so a fast-moving counter can be
    reported without making every poll a transition.
    """

    def __init__(self, logger, event: str, *, level: str = "info") -> None:
        self._logger = logger
        self._event = event
        self._level = level
        self._lock = threading.Lock()
        self._seen: dict[object, tuple] = {}

    def observe(self, key: object, signature: tuple, **fields: object) -> bool:
        """Log `fields` if `signature` differs from the last one seen for `key`.

        Returns whether a line was written, which is what a test asserts on.
        """
        with self._lock:
            previous = self._seen.get(key)
            if previous == signature:
                return False
            # Bounded on purpose: the key is an AppID or a table digest, and a
            # process that has seen thousands of them has a different problem.
            if len(self._seen) >= 256 and key not in self._seen:
                self._seen.clear()
            self._seen[key] = signature
            first = previous is None
        log_activity(self._logger, self._level, self._event, first=first, **fields)
        return True

    def forget(self, key: object) -> None:
        with self._lock:
            self._seen.pop(key, None)
