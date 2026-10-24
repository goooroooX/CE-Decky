"""Every test that takes a permission away says so, so it is skipped where that is impossible."""
from __future__ import annotations

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent


def _takes_every_permission_away(node: ast.AST) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "chmod"
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == 0
        for call in ast.walk(node)
    )


def _marked(function: ast.FunctionDef) -> bool:
    return any("denies_access" in ast.unparse(decorator) for decorator in function.decorator_list)


def test_every_test_that_denies_access_is_marked_for_it():
    unmarked = [
        f"{path.name}::{node.name}"
        for path in sorted(TESTS.glob("test_*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        and _takes_every_permission_away(node) and not _marked(node)
    ]
    assert unmarked == []


def test_the_guard_finds_what_it_is_for():
    marked, unmarked, ordinary = ast.parse(
        "@pytest.mark.denies_access\ndef test_a(p):\n    p.chmod(0o000)\n\n"
        "def test_b(p):\n    p.chmod(0)\n\n"
        "def test_c(p):\n    p.chmod(0o755)\n"
    ).body
    assert _takes_every_permission_away(marked) and _marked(marked)
    assert _takes_every_permission_away(unmarked) and not _marked(unmarked)
    assert not _takes_every_permission_away(ordinary)
