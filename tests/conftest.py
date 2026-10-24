from __future__ import annotations

import pytest

from ce_decky import managed_ce


@pytest.fixture(autouse=True)
def _no_system_compatibility_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Proton discovery to the test's own home.

    Production also reads the system-wide `compatibilitytools.d` directories,
    and a developer machine with a distribution Proton installed would hand
    every test that discovers tools one it never created. A test about those
    directories points this at its own.
    """
    monkeypatch.setattr(managed_ce, "SYSTEM_COMPAT_TOOL_ROOTS", ())
