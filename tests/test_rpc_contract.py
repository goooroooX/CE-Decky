from __future__ import annotations

import importlib
import inspect
import logging
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FrontendParameter:
    name: str
    optional: bool


def _matching_square(source: str, start: int) -> int:
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "[":
            depth += 1
        elif source[index] == "]":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unterminated frontend RPC parameter tuple")


def _split_top_level(source: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    for index, character in enumerate(source):
        if character in depths:
            depths[character] += 1
        elif character in pairs and depths[pairs[character]]:
            depths[pairs[character]] -= 1
        elif character == "," and not any(depths.values()):
            parts.append(source[start:index].strip())
            start = index + 1
    tail = source[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _snake_case(name: str) -> str:
    words = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", words).lower()


def _frontend_rpc_contract() -> dict[str, list[FrontendParameter]]:
    root = Path(__file__).resolve().parents[1]
    source = (root / "src" / "api.ts").read_text(encoding="utf-8")
    result: dict[str, list[FrontendParameter]] = {}
    declaration = re.compile(r"export\s+const\s+\w+\s*=\s*callable\s*<\s*\[")
    for match in declaration.finditer(source):
        tuple_start = source.index("[", match.start())
        tuple_end = _matching_square(source, tuple_start)
        rpc_match = re.search(r'>\s*\(\s*"([a-z0-9_]+)"\s*\)', source[tuple_end:])
        assert rpc_match is not None, "frontend callable is missing its RPC name"
        params: list[FrontendParameter] = []
        for item in _split_top_level(source[tuple_start + 1:tuple_end]):
            parameter = re.match(r"([A-Za-z][A-Za-z0-9]*)(\?)?\s*:", item)
            assert parameter is not None, f"cannot parse frontend RPC parameter: {item!r}"
            params.append(FrontendParameter(_snake_case(parameter.group(1)), bool(parameter.group(2))))
        result[rpc_match.group(1)] = params
    callable_exports = re.findall(r"export\s+const\s+\w+\s*=\s*callable\s*<", source)
    assert len(result) == len(callable_exports), "frontend RPC parser skipped or duplicated a callable export"
    return result


# RPC the panel deliberately never calls. `get_poll_counters` is read by the
# tracked cost harness either side of a measured window; wiring it into the
# frontend would put a display refresh on the instrument's own path and make
# reading the counters one of the counts.
DIAGNOSTIC_RPC = {"get_poll_counters"}


def _plugin_class(monkeypatch):
    fake_decky = types.ModuleType("decky")
    fake_decky.logger = logging.getLogger("fake-decky-rpc-contract")
    monkeypatch.setitem(sys.modules, "decky", fake_decky)
    sys.modules.pop("ce_decky.plugin", None)
    return importlib.import_module("ce_decky.plugin").Plugin


def _frontend_rpc_names() -> set[str]:
    return set(_frontend_rpc_contract())


def test_every_frontend_rpc_has_async_plugin_method_and_service_target(monkeypatch):
    Plugin = _plugin_class(monkeypatch)
    from ce_decky.service import PluginService

    rpc_names = _frontend_rpc_names() | DIAGNOSTIC_RPC
    assert rpc_names, "frontend RPC discovery unexpectedly returned no methods"
    for name in sorted(rpc_names):
        plugin_method = getattr(Plugin, name, None)
        service_method = getattr(PluginService, name if name != "run_self_test" else "self_test", None)
        assert plugin_method is not None, f"frontend RPC {name!r} has no Plugin method"
        assert inspect.iscoroutinefunction(plugin_method), f"Plugin.{name} must remain async for Decky callable semantics"
        assert service_method is not None and callable(service_method), f"frontend RPC {name!r} has no backend service target"


def test_plugin_exposes_no_unexpected_public_rpc(monkeypatch):
    Plugin = _plugin_class(monkeypatch)
    frontend = _frontend_rpc_names()
    public_async = {
        name
        for name, value in vars(Plugin).items()
        if inspect.iscoroutinefunction(value) and not name.startswith("_")
    }
    assert public_async == frontend | DIAGNOSTIC_RPC


def test_frontend_rpc_parameters_are_call_compatible_with_plugin(monkeypatch):
    Plugin = _plugin_class(monkeypatch)
    for rpc_name, frontend_params in _frontend_rpc_contract().items():
        backend_params = list(inspect.signature(getattr(Plugin, rpc_name)).parameters.values())[1:]
        assert [param.name for param in backend_params] == [param.name for param in frontend_params], (
            f"frontend/backend parameter order drift for {rpc_name}"
        )
        for frontend, backend in zip(frontend_params, backend_params):
            backend_optional = backend.default is not inspect.Parameter.empty
            assert not frontend.optional or backend_optional, (
                f"frontend may omit {rpc_name}.{frontend.name}, but the backend requires it"
            )


def test_high_risk_rpc_methods_forward_arguments_without_reordering(monkeypatch):
    Plugin = _plugin_class(monkeypatch)
    calls: list[tuple[str, tuple[object, ...]]] = []

    class ServiceSpy:
        def import_table(self, *args):
            calls.append(("import_table", args))
            return "imported"

        async def wait_table_acquisition(self, *args):
            calls.append(("wait_table_acquisition", args))
            return "ready"

        def complete_table_acquisition(self, *args):
            calls.append(("complete_table_acquisition", args))
            return "completed"

        def save_profile(self, *args):
            calls.append(("save_profile", args))
            return "saved"

        def set_startup_preference(self, *args):
            calls.append(("set_startup_preference", args))
            return "startup"

        def validate_effective_startup_plan(self, *args):
            calls.append(("validate_effective_startup_plan", args))
            return "valid"

        async def launch_ce_for_game(self, *args):
            calls.append(("launch_ce_for_game", args))
            return "launched"

        async def stop_ce_for_game(self, *args):
            calls.append(("stop_ce_for_game", args))
            return "stopped"

    class OperationsSpy:
        closing = False

        async def run_blocking(self, function, *args):
            return function(*args)

        async def create(self, awaitable, *, label):
            return await awaitable

    async def exercise() -> None:
        plugin = Plugin()
        plugin.operations = OperationsSpy()
        plugin.service = ServiceSpy()
        assert await plugin.import_table("table.zip", "member.CT", "secret") == "imported"
        assert await plugin.complete_table_acquisition("acq", "/picked", "member.CT", "secret") == "completed"
        assert await plugin.save_profile(620, "Portal 2", False, "sha", "portal2.exe") == "saved"
        assert await plugin.set_startup_preference(620, "sha", 7, True, "100") == "startup"
        assert await plugin.validate_effective_startup_plan(620, "sha", [], []) == "valid"
        assert await plugin.launch_ce_for_game(620, "proton-9") == "launched"
        assert await plugin.stop_ce_for_game(620) == "stopped"
        assert await plugin.stop_ce_for_game(620, "a" * 64) == "stopped"
        assert await plugin.stop_ce_for_game(620, None, True) == "stopped"

    import asyncio

    asyncio.run(exercise())
    assert calls == [
        ("import_table", ("table.zip", "member.CT", "secret", None)),
        ("wait_table_acquisition", ("acq",)),
        ("complete_table_acquisition", ("acq", "/picked", "member.CT", "secret")),
        ("save_profile", (620, "Portal 2", False, "sha", "portal2.exe")),
        ("set_startup_preference", (620, "sha", 7, True, "100")),
        ("validate_effective_startup_plan", (620, "sha", [], [])),
        ("launch_ce_for_game", (620, "proton-9")),
        ("stop_ce_for_game", (620, None, False)),
        ("stop_ce_for_game", (620, "a" * 64, False)),
        ("stop_ce_for_game", (620, None, True)),
    ]


def test_the_panel_knows_every_cause_the_backend_records():
    """The chip on a retired row is keyed by a token the backend writes.

    A cause added on one side and not the other is silent: the record still
    refuses the import, so nothing fails, and the row just stops saying which
    of the three things happened to it, which is the whole reason the cause is
    recorded. The panel's fail-soft floor is for a corrupt file, not for a
    vocabulary the two halves of this product disagree about.
    """
    from ce_decky.table_blocklist import BLOCK_CAUSES

    root = Path(__file__).resolve().parents[1]
    types_source = (root / "src" / "types.ts").read_text(encoding="utf-8")
    declared = re.search(r"export type BlockedTableCause =([^;]+);", types_source)
    assert declared is not None, "src/types.ts no longer declares BlockedTableCause"
    frontend = set(re.findall(r'"([a-z_]+)"', declared.group(1)))
    assert frontend == set(BLOCK_CAUSES)

    # And every one of them names a chip, so none can render as an empty mark.
    catalog_source = (root / "src" / "providerCatalog.tsx").read_text(encoding="utf-8")
    chips = re.search(r"BLOCKED_MARK_TEXT: Record<BlockedTableCause, string> = \{([^}]+)\}", catalog_source)
    assert chips is not None, "src/providerCatalog.tsx no longer maps a cause to a chip"
    assert {name for name in re.findall(r"^\s*([a-z_]+):", chips.group(1), re.M)} == set(BLOCK_CAUSES)
