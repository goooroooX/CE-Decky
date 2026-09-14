from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import asyncio
import logging
import threading
import time

import pytest

from ce_decky import acquisition
from ce_decky.archive_import import ARGV_PASSWORD_FORMATS, argv_password_refusal
from ce_decky.acquisition import detect_download, snapshot_downloads
from ce_decky.acquisition import AcquisitionManager, ACQUISITION_TTL_SECONDS
from ce_decky.catalog import ArtifactRecord, PlaygroundDownloadRequest, ProviderRateLimited
from ce_decky.network import ArtifactGone
from ce_decky.providers import CatalogResult
from ce_decky.table_store import TableStore
from ce_decky.network import NetworkError
import ce_decky.acquisition as acquisition_module


CT = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'
# One real Playground table closes a CheatEntry it never opened; the bytes still
# match the advertised SHA-256, so only structural inspection can reject it.
DAMAGED_CT = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries><CheatEntry><ID>1</ID></CheatEntry></CheatEntry></CheatEntries></CheatTable>'


def _record(filename="Example.CT", size=None, version=None):
    return ArtifactRecord(CatalogResult(
        provider="playground", provider_display_name="Playground", topic_id="1",
        artifact_id="page-1:file-2", table_title="Example", filename=filename,
        version=version, size_bytes=size, source_page="https://www.playground.ru/cheat/example-1",
        download_mode="system_browser_downloads", match_score=.9, provider_rank=85,
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("member_count", [1, 2])
async def test_blocked_archive_preserves_final_identity_without_damaged_mark(tmp_path: Path, member_count):
    import io
    import zipfile
    from ce_decky.artifact_resolutions import ArtifactResolutions
    from ce_decky.table_blocklist import TableBlocklist

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(member_count):
            archive.writestr(f"{index}.CT", CT)
    payload = buffer.getvalue()
    digest = sha256(CT).hexdigest()
    archive_sha = sha256(payload).hexdigest()
    base = _record(filename="Pack.zip").result
    record = ArtifactRecord(CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
                            direct_url="https://www.playground.ru/download/pack.zip", advertised_sha256=archive_sha)
    blocks = TableBlocklist(tmp_path / "blocked.json")
    blocks.block(sha256=digest, reason="A cheat did not activate", cause="refused")
    store = TableStore(tmp_path / "tables", assert_importable=lambda digest, origins: blocks.assert_importable(digest))
    mappings = ArtifactResolutions(tmp_path / "resolutions.json")
    manager = AcquisitionManager(_Catalog(record), _Network(payload), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)
    manager.record_resolution = mappings.record
    started = await manager.start(record.result.provider, record.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))
    state = await manager.complete(str(started["acquisition_id"]), member_path="0.CT")
    assert state["state"] == "failed"
    assert state["resolved_table_sha256"] == digest
    assert not state["artifact_rejected"]
    assert len(mappings.snapshot()["entries"]) == (1 if member_count == 1 else 0)
    await manager.close()


def test_download_detection_requires_one_new_stable_matching_regular_file(tmp_path: Path):
    downloads = tmp_path / "Downloads"
    before = snapshot_downloads(downloads)
    target = downloads / "Example.CT"
    target.write_bytes(CT)
    assert detect_download(downloads, before, _record(size=len(CT))) == target
    (downloads / "Example (1).CT").write_bytes(CT)
    with pytest.raises(ValueError, match="ambiguous"):
        detect_download(downloads, before, _record(size=len(CT)))


def test_multi_origin_same_sha_is_bounded_and_deduplicated(tmp_path: Path):
    source = tmp_path / "Example.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "tables")
    artifact = store.import_ct(str(source))
    origin = {
        "provider": "fearless", "artifact_id": "topic-1:attachment-2", "topic_id": "1",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=1",
        "original_filename": "Example.CT", "retrieved_at": "2026-08-16T00:00:00Z",
        "advertised_sha256": sha256(CT).hexdigest(),
    }
    store.add_origin(artifact.sha256, origin)
    store.add_origin(artifact.sha256, {**origin, "retrieved_at": "2026-08-16T00:00:01Z"})
    item = store.get_table(artifact.sha256)
    assert len(item["origins"]) == 1
    assert item["origins"][0]["retrieved_at"] == "2026-08-16T00:00:01Z"


class _Catalog:
    diagnostics = None
    def __init__(self, record):
        self.record = record
    def resolve(self, provider, artifact_id):
        if (provider, artifact_id) != (self.record.result.provider, self.record.result.artifact_id):
            raise ValueError("unknown")
        return self.record

    async def resolve_download(self, record, *, on_countdown=None):
        assert record is self.record
        if on_countdown is not None:
            on_countdown(2)
        return "https://dl1.gamedl.ru/files/Example.CT?token=redacted", frozenset({"dl1.gamedl.ru"})


class _Network:
    def __init__(self, body):
        self.body = body
        self.download_calls = []
        self.closed = False
    async def download(self, url, destination, **kwargs):
        self.download_calls.append((url, kwargs))
        destination.write_bytes(self.body)
    async def aclose(self):
        self.closed = True


class _RateLimitedOnceNetwork(_Network):
    """The provider throttles the first attempt and serves the second."""

    def __init__(self, body, retry_after=None, refusals=1):
        super().__init__(body)
        self.retry_after = retry_after
        self.refusals = refusals

    async def download(self, url, destination, **kwargs):
        self.download_calls.append((url, kwargs))
        if self.refusals > 0:
            self.refusals -= 1
            raise ProviderRateLimited(self.retry_after, "provider download returned HTTP 429")
        destination.write_bytes(self.body)


class _BlockedNetwork(_Network):
    def __init__(self, mode):
        self.mode = mode
    async def download(self, url, destination, **kwargs):
        if self.mode == "403":
            raise NetworkError("provider download returned HTTP 403")
        destination.write_bytes(b"<!doctype html><html>challenge</html>")


class _GoneNetwork(_Network):
    """The provider answering about this exact file: it is not there."""

    def __init__(self, detail="HTTP 404"):
        self.detail = detail

    async def download(self, url, destination, **kwargs):
        raise ArtifactGone(self.detail)


class _WaitingCatalog(_Catalog):
    def __init__(self, record):
        super().__init__(record)
        self.started = __import__("asyncio").Event()

    async def resolve_download(self, record, *, on_countdown=None):
        assert record is self.record
        if on_countdown is not None:
            on_countdown(60)
        self.started.set()
        await __import__("asyncio").Event().wait()
        raise AssertionError("cancelled resolver continued")


class _Diagnostics:
    def __init__(self):
        self.failures = []
        self.throttles = []
        self.downloads = []

    def record_failure(self, provider, **kwargs):
        self.failures.append((provider, kwargs))

    def record_throttle(self, provider, **kwargs):
        self.throttles.append((provider, kwargs))

    def record_download(self, provider, **kwargs):
        self.downloads.append((provider, kwargs))


class _DiagnosticCatalog(_Catalog):
    """An ordinary catalog that keeps provider diagnostics, as the real one does."""

    def __init__(self, record):
        super().__init__(record)
        self.diagnostics = _Diagnostics()


class _RateLimitedCatalog(_DiagnosticCatalog):
    """A rate limit that lands after the link handshake has spent its one token."""

    async def resolve_download(self, record, *, on_countdown=None):
        assert record is self.record
        raise ProviderRateLimited("90", "Playground API returned HTTP 429", restartable=False)


class _RateLimitedOnceCatalog(_DiagnosticCatalog):
    """A rate limit on the opening request, which has consumed nothing yet."""

    def __init__(self, record, refusals=1):
        super().__init__(record)
        self.refusals = refusals
        self.attempts = 0

    async def resolve_download(self, record, *, on_countdown=None):
        assert record is self.record
        self.attempts += 1
        if self.refusals > 0:
            self.refusals -= 1
            raise ProviderRateLimited(None, "Playground API returned HTTP 429")
        return "https://dl1.gamedl.ru/files/Example.CT?token=redacted", frozenset({"dl1.gamedl.ru"})


@pytest.mark.asyncio
async def test_a_rate_limited_download_waits_and_succeeds(tmp_path: Path, monkeypatch):
    """A 429 is a wait, not a failed download.

    On the target one artifact refused with HTTP 429 and downloaded normally
    half a minute later: a background listing crawl and this download were
    asking the same provider for budget at the same moment. It was reported as
    a failed download, and the only way forward the user was offered was
    pressing the row again.
    """
    slept: list[float] = []
    # `asyncio.sleep` is one module attribute for the whole process, so the
    # replacement has to keep working as a sleep: this test's own poll loop
    # yields through it too, and a stub that never yields would spin forever
    # without ever letting the download it is waiting for run.
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(acquisition.asyncio, "sleep", _record_sleep)
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT")
    network = _RateLimitedOnceNetwork(CT)
    manager = AcquisitionManager(_Catalog(record), network, TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)

    while manager.poll(started["acquisition_id"])["state"] in {"downloading", "waiting_provider"}:
        await real_sleep(0)

    assert manager.poll(started["acquisition_id"])["state"] == "ready_to_import"
    assert len(network.download_calls) == 2
    # The first wait of the interactive backoff, because this provider named no
    # number of its own.
    assert slept == [acquisition.DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS[0]]
    await manager.close()


@pytest.mark.asyncio
async def test_a_rate_limited_download_believes_the_provider_over_the_backoff(tmp_path: Path, monkeypatch):
    """`Retry-After` is the provider saying what it is enforcing; the backoff is a guess."""
    slept: list[float] = []
    # `asyncio.sleep` is one module attribute for the whole process, so the
    # replacement has to keep working as a sleep: this test's own poll loop
    # yields through it too, and a stub that never yields would spin forever
    # without ever letting the download it is waiting for run.
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(acquisition.asyncio, "sleep", _record_sleep)
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT")
    network = _RateLimitedOnceNetwork(CT, retry_after="30")
    manager = AcquisitionManager(_Catalog(record), network, TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)

    while manager.poll(started["acquisition_id"])["state"] in {"downloading", "waiting_provider"}:
        await real_sleep(0)

    assert manager.poll(started["acquisition_id"])["state"] == "ready_to_import"
    assert slept == [30]
    await manager.close()


@pytest.mark.asyncio
async def test_a_zero_second_retry_after_still_waits_before_asking_again(tmp_path: Path, monkeypatch):
    """A provider asking to be retried at once is how one rate limit becomes several."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(acquisition.asyncio, "sleep", _record_sleep)
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT")
    network = _RateLimitedOnceNetwork(CT, retry_after="0")
    manager = AcquisitionManager(_Catalog(record), network, TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)

    while manager.poll(started["acquisition_id"])["state"] in {"downloading", "waiting_provider"}:
        await real_sleep(0)

    assert manager.poll(started["acquisition_id"])["state"] == "ready_to_import"
    assert slept == [1]
    await manager.close()


@pytest.mark.asyncio
async def test_a_provider_that_keeps_refusing_is_still_a_bounded_failure(tmp_path: Path, monkeypatch):
    """Waiting is bounded: a throttle that does not clear must not hold the row forever."""
    slept: list[float] = []
    # `asyncio.sleep` is one module attribute for the whole process, so the
    # replacement has to keep working as a sleep: this test's own poll loop
    # yields through it too, and a stub that never yields would spin forever
    # without ever letting the download it is waiting for run.
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(acquisition.asyncio, "sleep", _record_sleep)
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT")
    network = _RateLimitedOnceNetwork(CT, refusals=99)
    manager = AcquisitionManager(_Catalog(record), network, TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)

    while manager.poll(started["acquisition_id"])["state"] in {"downloading", "waiting_provider"}:
        await real_sleep(0)

    state = manager.poll(started["acquisition_id"])
    assert state["state"] == "failed"
    assert "429" in state["error"]
    assert list(slept) == list(acquisition.DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS)
    assert len(network.download_calls) == len(acquisition.DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS) + 1
    await manager.close()


@pytest.mark.asyncio
async def test_direct_acquisition_uses_opaque_id_imports_and_expires(tmp_path: Path):
    digest = sha256(CT).hexdigest()
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT", advertised_sha256=digest)
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    assert len(started["acquisition_id"]) == 32 and "https://" not in started["acquisition_id"]
    while manager.poll(started["acquisition_id"])["state"] == "downloading":
        await __import__("asyncio").sleep(0)
    completed = await manager.complete(started["acquisition_id"])
    assert completed["state"] == "imported"
    assert completed["execution_consent"] is False
    manager.items[started["acquisition_id"]].updated -= ACQUISITION_TTL_SECONDS + 1
    with pytest.raises(ValueError, match="expired"):
        manager.poll(started["acquisition_id"])
    await manager.close()


@pytest.mark.asyncio
async def test_an_import_in_progress_is_never_expired_underneath_itself(tmp_path: Path, monkeypatch):
    """Crossing the TTL boundary mid-import must not delete the import's source.

    Expiry sweeps on age alone from any poll, and it unlinks the staged file
    before dropping the acquisition. Hashing, extracting and storing a large
    table is unbounded work that a Complete pressed just before the boundary
    runs straight across, so the source could disappear while the operation
    that owns it is still reading it.
    """
    digest = sha256(CT).hexdigest()
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT", advertised_sha256=digest,
    )
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(
        _Catalog(record), _Network(CT), store,
        tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None,
    )  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    while manager.poll(started["acquisition_id"])["state"] == "downloading":
        await asyncio.sleep(0)

    item = manager.items[started["acquisition_id"]]
    staged = item.staged_path
    assert staged is not None and staged.is_file()

    importing = threading.Event()
    release = threading.Event()
    original = store.import_selection

    def slow_import(*args, **kwargs):
        importing.set()
        release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "import_selection", slow_import)

    completion = asyncio.create_task(manager.complete(started["acquisition_id"]))
    while not importing.is_set():
        await asyncio.sleep(0)
    # The boundary is crossed while the import holds the acquisition. The sweep
    # is what every poll runs, and it never takes the item lock, so it is what
    # could retire an acquisition another thread is in the middle of importing.
    item.updated -= ACQUISITION_TTL_SECONDS + 1
    manager._expire()
    assert started["acquisition_id"] in manager.items
    assert staged.is_file(), "the import's own source was unlinked underneath it"
    release.set()

    completed = await completion
    assert completed["state"] == "imported"
    # The lease ends with the import, and the ordinary TTL resumes from there.
    item.updated -= ACQUISITION_TTL_SECONDS + 1
    with pytest.raises(ValueError, match="expired"):
        manager.poll(started["acquisition_id"])
    await manager.close()


@pytest.mark.asyncio
async def test_expiry_cannot_retire_an_acquisition_another_thread_is_holding(tmp_path: Path):
    """The lease is taken under the item lock, so expiry has to respect that lock.

    An importer proves the acquisition is still registered and then takes its
    lease, both while holding the item lock. Expiry only read the lease counter
    beside the lock, so it could still delete the acquisition and unlink its
    staging in the gap between those two steps, and the import would continue
    against a file that no longer exists.
    """
    digest = sha256(CT).hexdigest()
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT", advertised_sha256=digest,
    )
    manager = AcquisitionManager(
        _Catalog(record), _Network(CT), TableStore(tmp_path / "tables"),
        tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None,
    )  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    while manager.poll(started["acquisition_id"])["state"] == "downloading":
        await asyncio.sleep(0)

    item = manager.items[started["acquisition_id"]]
    staged = item.staged_path
    assert staged is not None and staged.is_file()
    # Expired by age, held by a caller, and the lease not taken yet: exactly the
    # instant between the identity check and the admission.
    item.updated -= ACQUISITION_TTL_SECONDS + 1
    assert item.active_mutations == 0

    swept = threading.Event()
    with item.mutation_lock:
        threading.Thread(target=lambda: (manager._expire(), swept.set()), daemon=True).start()
        assert swept.wait(timeout=5), "the sweep blocked on the holder instead of skipping it"
        assert started["acquisition_id"] in manager.items
        assert staged.is_file()

    # Once nobody holds it, the ordinary lifetime applies again.
    manager._expire()
    assert started["acquisition_id"] not in manager.items
    assert not staged.exists()
    await manager.close()


@pytest.mark.asyncio
async def test_browser_handoff_cannot_publish_after_close_finishes(tmp_path: Path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def blocked_snapshot(_root):
        entered.set()
        release.wait(timeout=5)
        return {}

    monkeypatch.setattr(acquisition_module, "snapshot_downloads", blocked_snapshot)
    record = _record()
    network = _Network(CT)
    manager = AcquisitionManager(
        _Catalog(record), network, TableStore(tmp_path / "tables"),
        tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None,
    )  # type: ignore[arg-type]

    start = asyncio.create_task(manager.start("playground", record.result.artifact_id))
    while not entered.is_set():
        await asyncio.sleep(0)
    await manager.close()
    release.set()

    with pytest.raises(RuntimeError, match="unloading"):
        await start
    assert manager.items == {}


@pytest.mark.asyncio
async def test_close_cancellation_drains_staged_file_cleanup(tmp_path: Path, monkeypatch):
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    manager = AcquisitionManager(
        _Catalog(record), _Network(CT), TableStore(tmp_path / "tables"),
        tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None,
    )  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    acquisition_id = str(started["acquisition_id"])
    await manager.wait_ready(acquisition_id)
    staged = manager.items[acquisition_id].staged_path
    assert staged is not None and staged.is_file()

    entered = threading.Event()
    release = threading.Event()
    original_unlink = acquisition_module._safe_unlink

    def blocked_unlink(path):
        entered.set()
        assert release.wait(2)
        original_unlink(path)

    monkeypatch.setattr(acquisition_module, "_safe_unlink", blocked_unlink)
    closing = asyncio.create_task(manager.close())
    assert await asyncio.to_thread(entered.wait, 2)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not staged.exists()


@pytest.mark.asyncio
async def test_repeatedly_cancelled_close_drains_cleanup_and_releases_the_network(tmp_path: Path, monkeypatch):
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    network = _Network(CT)
    manager = AcquisitionManager(
        _Catalog(record), network, TableStore(tmp_path / "tables"),
        tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None,
    )  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    acquisition_id = str(started["acquisition_id"])
    await manager.wait_ready(acquisition_id)
    staged = manager.items[acquisition_id].staged_path
    assert staged is not None and staged.is_file()

    entered = threading.Event()
    release = threading.Event()
    original_unlink = acquisition_module._safe_unlink

    def blocked_unlink(path):
        entered.set()
        assert release.wait(2)
        original_unlink(path)

    monkeypatch.setattr(acquisition_module, "_safe_unlink", blocked_unlink)
    closing = asyncio.create_task(manager.close())
    assert await asyncio.to_thread(entered.wait, 2)
    # A shutdown that cancels more than once must not orphan the unlink worker
    # and must not skip releasing shared HTTP ownership.
    for _ in range(3):
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert not staged.exists()
    assert network.closed


@pytest.mark.asyncio
async def test_playground_display_size_does_not_override_matching_artifact_sha(tmp_path: Path):
    digest = sha256(CT).hexdigest()
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT) + 1).result,
        direct_url="https://www.playground.ru/files/Example.CT",
        advertised_sha256=digest,
        size_exact=False,
    )
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])

    ready = manager.poll(started["acquisition_id"])
    assert ready["state"] == "ready_to_import"
    assert ready["bytes_received"] == len(CT)
    assert ready["expected_bytes"] == len(CT) + 1
    assert (await manager.complete(started["acquisition_id"]))["state"] == "imported"
    await manager.close()


@pytest.mark.asyncio
async def test_acquisition_failure_log_names_phase_and_reason_without_provider_url(tmp_path: Path, caplog):
    logger = logging.getLogger("ce-decky-acquisition-test")
    caplog.set_level(logging.INFO, logger=logger.name)
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT) + 1).result,
        direct_url="https://www.playground.ru/files/Example.CT?token=do-not-log",
        advertised_sha256=sha256(CT).hexdigest(),
        size_exact=True,
    )
    manager = AcquisitionManager(
        _Catalog(record), _Network(CT), TableStore(tmp_path / "tables"),
        tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home",
        lambda: None, logger=logger,
    )  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])

    assert manager.poll(started["acquisition_id"])["state"] == "failed"
    output = "\n".join(item.getMessage() for item in caplog.records)
    assert "event=acquisition.download_failed" in output
    assert "phase=inspect" in output
    assert "downloaded artifact size does not match provider metadata" in output
    assert "do-not-log" not in output
    await manager.close()


@pytest.mark.asyncio
async def test_resolved_playground_link_stays_backend_owned(tmp_path: Path):
    digest = sha256(CT).hexdigest()
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        advertised_sha256=digest,
        size_exact=True,
        acquisition=PlaygroundDownloadRequest("2", "1"),
    )
    network = _Network(CT)
    manager = AcquisitionManager(_Catalog(record), network, TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    assert "url" not in started and started["source_page"] is None
    await manager.wait_ready(started["acquisition_id"])
    assert manager.poll(started["acquisition_id"])["state"] == "ready_to_import"
    assert network.download_calls[0][1]["allowed_hosts"] == frozenset({"dl1.gamedl.ru"})
    await manager.close()


@pytest.mark.asyncio
async def test_single_encrypted_member_is_reported_as_needing_input(tmp_path: Path, monkeypatch):
    record = ArtifactRecord(
        _record(filename="Locked.zip").result,
        direct_url="https://www.playground.ru/files/Locked.zip",
    )
    monkeypatch.setattr(acquisition_module, "_inspect_download", lambda *_args: (
        b"7z", 10,
        {"format": "zip", "members": [{
            "path": "locked.CT", "size": 20, "packed_size": 12,
            "encrypted": True, "format": "zip",
        }]},
        None,
    ))
    manager = AcquisitionManager(_Catalog(record), _Network(b"archive"), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])

    assert manager.poll(started["acquisition_id"])["state"] == "needs_selection"
    await manager.close()


@pytest.mark.asyncio
async def test_late_cancel_cannot_delete_staging_from_under_import(tmp_path: Path, monkeypatch):
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(_Catalog(record), _Network(CT), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])
    entered = threading.Event()
    release = threading.Event()
    original_import = store.import_selection

    def blocked_import(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original_import(*args, **kwargs)

    monkeypatch.setattr(store, "import_selection", blocked_import)
    completing = asyncio.create_task(manager.complete(started["acquisition_id"]))
    assert await asyncio.to_thread(entered.wait, 2)
    cancelling = asyncio.create_task(manager.cancel(started["acquisition_id"]))
    await asyncio.sleep(0)
    assert not cancelling.done()
    release.set()

    completed = await completing
    cancelled = await cancelling
    assert completed["state"] == "imported"
    assert cancelled["state"] == "imported"
    assert manager.poll(started["acquisition_id"])["state"] == "imported"
    await manager.close()


@pytest.mark.asyncio
async def test_cancel_cleans_completion_failure_staging_without_erasing_diagnostic(tmp_path: Path):
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    acquisition_id = str(started["acquisition_id"])
    await manager.wait_ready(acquisition_id)
    item = manager.items[acquisition_id]
    staged = item.staged_path
    assert staged is not None and staged.is_file()
    item.record = ArtifactRecord(record.result, direct_url=record.direct_url, advertised_sha256="0" * 64)

    with pytest.raises(ValueError, match="advertised SHA-256"):
        await manager.complete(acquisition_id)
    assert manager.poll(acquisition_id)["state"] == "failed"
    assert staged.is_file()

    cancelled = await manager.cancel(acquisition_id)
    assert cancelled["state"] == "failed"
    assert "advertised SHA-256" in str(cancelled["error"])
    assert not staged.exists()
    await manager.close()


@pytest.mark.asyncio
async def test_a_committed_table_is_checked_against_gits_own_object_name(tmp_path: Path):
    """The GitHub tree route advertises a git object name, not a content digest.

    It is still an exact identity, so it is verified rather than trusted: git
    defines the name as the SHA-1 of `blob <length>` plus a NUL plus the bytes,
    and the same bytes must reproduce it.
    """
    from ce_decky.github_discovery import git_blob_sha1

    base = _record(filename="Ducks.CT", size=len(CT)).result
    result = CatalogResult(**{**base.as_dict(), "provider": "github", "download_mode": "direct_https"})
    good = ArtifactRecord(
        result,
        direct_url="https://raw.githubusercontent.com/someone/tables/main/Ducks.CT",
        blob_sha1=git_blob_sha1(CT),
    )
    manager = AcquisitionManager(_Catalog(good), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("github", good.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))
    assert manager.poll(str(started["acquisition_id"]))["state"] == "ready_to_import"
    await manager.close()

    wrong = ArtifactRecord(result, direct_url=good.direct_url, blob_sha1="a" * 40)
    manager = AcquisitionManager(_Catalog(wrong), _Network(CT), TableStore(tmp_path / "tables2"), tmp_path / "tmp2", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("github", wrong.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))
    state = manager.poll(str(started["acquisition_id"]))
    assert state["state"] == "failed"
    assert "git object name" in str(state["error"])
    await manager.close()


class _ExclusiveRequest:
    """A provider that serves this client one download at a time.

    VGTimes says so in its own words and refuses the second with its own error
    code, so the plugin holds one in flight rather than being told twice.
    """

    provider = "vgtimes"
    artifact_hosts = frozenset({"vgtimes.ru"})
    send_source_referer = True
    exclusive = True

    def __init__(self) -> None:
        self.inside = 0
        self.overlapped = False
        self.release = asyncio.Event()

    async def resolve(self, network, record, *, sleep, on_countdown=None):
        self.inside += 1
        self.overlapped = self.overlapped or self.inside > 1
        await self.release.wait()
        self.inside -= 1
        return "https://vgtimes.ru/index.php?do=files&op=showfile", self.artifact_hosts


class _TwoRecordCatalog:
    diagnostics = None

    def __init__(self, records):
        self.records = {(row.result.provider, row.result.artifact_id): row for row in records}

    def resolve(self, provider, artifact_id):
        return self.records[(provider, artifact_id)]

    async def resolve_download(self, record, *, on_countdown=None):
        return await record.acquisition.resolve(None, record, sleep=None, on_countdown=on_countdown)


@pytest.mark.asyncio
async def test_a_provider_that_serves_one_download_at_a_time_is_serialized(tmp_path: Path):
    request = _ExclusiveRequest()
    base = _record(filename="Table.CT", size=len(CT)).result

    def row(artifact_id: str) -> ArtifactRecord:
        return ArtifactRecord(
            CatalogResult(**{**base.as_dict(), "provider": "vgtimes", "artifact_id": artifact_id,
                             "download_mode": "direct_https"}),
            acquisition=request,
        )

    records = [row("game-a:file-1"), row("game-b:file-2")]
    manager = AcquisitionManager(_TwoRecordCatalog(records), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    first = await manager.start("vgtimes", "game-a:file-1")
    # The second waits for the first rather than being sent to be refused, and
    # it says it is waiting for the provider rather than looking stalled.
    second = await manager.start("vgtimes", "game-b:file-2")
    await asyncio.sleep(0)
    assert manager.poll(str(second["acquisition_id"]))["state"] == "waiting_provider"
    request.release.set()
    await manager.wait_ready(str(first["acquisition_id"]))
    await manager.wait_ready(str(second["acquisition_id"]))
    assert not request.overlapped
    assert manager.poll(str(first["acquisition_id"]))["state"] == "ready_to_import"
    assert manager.poll(str(second["acquisition_id"]))["state"] == "ready_to_import"
    await manager.close()


@pytest.mark.asyncio
async def test_cancelling_an_acquisition_that_is_still_queued_reports_a_cancellation(tmp_path: Path):
    """Someone who presses Cancel is told it was cancelled, not that it failed.

    The download worker records its own cancellation, but an acquisition
    cancelled while it was still waiting behind another one of the same
    provider never reached that worker, so the terminal-state guard stamped it
    "failed: the download stopped without reporting an outcome".
    """
    request = _ExclusiveRequest()
    base = _record(filename="Table.CT", size=len(CT)).result

    def row(artifact_id: str) -> ArtifactRecord:
        return ArtifactRecord(
            CatalogResult(**{**base.as_dict(), "provider": "vgtimes", "artifact_id": artifact_id,
                             "download_mode": "direct_https"}),
            acquisition=request,
        )

    records = [row("game-a:file-1"), row("game-b:file-2")]
    manager = AcquisitionManager(_TwoRecordCatalog(records), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    first = await manager.start("vgtimes", "game-a:file-1")
    queued = await manager.start("vgtimes", "game-b:file-2")
    await asyncio.sleep(0)
    assert manager.poll(str(queued["acquisition_id"]))["state"] == "waiting_provider"

    cancelled = await manager.cancel(str(queued["acquisition_id"]))
    assert cancelled["state"] == "cancelled"
    assert not cancelled["error"]

    request.release.set()
    await manager.wait_ready(str(first["acquisition_id"]))
    assert manager.poll(str(first["acquisition_id"]))["state"] == "ready_to_import"
    assert manager.poll(str(queued["acquisition_id"]))["state"] == "cancelled"
    await manager.close()


@pytest.mark.asyncio
async def test_an_archive_with_no_table_in_it_reaches_the_list_the_user_can_clear(tmp_path: Path):
    """It opened, it holds no `.CT`, and that is a property of these exact bytes.

    So it is recorded where a damaged table is recorded: the durable list under
    Advanced, by the digest of what was actually downloaded and against the
    provider row it came from, which is what makes it clearable by hand and what
    stops the same download being paid for again on the next search.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "no table here")
    payload = buffer.getvalue()

    base = _record(filename="Pack.zip", size=len(payload)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "filename": "Pack.zip", "download_mode": "direct_https"}),
        direct_url="https://www.playground.ru/download/pack.zip",
    )
    recorded: list[tuple[str, str, str, tuple[str, ...]]] = []
    store = TableStore(tmp_path / "tables", record_unusable=lambda *args: recorded.append(args))
    manager = AcquisitionManager(_Catalog(record), _Network(payload), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start(record.result.provider, record.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))
    state = manager.poll(str(started["acquisition_id"]))
    assert state["state"] == "failed"
    assert state["artifact_rejected"] is True
    # Said to be what it is, rather than described as a damaged file.
    assert "holds no Cheat Engine table" in str(state["error"])

    # Recorded by the digest of what was actually downloaded, against the
    # provider row it came from, which is the pair the durable list is built on.
    assert len(recorded) == 1
    digest, reason, filename, origins, app_id, cause = recorded[0]
    assert digest == sha256(payload).hexdigest()
    assert "holds no Cheat Engine table" in reason
    assert filename == "Pack.zip"
    assert origins == (f"{record.result.provider}:{record.result.artifact_id}",)
    # Nothing said which game this download was for, so the record says nothing.
    assert app_id is None
    assert cause == "unusable"
    await manager.close()


