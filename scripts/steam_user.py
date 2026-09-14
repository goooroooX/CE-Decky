#!/usr/bin/env python3
"""The Steam user's home, for the developer helpers that read what it owns.

One copy, because it is a refusal rather than a computation and two copies of a
refusal drift apart quietly. `provider_search_survey.py` and
`provider_corpus_survey.py` both open the Steam library, this plugin's cache and
its listing index, and both used to carry this function verbatim.
"""
from __future__ import annotations

import os
from pathlib import Path


def steam_user_home(explicit: str | None) -> Path:
    """The Steam user's home, from the authority rather than from the login.

    `DECKY_USER_HOME` is what owns the Steam library and this plugin's cache,
    and the account a developer helper happens to run as is not it. Where the
    two differ, deriving these paths from the login silently surveys the wrong
    library and reports the result as evidence, and an empty answer from the
    wrong home reads exactly like an empty answer from the right one. So this
    refuses rather than falling back. `target_plugin_install.py authority`
    reports the exact value as `user_home`.
    """
    raw = explicit or os.environ.get("DECKY_USER_HOME")
    if not raw:
        raise SystemExit(
            "pass --user-home with the exact DECKY_USER_HOME, or pass the exact paths this helper takes; "
            "`python3 scripts/target_plugin_install.py authority` reports it as user_home"
        )
    home = Path(raw).expanduser()
    if not home.is_absolute() or ".." in home.parts or not home.is_dir():
        raise SystemExit(f"--user-home must be an existing absolute path: {raw!r}")
    return home
