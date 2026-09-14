from pathlib import Path


def _lua_string_literals(source: str) -> list[str]:
    """Extract single- and double-quoted Lua string literals from the bridge."""
    literals: list[str] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character in "\"'":
            quote = character
            index += 1
            start = index
            while index < len(source) and source[index] != quote:
                if source[index] == chr(92):
                    index += 1
                index += 1
            literals.append(source[start:index])
        elif source.startswith("--", index):
            end = source.find("\n", index)
            index = len(source) if end == -1 else end
        index += 1
    return literals


def _outside_character_classes(literal: str) -> str:
    """Return the literal with `[...]` spans removed.

    Inside a Lua character class `|`, `*` and `?` are ordinary characters.
    """
    out: list[str] = []
    depth = 0
    for character in literal:
        if character == "[":
            depth += 1
        elif character == "]" and depth:
            depth -= 1
        elif depth == 0:
            out.append(character)
    return "".join(out)


def test_bridge_patterns_avoid_syntax_lua_patterns_do_not_support():
    """Lua patterns are not regular expressions.

    Regex-only syntax does not raise in Lua: `^(0|[1-9][0-9]*)$` silently
    matched the literal text `0|<digits>` and rejected every real integer, which
    made the whole descriptor/control protocol unusable while every static and
    Python-side test still passed. Keep that class of defect out.
    """
    bridge = (Path(__file__).parents[1] / "py_modules" / "ce_decky" / "ce_decky_bridge.lua").read_text(encoding="utf-8")
    for literal in _lua_string_literals(bridge):
        assert "%z" not in literal, f"%z is not a documented class in current Lua: {literal!r}"
        outside = _outside_character_classes(literal)
        assert "|" not in outside, f"alternation is not a Lua pattern: {literal!r}"
        for token in (chr(92) + "d", chr(92) + "w", chr(92) + "s", "*?", "+?", "(?"):
            assert token not in outside, f"regex-only syntax in a Lua string: {literal!r}"


def test_frontend_startup_wait_covers_the_bridge_per_action_budget():
    """The panel must not call a startup failed while the bridge is still within
    its own declared budget for one action.

    Auto-load waited a fixed total that was shorter than a single action's worst
    case, so it reported "the saved cheats had not been applied yet" for a
    startup that went on to apply them. These two numbers live in different
    languages, so nothing but this keeps them in step.
    """
    import re

    root = Path(__file__).parents[1]
    bridge = (root / "py_modules" / "ce_decky" / "ce_decky_bridge.lua").read_text(encoding="utf-8")
    panel = (root / "src" / "index.tsx").read_text(encoding="utf-8")

    def lua_constant(name: str) -> int:
        match = re.search(rf"^local {name} = (\d+)$", bridge, re.MULTILINE)
        assert match is not None, f"{name} is no longer declared as a plain Lua constant"
        return int(match.group(1))

    poll_ms = lua_constant("POLL_MS")
    worst_action_ms = poll_ms * max(
        lua_constant("MAX_STARTUP_WAIT_TICKS"), lua_constant("MAX_ACTIVATION_WAIT_TICKS")
    )
    # The outcome is only visible once the bridge publishes it, so allow for one
    # further heartbeat period on top of the wait itself.
    required_ms = worst_action_ms + lua_constant("HEARTBEAT_MS")

    match = re.search(r"const BRIDGE_STARTUP_ACTION_BUDGET_MS = ([\d_]+);", panel)
    assert match is not None, "the panel no longer declares a per-action startup budget"
    budget_ms = int(match.group(1).replace("_", ""))
    assert budget_ms >= required_ms, (
        f"the panel allows {budget_ms} ms per startup action while the bridge may take {required_ms} ms"
    )