@pytest.mark.asyncio
async def test_a_file_the_provider_no_longer_has_retires_the_row_and_says_so(tmp_path: Path):
    """One provider listed an entry whose file was gone and said so after its
    own thirty-second countdown, in its own language and with an error code.

    Asking again reads the same however often it is done, so the row is retired
    exactly as a damaged download is, and the user is told which source answered
    rather than shown the source's own code.
    """
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        direct_url="https://www.playground.ru/download/example.CT",
    )
    diagnostics = _Diagnostics()
    catalog = _Catalog(record)
    catalog.diagnostics = diagnostics
    manager = AcquisitionManager(catalog, _GoneNetwork(), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start(record.result.provider, record.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))
    state = manager.poll(str(started["acquisition_id"]))
    assert state["state"] == "failed"
    assert state["artifact_rejected"] is True
    assert "no longer has this file" in str(state["error"])
    # The source that answered is named, and its own wording is not shown.
    assert record.result.provider_display_name in str(state["error"])
    assert "404" not in str(state["error"])
    # It is still a failed download, so the screen that says what each source is
    # doing counts it.
    assert diagnostics.failures and diagnostics.failures[-1][0] == record.result.provider
    assert not list((tmp_path / "tmp" / "provider-downloads").glob("*"))
    await manager.close()


