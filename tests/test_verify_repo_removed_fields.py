"""Keep a field the public-release cleanup deleted out of every fixture."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


# The removed field, read from the guard rather than typed out: writing a key
# position here would make this module fail its own first check.
FIELD = verify_repo.REMOVED_API_FIELDS[0]


def test_the_repository_emits_no_removed_api_field():
    verify_repo._verify_removed_api_fields()


def test_a_test_fixture_that_still_emits_a_removed_field_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # This is the shape that got past everything: the production interface lost
    # the field, the fixtures kept sending it, and `tsconfig.json` covers `src`
    # and not `tests`, so the ordinary type-check never read the file.
    source = tmp_path / "tests" / "example.test.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        'const capability = { schema: 2, %s: "C03", modes: ["attached"] };\n' % FIELD,
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_removed_api_fields()
    assert f"tests/example.test.ts:1={FIELD}" in str(refused.value)

    source.write_text('const capability = { schema: 2, modes: ["attached"] };\n', encoding="utf-8")
    verify_repo._verify_removed_api_fields()


def test_a_production_payload_that_writes_one_back_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "py_modules" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'def view():\n    return {"schema": 3, "%s": "C01"}\n' % FIELD,
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_removed_api_fields()
    assert f"py_modules/example.py:2={FIELD}" in str(refused.value)


def test_naming_the_removed_field_in_prose_or_an_assertion_is_not_a_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # The check that the backend stopped sending it has to name it, and so does
    # the comment explaining why. Only a key position is a payload.
    source = tmp_path / "tests" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        f'# The managed capability no longer carries a {FIELD}.\n'
        'def test_managed(managed):\n'
        f'    assert "{FIELD}" not in managed\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    verify_repo._verify_removed_api_fields()
