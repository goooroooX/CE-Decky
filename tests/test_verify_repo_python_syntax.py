"""Keep every module parseable by the interpreter this plugin actually runs on.

Decky's loader ships CPython 3.11.7 and CI pins 3.11 to match it, while a
development device runs whatever SteamOS currently has. Syntax accepted only by
the newer one does not fail as one bad test: the file will not import at all, so
the whole module goes with it, and the first place that shows is CI or the
target rather than the machine the change was written on.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


def test_the_repository_parses_on_the_runtime_interpreter():
    verify_repo._verify_python_runtime_syntax()


def _module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    (tmp_path / "py_modules").mkdir(parents=True)
    (tmp_path / "py_modules" / "sample.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)


def test_an_escape_inside_an_f_string_field_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # PEP 701 allows this from 3.12. Before that it is a SyntaxError, and this
    # is the exact shape that took a whole test module out of the backend gate.
    _module(tmp_path, monkeypatch, 'value = f"<a>{chr(0x4e2d) * 2}</a>"\nwide = f"{\'\\u4e2d\'}"\n')

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_python_runtime_syntax()

    assert "py_modules/sample.py:2" in str(refused.value)


def test_a_nested_quote_of_the_outer_kind_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _module(tmp_path, monkeypatch, 'row = {}\nname = f"table {row["name"]}"\n')

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_python_runtime_syntax()

    assert "py_modules/sample.py:2" in str(refused.value)


def test_grammar_newer_than_the_runtime_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # PEP 695 type parameters, which are 3.12 grammar rather than tokenizing.
    _module(tmp_path, monkeypatch, "def first[T](values: list[T]) -> T:\n    return values[0]\n")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_python_runtime_syntax()

    assert "py_modules/sample.py" in str(refused.value)


def test_the_scan_stands_aside_on_the_runtime_interpreter_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Before 3.12 an f-string is one token, so the tokens this reads do not
    # exist there. That interpreter refuses these forms itself, and the parse
    # this sits beside reports it with a line number, so the scan has nothing
    # to add and must not fail trying: reaching for a token type it does not
    # have took repository QA and the whole backend suite down with it.
    import tokenize

    monkeypatch.delattr(tokenize, "FSTRING_START", raising=False)

    # A source only 3.12 can parse. The scan says nothing about it rather than
    # raising, because on that interpreter the parse beside it is what refuses
    # it and what carries the line number.
    assert verify_repo._fstring_is_312_only('wide = f"{\'\\u4e2d\'}"\n') == []

    # And the tree itself still goes through, which is the run this broke.
    _module(tmp_path, monkeypatch, 'name = "table"\nrow = f"{name}"\n')
    verify_repo._verify_python_runtime_syntax()


def test_an_f_string_the_runtime_accepts_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _module(
        tmp_path,
        monkeypatch,
        "row = {}\n"
        'name = f"table {row[\'name\']}"\n'
        'lines = f"first\\nsecond {len(name)}"\n'
        'nested = f"{f\'{len(name)}\'}"\n',
    )

    verify_repo._verify_python_runtime_syntax()