@pytest.mark.asyncio
async def test_a_retired_download_carries_the_game_it_was_started_for(tmp_path: Path):
    """The record is read months later, in a list that spans every game.

    The download knows which game the search was for, and it is the only thing
    that does: the bytes never enter the catalog and a row whose file is gone
    produced no bytes at all. Without it the list showed an archive name and a
    provider key and named no game, so the rows one game's search had just
    retired could not be found in it.
    """
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        direct_url="https://www.playground.ru/download/example.CT",
    )
    catalog = _Catalog(record)
    catalog.diagnostics = _Diagnostics()
    missing: list[tuple] = []
    manager = AcquisitionManager(catalog, _GoneNetwork(), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    manager.record_missing = lambda *args: missing.append(args)
    started = await manager.start(record.result.provider, record.result.artifact_id, None, 42)
    await manager.wait_ready(str(started["acquisition_id"]))

    assert len(missing) == 1
    provider, artifact_id, _reason, app_id, filename = missing[0]
    assert (provider, artifact_id) == (record.result.provider, record.result.artifact_id)
    assert app_id == 42
    # And the file the row offered, so the list stops naming it by its key.
    assert filename == "Example.CT"
    await manager.close()


@pytest.mark.asyncio
async def test_a_game_identity_that_is_not_one_never_fails_a_download(tmp_path: Path):
    # It arrives over the same RPC boundary as everything else and is used for
    # nothing but a display field in a record that may never be written. A
    # download must not fail over it.
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        direct_url="https://www.playground.ru/download/example.CT",
    )
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start(record.result.provider, record.result.artifact_id, None, "not-an-appid")  # type: ignore[arg-type]
    await manager.wait_ready(str(started["acquisition_id"]))
    assert manager.poll(str(started["acquisition_id"]))["state"] == "ready_to_import"
    assert manager.items[str(started["acquisition_id"])].app_id is None
    await manager.close()


