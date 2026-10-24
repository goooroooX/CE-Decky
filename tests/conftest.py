from __future__ import annotations

import os
from pathlib import Path

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


@pytest.fixture(scope="session")
def _access_can_be_denied(tmp_path_factory: pytest.TempPathFactory) -> bool:
    """Whether a directory with no permissions is refused to this process.

    Asked of the process rather than read off its uid: root is refused nothing,
    and so is any process holding `CAP_DAC_OVERRIDE`, while root inside a user
    namespace without it is refused like anyone else.
    """
    closed = tmp_path_factory.mktemp("access-probe") / "closed"
    closed.mkdir()
    closed.chmod(0)
    try:
        os.listdir(closed)
    except PermissionError:
        return True
    finally:
        closed.chmod(0o700)
    return False


@pytest.fixture(autouse=True)
def _skip_where_access_cannot_be_denied(request: pytest.FixtureRequest) -> None:
    """Skip a `denies_access` test where its denial would be no denial at all.

    Run anyway, such a test fails on the refusal it never got, which reads as a
    regression on a tree that has none. A skip says the case was not checked
    here, which is the truth; CI runs as an ordinary user and checks it.
    """
    if request.node.get_closest_marker("denies_access") is None:
        return
    if not request.getfixturevalue("_access_can_be_denied"):
        pytest.skip("this process is refused nothing, as root is, so a permission cannot be taken away here")
