"""Disposable child of decky_compat_probe.py; never instantiate a live plugin."""
from pathlib import Path
import asyncio
import bz2
import importlib.util
import json
import logging
import lzma
import shutil
import sys
import threading
import types


class ProbeUnavailable(RuntimeError):
    pass


def run(root: Path, temporary: Path) -> dict:
    if sys.flags.optimize:
        raise ProbeUnavailable("runtime disables assertions; the diagnostic checks cannot establish compatibility")
    sys.dont_write_bytecode = True
    sys.modules["decky"] = types.ModuleType("decky")
    spec = importlib.util.spec_from_file_location("compatibility_plugin_main", root / "main.py")
    entrypoint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entrypoint)
    assert isinstance(entrypoint.Plugin, type)
    from ce_decky import __version__, runtime_compat
    from ce_decky.atomic import atomic_write_json, load_json
    from ce_decky.ce_launch import CELaunchSupervisor
    from ce_decky.ct_inspector import inspect_table
    from ce_decky.session_protocol import SessionStore
    from ce_decky.table_store import TableStore
    from bs4 import BeautifulSoup
    from defusedxml.ElementTree import fromstring
    from defusedxml.common import DefusedXmlException
    import httpcore._synchronization as synchronization
    assert synchronization.anyio is not None, "httpcore silently disabled its async backend"
    checks = ["entrypoint", "async_backend"]
    assert BeautifulSoup("<p>A&amp;B</p>", "html.parser").get_text() == "A&B"
    try:
        fromstring('<!DOCTYPE r [<!ENTITY a "bad">]><r>&a;</r>')
    except DefusedXmlException:
        pass
    else:
        raise AssertionError("XML entity accepted")
    for codec in (bz2, lzma):
        assert codec.decompress(codec.compress(b"fixture")) == b"fixture"
    checks.append("parsers_archives")
    data = b"<CheatTable><CheatEntries /></CheatTable>"
    source = temporary / "fixture.CT"
    source.write_bytes(data)
    tables = TableStore(temporary / "tables")
    table = tables.import_ct(str(source))
    assert tables.verified_blob(table.sha256).read_bytes() == data
    assert inspect_table(Path(table.blob_path), table.sha256).total_entries == 0
    assert len(tables.list_tables()) == 1
    state = temporary / "state.json"
    atomic_write_json(state, {"ok": True})
    assert load_json(state, None) == {"ok": True}
    checks.append("offline_table_state")

    private = temporary / "private"
    private.mkdir()
    managed = private / "managed"
    managed.mkdir()
    sessions = SessionStore(managed, temporary)
    hidden_tables = TableStore(managed / "tables")
    launches = CELaunchSupervisor(temporary, temporary / "ce", managed, logging.getLogger("compatibility"))
    assert sessions.load_current(42) is None
    assert sessions.inventory()["total_sessions"] == 0
    assert hidden_tables.list_tables_with_errors() == ([], [])
    assert launches.inspect_owned_launch_record(42)["state"] == "absent"
    witness = managed / "witness"
    witness.write_bytes(b"preserved")
    private.chmod(0)
    try:
        try:
            managed.stat()
        except PermissionError:
            pass
        else:
            raise ProbeUnavailable("permission fixture has privilege; access denial was not exercised")
        for reader in (lambda: sessions.load_current(42), sessions.inventory,
                       hidden_tables.list_tables_with_errors, lambda: launches.inspect_owned_launch_record(42)):
            try:
                reader()
            except (ValueError, OSError):
                pass
            else:
                raise AssertionError("unreadable state was reported as absent")
        assert launches.has_live_owned_launch(), "unreadable ownership lost protection"
    finally:
        private.chmod(0o700)
    assert witness.read_bytes() == b"preserved"
    checks.append("state_access_refusals")

    async def network():
        from ce_decky import network as production
        from ce_decky.network_tls import build_verified_ssl_context
        async def respond(reader, writer):
            try:
                await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nfixture")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(respond, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
        original = production.validate_network_url
        async def fixture_policy(candidate, hosts):
            assert candidate == url and hosts == frozenset({"127.0.0.1"})
            return "127.0.0.1"
        # Fixture-only substitution; never used in the plugin or for live URLs.
        production.validate_network_url = fixture_policy
        client = production.NetworkClient(build_verified_ssl_context()[0])
        try:
            async with synchronization.AsyncLock():
                pass
            response = await client.get(url, allowed_hosts=frozenset({"127.0.0.1"}), max_bytes=16, retries=0)
            assert response.body == b"fixture"
            target = temporary / "download"
            await client.download(url, target, allowed_hosts=frozenset({"127.0.0.1"}), max_bytes=16)
            assert target.read_bytes() == b"fixture"
            assert await asyncio.to_thread(lambda: 42) == 42
        finally:
            await client.aclose()
            production.validate_network_url = original
            server.close()
            await server.wait_closed()
        from ce_decky.child_env import child_environment
        from ce_decky.operations import OperationRegistry
        executable = shutil.which("true")
        if executable is None:
            raise ProbeUnavailable("the harmless child-process fixture is unavailable")
        child = await asyncio.create_subprocess_exec(executable, env=child_environment())
        try:
            assert await asyncio.wait_for(child.wait(), 5) == 0
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
        registry = OperationRegistry()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("worker fixture timed out")
            finished.set()
        task = asyncio.create_task(registry.run_blocking(worker))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            closing = asyncio.create_task(registry.close())
            await asyncio.sleep(0)
            assert not closing.done(), "cancelling the awaiter lost ownership of its worker"
            release.set()
            await asyncio.wait_for(closing, 3)
            assert finished.is_set() and registry.active_count == 0
        finally:
            release.set()
            await registry.close()
    asyncio.run(network())
    checks.extend(["async_http_download_threads", "async_child_and_worker_drain"])
    return {"status": "PASS", "checks": checks, "plugin_version": __version__,
            "python": sys.version.split()[0], "frozen": bool(getattr(sys, "frozen", False)),
            "pathlib_types_fallback": runtime_compat.PATHLIB_TYPES_FALLBACK_USED}


if "compat_plugin_root" in globals():
    report = Path(compat_report_path)
    report.write_text(json.dumps({"status": "RUNNING"}), encoding="utf-8")
    try:
        result = run(Path(compat_plugin_root), report.parent)
    except ProbeUnavailable as exc:
        result = {"status": "INCOMPLETE", "error": str(exc)}
    except Exception as exc:
        result = {"status": "FAIL", "error": f"{type(exc).__name__}: {str(exc)[:1000]}"}
    report.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    raise SystemExit({"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[result["status"]])