@pytest.mark.asyncio
async def test_cancellation_during_playground_countdown_cleans_staging(tmp_path: Path):
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        advertised_sha256=sha256(CT).hexdigest(),
        acquisition=PlaygroundDownloadRequest("2", "1"),
    )
    catalog = _WaitingCatalog(record)
    manager = AcquisitionManager(catalog, _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await catalog.started.wait()
    waiting = manager.poll(started["acquisition_id"])
    assert waiting["state"] == "waiting_provider"
    assert 1 <= waiting["provider_wait_seconds"] <= 60
    cancelled = await manager.cancel(started["acquisition_id"])
    assert cancelled["state"] == "cancelled"
    assert not list((tmp_path / "tmp" / "provider-downloads").glob("*"))
    await manager.close()


@pytest.mark.asyncio
async def test_playground_429_after_the_lock_exchange_records_download_cooldown_inputs(tmp_path: Path):
    """A limit that lands once the one-shot token is spent ends the attempt.

    Playground issues its countdown token once and the countdown has already
    been served by this point, so waiting and asking again would be replaying a
    handshake the provider has closed. This is the one acquisition step that a
    rate limit still fails outright, and it records the provider's own bounded
    `Retry-After` on the way out.
    """
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        acquisition=PlaygroundDownloadRequest("2", "1"),
    )
    catalog = _RateLimitedCatalog(record)
    manager = AcquisitionManager(catalog, _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])
    failed = manager.poll(started["acquisition_id"])
    assert failed["state"] == "failed"
    provider, details = catalog.diagnostics.failures[0]
    assert provider == "playground"
    assert details["http_status"] == 429
    assert details["retry_after"] == "90"
    assert details["download"] is True
    assert catalog.diagnostics.throttles == []
    await manager.close()


@pytest.mark.asyncio
async def test_a_rate_limited_link_resolution_waits_and_then_resolves(tmp_path: Path, monkeypatch):
    """The opening request of a link handshake is a wait like the transfer is.

    Waiting out a 429 used to start at the artifact transfer, which left the
    phase before it, the request that asks a provider for a transient link, on
    the old behavior: the acquisition failed and the only way forward offered
    was pressing the row again later. Nothing has been spent at that point, so
    the same bounded wait applies.
    """
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _record_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(acquisition.asyncio, "sleep", _record_sleep)
    base = _record(filename="Example.CT", size=len(CT)).result
    record = ArtifactRecord(
        CatalogResult(**{**base.as_dict(), "download_mode": "direct_https"}),
        acquisition=PlaygroundDownloadRequest("2", "1"),
    )
    catalog = _RateLimitedOnceCatalog(record)
    manager = AcquisitionManager(catalog, _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)

    while manager.poll(started["acquisition_id"])["state"] in {"downloading", "waiting_provider"}:
        await real_sleep(0)

    assert manager.poll(started["acquisition_id"])["state"] == "ready_to_import"
    assert catalog.attempts == 2
    assert slept == [acquisition.DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS[0]]
    # A wait the acquisition recovered from is not a failed download and is not
    # a persisted cooldown, but it is not nothing either.
    assert catalog.diagnostics.throttles == [
        ("playground", {"wait_seconds": acquisition.DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS[0]}),
    ]
    assert catalog.diagnostics.failures == []
    await manager.close()


@pytest.mark.asyncio
async def test_a_recovered_download_429_is_counted_in_provider_diagnostics(tmp_path: Path, monkeypatch):
    """Advanced could not tell a provider that throttles every transfer from one that does not.

    Only the terminal-failure path wrote anything, so a 429 the download waited
    out and recovered from left the diagnostics saying the file simply arrived,
    while the user had watched a countdown to get it.
    """
    real_sleep = asyncio.sleep

    async def _record_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(acquisition.asyncio, "sleep", _record_sleep)
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT")
    catalog = _DiagnosticCatalog(record)
    manager = AcquisitionManager(catalog, _RateLimitedOnceNetwork(CT, retry_after="30"), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)

    while manager.poll(started["acquisition_id"])["state"] in {"downloading", "waiting_provider"}:
        await real_sleep(0)

    assert manager.poll(started["acquisition_id"])["state"] == "ready_to_import"
    assert catalog.diagnostics.throttles == [("playground", {"wait_seconds": 30})]
    # The download that eventually arrived is still a successful download.
    assert catalog.diagnostics.failures == []
    assert catalog.diagnostics.downloads == [("playground", {"bytes_downloaded": len(CT)})]
    await manager.close()


@pytest.mark.asyncio
async def test_direct_acquisition_digest_mismatch_fails_and_cleans_staging(tmp_path: Path):
    record = ArtifactRecord(_record(filename="Example.CT", size=len(CT)).result, direct_url="https://www.playground.ru/files/Example.CT", advertised_sha256="0" * 64)
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    while manager.poll(started["acquisition_id"])["state"] == "downloading":
        await __import__("asyncio").sleep(0)
    failed = manager.poll(started["acquisition_id"])
    assert failed["state"] == "failed"
    assert not list((tmp_path / "tmp" / "provider-downloads").glob("*"))
    await manager.close()


@pytest.mark.asyncio
async def test_browser_download_is_staged_then_requires_separate_confirmation(tmp_path: Path):
    record = _record(filename="Example.CT", size=len(CT))
    home = tmp_path / "home"
    downloads = home / "Downloads"
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", downloads, home, lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    (downloads / "Example.CT").write_bytes(CT)
    staged = await manager.complete(started["acquisition_id"])
    assert staged["state"] == "ready_to_import"
    assert staged["imported"] is None
    imported = await manager.complete(started["acquisition_id"])
    assert imported["state"] == "imported"
    assert imported["execution_consent"] is False
    await manager.close()


@pytest.mark.asyncio
async def test_browser_search_handoff_requires_picker_and_records_actual_filename(tmp_path: Path):
    base = _record(filename="Downloaded table.CT", size=None)
    record = ArtifactRecord(CatalogResult(**{**base.result.as_dict(), "provider": "fearless", "provider_display_name": "FearLess Cheat Engine", "artifact_id": "browser-search:fixture", "source_page": "https://fearlessrevolution.com/search.php?keywords=example", "download_mode": "file_picker"}))
    home = tmp_path / "home"
    downloads = home / "Downloads"
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", downloads, home, lambda: None)  # type: ignore[arg-type]
    started = await manager.start("fearless", record.result.artifact_id)
    with pytest.raises(ValueError, match="choose the downloaded table explicitly"):
        await manager.complete(started["acquisition_id"])
    actual = downloads / "Actual Table.CT"
    actual.write_bytes(CT)
    staged = await manager.complete(started["acquisition_id"], picked_path=str(actual))
    assert staged["filename"] == "Actual Table.CT"
    imported = await manager.complete(started["acquisition_id"])
    assert imported["imported"]["origins"][0]["original_filename"] == "Actual Table.CT"
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["403", "html"])
async def test_blocked_direct_download_ends_as_a_bounded_provider_failure(tmp_path: Path, mode: str):
    """A challenged automatic row must terminate, not wait for a browser.

    Browser use is not a controller workflow, and the design reports a
    challenged provider as unavailable rather than handing it to the user. An
    in-flight direct download that moved into browser handoff was stranded in a
    state the panel had no forward action for until it was cancelled.
    """
    base = _record(filename="Example.CT", size=None)
    record = ArtifactRecord(base.result, direct_url="https://www.playground.ru/files/Example.CT")
    home = tmp_path / "home"
    manager = AcquisitionManager(_Catalog(record), _BlockedNetwork(mode), TableStore(tmp_path / "tables"), tmp_path / "tmp", home / "Downloads", home, lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    while manager.poll(started["acquisition_id"])["state"] == "downloading":
        await __import__("asyncio").sleep(0)
    blocked = manager.poll(started["acquisition_id"])
    assert blocked["state"] == "failed"
    assert "Local file" in str(blocked["error"])
    await manager.close()


@pytest.mark.asyncio
async def test_damaged_table_fails_the_acquisition_without_blaming_the_provider(tmp_path: Path, caplog):
    logger = logging.getLogger("ce-decky-damaged-table-test")
    caplog.set_level(logging.WARNING, logger=logger.name)
    digest = sha256(DAMAGED_CT).hexdigest()
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(DAMAGED_CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
        advertised_sha256=digest,
    )
    catalog = _Catalog(record)
    catalog.diagnostics = _Diagnostics()
    manager = AcquisitionManager(catalog, _Network(DAMAGED_CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None, logger=logger)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])

    failed = manager.poll(started["acquisition_id"])
    assert failed["state"] == "failed"
    assert "its XML is malformed" in str(failed["error"])
    assert "choose a different table" in str(failed["error"]).casefold()
    assert failed["bytes_received"] == len(DAMAGED_CT)
    assert catalog.diagnostics.failures == []
    assert not list((tmp_path / "tmp" / "provider-downloads").glob("*"))
    assert any("acquisition.artifact_rejected" in message for message in caplog.messages)
    await manager.close()


@pytest.mark.asyncio
async def test_a_worker_that_raises_in_its_own_bookkeeping_still_terminates(tmp_path: Path):
    """No exception may leave an acquisition nonterminal with a dead worker.

    The panel polls this state machine, so an item left `downloading` with no
    task behind it is a row nothing will ever advance.
    """
    record = ArtifactRecord(
        _record(filename="Example.CT", size=None).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]

    async def explode(_item):
        raise RuntimeError("bookkeeping failed")

    manager._download = explode  # type: ignore[assignment]
    started = await manager.start("playground", record.result.artifact_id)
    while manager.poll(started["acquisition_id"])["state"] == "downloading":
        await __import__("asyncio").sleep(0)

    final = manager.poll(started["acquisition_id"])
    assert final["state"] == "failed"
    assert "bookkeeping failed" in str(final["error"])
    await manager.close()


@pytest.mark.asyncio
async def test_an_encrypted_7z_terminates_instead_of_asking_for_a_password(tmp_path: Path, monkeypatch):
    """CE Decky will not transport a 7z password, so it must not solicit one.

    The refusal message contains "password", which routed the acquisition back
    to member selection and kept offering an action no entry can satisfy.
    """
    record = ArtifactRecord(
        _record(filename="Example.CT", size=None).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]

    # Every format 7-Zip takes a password for on its argv, not one of them: the
    # refusal was recognised by a hard-coded `.7z`, so adding `.rar` put that
    # format straight back in front of a password prompt no answer satisfies.
    for archive_format in sorted(ARGV_PASSWORD_FORMATS):
        def refuse(*_args, _kind=archive_format, **_kwargs):
            raise ValueError(argv_password_refusal(_kind))

        started = await manager.start("playground", record.result.artifact_id)
        await manager.wait_ready(started["acquisition_id"])
        monkeypatch.setattr(manager.table_store, "import_selection", refuse)
        final = await manager.complete(started["acquisition_id"])

        assert final["state"] == "failed", archive_format
        assert "does not open" in str(final["error"])
        assert "Local file" in str(final["error"])
    await manager.close()


@pytest.mark.asyncio
async def test_an_archive_import_records_which_table_inside_it_this_is(tmp_path: Path):
    """One archive digest is not the identity of one of several tables in it."""
    record = ArtifactRecord(
        _record(filename="Example.CT", size=None).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(_Catalog(record), _Network(CT), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])
    imported = await manager.complete(started["acquisition_id"])

    origin = store.get_table(imported["imported"]["sha256"])["origins"][-1]
    # A direct `.CT` has no member to choose, so neither field is recorded and
    # its advertised digest remains an unambiguous shortcut.
    assert "member_path" not in origin
    assert "member_count" not in origin
    await manager.close()


@pytest.mark.asyncio
async def test_an_import_records_the_release_it_was_offered_under(tmp_path: Path):
    """An imported table keeps the version search showed, not only its digest."""
    record = ArtifactRecord(
        _record(filename="Example.CT", version="1.0.6").result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(_Catalog(record), _Network(CT), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])
    imported = await manager.complete(started["acquisition_id"])

    assert store.get_table(imported["imported"]["sha256"])["origins"][-1]["version"] == "1.0.6"
    await manager.close()


@pytest.mark.asyncio
async def test_an_import_of_a_table_with_no_advertised_release_records_none(tmp_path: Path):
    record = ArtifactRecord(
        _record(filename="Example.CT").result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(_Catalog(record), _Network(CT), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])
    imported = await manager.complete(started["acquisition_id"])

    assert "version" not in store.get_table(imported["imported"]["sha256"])["origins"][-1]
    await manager.close()


@pytest.mark.asyncio
async def test_an_all_encrypted_7z_archive_is_terminal_before_selection(tmp_path: Path, monkeypatch):
    """A selection with no importable candidate is not a selection.

    Every member being an encrypted 7z leaves nothing the user could choose, and
    the frontend disables the action that would have made the acquisition
    terminal - so `needs_selection` dead-ended with Cancel as the only move.
    """
    record = ArtifactRecord(
        _record(filename="Pack.7z").result,
        direct_url="https://www.playground.ru/files/Pack.7z",
    )
    monkeypatch.setattr(acquisition_module, "_inspect_download", lambda *_args: (
        b"7z", 10,
        {"format": "7z", "members": [
            {"path": "one.CT", "size": 20, "packed_size": 12, "encrypted": True, "format": "7z"},
            {"path": "two.CT", "size": 30, "packed_size": 18, "encrypted": True, "format": "7z"},
        ]},
        None,
    ))
    recorded: list[tuple] = []
    store = TableStore(tmp_path / "tables", record_unusable=lambda *args: recorded.append(args))
    manager = AcquisitionManager(_Catalog(record), _Network(b"archive"), store, tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id, None, 42)
    await manager.wait_ready(started["acquisition_id"])

    state = manager.poll(started["acquisition_id"])
    assert state["state"] == "failed"
    assert "does not open" in str(state["error"])
    # Retrying costs another provider countdown for the same terminal answer.
    assert state["artifact_rejected"] is True

    # And durably, not only for this session: this was the one terminal outcome
    # that wrote nothing down, so the row came back on the next search and was
    # paid for again, and it was the one mark in the search list with no entry
    # behind it in the list the user reads and clears. Its cause is its own,
    # because these bytes may hold a good table and re-packing the archive as a
    # zip is something the user can act on.
    assert len(recorded) == 1
    digest, reason, filename, origins, app_id, cause = recorded[0]
    assert digest == sha256(b"archive").hexdigest()
    assert "does not open" in reason
    assert filename == "Pack.7z"
    assert origins == (f"{record.result.provider}:{record.result.artifact_id}",)
    assert (app_id, cause) == (42, "encrypted")
    await manager.close()


@pytest.mark.asyncio
async def test_a_mixed_archive_still_offers_its_supported_members(tmp_path: Path, monkeypatch):
    """One unusable member must not retire the tables beside it."""
    record = ArtifactRecord(
        _record(filename="Pack.7z").result,
        direct_url="https://www.playground.ru/files/Pack.7z",
    )
    monkeypatch.setattr(acquisition_module, "_inspect_download", lambda *_args: (
        b"7z", 10,
        {"format": "7z", "members": [
            {"path": "locked.CT", "size": 20, "packed_size": 12, "encrypted": True, "format": "7z"},
            {"path": "plain.CT", "size": 30, "packed_size": 18, "encrypted": False, "format": "7z"},
        ]},
        None,
    ))
    manager = AcquisitionManager(_Catalog(record), _Network(b"archive"), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(started["acquisition_id"])

    state = manager.poll(started["acquisition_id"])
    assert state["state"] == "needs_selection"
    assert state["artifact_rejected"] is False
    await manager.close()


@pytest.mark.asyncio
async def test_a_deletion_that_begins_mid_snapshot_still_stops_the_acquisition(tmp_path: Path, monkeypatch):
    """The suspension guard was on the wrong side of an await.

    A browser handoff takes a filesystem snapshot in a worker thread before it
    publishes anything. Checking only before that await let a deletion begin
    while the snapshot ran and an acquisition appear inside the destructive
    transaction, against staging the deletion had already accounted for.
    """
    record = ArtifactRecord(_record(filename="Example.CT").result)
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]

    reached = threading.Event()
    release = threading.Event()

    def blocking_snapshot(root):
        reached.set()
        assert release.wait(timeout=5), "test did not release the snapshot"
        return {}

    monkeypatch.setattr(acquisition_module, "snapshot_downloads", blocking_snapshot)

    started = asyncio.create_task(manager.start("playground", record.result.artifact_id))
    await asyncio.to_thread(reached.wait, 5)
    # The initial guard has already passed; the deletion begins now.
    manager.suspend()
    release.set()

    with pytest.raises(ValueError, match="being deleted"):
        await started
    assert manager.items == {}
    await manager.close()


@pytest.mark.asyncio
async def test_an_unresolved_browser_handoff_owns_staging_it_has_not_created_yet(tmp_path: Path, monkeypatch):
    """Its staging is created by `complete()`, against the snapshot it holds."""
    record = ArtifactRecord(_record(filename="Example.CT").result)
    manager = AcquisitionManager(_Catalog(record), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    monkeypatch.setattr(acquisition_module, "snapshot_downloads", lambda root: {})

    started = await manager.start("playground", record.result.artifact_id)
    assert started["state"] == "browser_handoff"
    # No task, no staged file - and still not something to delete underneath.
    item = manager.items[started["acquisition_id"]]
    assert item.task is None and item.staged_path is None
    assert manager.has_active() is True

    await manager.cancel(started["acquisition_id"])
    assert manager.has_active() is False
    await manager.close()


@pytest.mark.asyncio
async def test_a_provider_unavailable_download_says_why_in_the_diagnostics(tmp_path: Path):
    """The support bundle has to answer this without the device.

    Both terminal paths returned before the generic download bookkeeping, so
    `downloads_failed` never moved, the provider's own last error never
    described it, and the log said the provider had been unavailable without
    saying why, which is the one question a released bug report exists to ask.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    diagnostics = ProviderDiagnosticsStore(tmp_path / "providers.json")
    # A cooldown the provider did ask for, which this failure must not erase.
    diagnostics.record_failure("playground", error="HTTP 429", http_status=429, retry_after="600")
    deadline = diagnostics.snapshot()["providers"]["playground"]["cooldown_until_epoch_s"]

    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    catalog = _Catalog(record)
    catalog.diagnostics = diagnostics
    manager = AcquisitionManager(catalog, _BlockedNetwork("403"), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))
    assert manager.poll(str(started["acquisition_id"]))["state"] == "failed"

    state = diagnostics.snapshot()["providers"]["playground"]
    assert state["counters"]["downloads_failed"] == 1
    assert state["last_http_status"] == 403
    assert "403" in str(state["last_error"])
    # And the deadline the provider named is still the deadline.
    assert state["cooldown_until_epoch_s"] == deadline
    await manager.close()


def test_an_acquisition_the_manager_no_longer_has_says_so_in_one_sentence(tmp_path: Path):
    """The wording is load-bearing, because it is all that crosses the RPC.

    A Decky rejection carries the message and nothing else, and the window that
    owns a download has to tell one answer from every other: the backend saying
    it does not have this acquisition is terminal the moment it arrives, and it
    is also the one answer that must never make that window try again. Poll and
    cancel both raise it, and `TableAcquisitionModal` reads exactly this
    sentence to know that there is nothing left to poll and nothing left to
    cancel, so Close is a way out rather than a second attempt at an object that
    is not there.
    """
    manager = AcquisitionManager(
        _Catalog(None), _Network(CT), TableStore(tmp_path / "tables"), tmp_path / "tmp",
        tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None,
    )  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="acquisition is unknown or expired"):
        manager.poll("0" * 32)
    with pytest.raises(ValueError, match="acquisition is unknown or expired"):
        asyncio.run(manager.cancel("0" * 32))


@pytest.mark.asyncio
async def test_an_html_challenge_records_a_failed_download_without_inventing_a_status(tmp_path: Path):
    from ce_decky.providers import ProviderDiagnosticsStore

    diagnostics = ProviderDiagnosticsStore(tmp_path / "providers.json")
    record = ArtifactRecord(
        _record(filename="Example.CT", size=len(CT)).result,
        direct_url="https://www.playground.ru/files/Example.CT",
    )
    catalog = _Catalog(record)
    catalog.diagnostics = diagnostics
    manager = AcquisitionManager(catalog, _BlockedNetwork("html"), TableStore(tmp_path / "tables"), tmp_path / "tmp", tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)  # type: ignore[arg-type]
    started = await manager.start("playground", record.result.artifact_id)
    await manager.wait_ready(str(started["acquisition_id"]))

    state = diagnostics.snapshot()["providers"]["playground"]
    assert state["counters"]["downloads_failed"] == 1
    assert "web page" in str(state["last_error"])
    # It answered HTTP 200, so no status is invented for it.
    assert state["last_http_status"] is None
    await manager.close()


@pytest.mark.asyncio
async def test_metadata_publication_failure_retains_download_for_retry(tmp_path, monkeypatch):
    import ce_decky.table_store as store_module
    record = ArtifactRecord(_record().result, direct_url="https://www.playground.ru/files/Example.CT")
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(_Catalog(record), _Network(CT), store, tmp_path / "tmp",
        tmp_path / "home" / "Downloads", tmp_path / "home", lambda: None)
    started = await manager.start("playground", record.result.artifact_id)
    acquisition_id = started["acquisition_id"]
    while manager.poll(acquisition_id)["state"] in {"downloading", "waiting_provider"}:
        await asyncio.sleep(0)
    item = manager.items[acquisition_id]
    staged = item.staged_path
    def refuse(*args, **kwargs):
        raise OSError("metadata refused")
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "atomic_write_json", refuse)
        with pytest.raises(OSError, match="metadata refused"):
            await manager.complete(acquisition_id)
    assert manager.poll(acquisition_id)["state"] == "ready_to_import"
    assert staged.read_bytes() == CT
    result = await manager.complete(acquisition_id)
    assert result["state"] == "imported"
    assert not staged.exists()
    await manager.close()
