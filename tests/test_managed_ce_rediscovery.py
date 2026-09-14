"""Regressions for how a managed install decides which artifact it may use.

The reviewed URL and its SHA-256 remain the ordinary route. These cover what
happens when that route stops working: what rediscovery is allowed to accept,
what it must refuse, and what it records about the choice afterwards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import zlib
from hashlib import sha256
from pathlib import Path

import pytest

from ce_decky import inno_reader, managed_ce
from ce_decky.authenticode import SignatureFacts
from ce_decky.managed_ce import (
    ManagedCEManager,
    ManagedCERelease,
    _valid_provenance,
    load_release_manifest,
    read_managed_provenance,
)
from ce_decky.network import DownloadDestinationError, NetworkError, NetworkResponse
from ce_decky.paths import PluginPaths

PINNED_URL = "https://cdn.example/f/CheatEngine/2129/CheatEngine77.exe"
ROTATED_URL = "https://cdn.example/f/CheatEngine/2500/CheatEngine77.exe"
OTHER_URL = "https://cdn.example/f/CheatEngine/3000/CheatEngine78.exe"
STUB_URL = "https://helper.example/Gl9OXLQsE.exe"
PAGE_URL = "https://www.cheatengine.org/downloads.php"
KEY = "c" * 64

REVIEWED = b"MZ" + b"\x00" * 4000
OTHER = b"MZ" + b"\xff" * 5000


def _release(**overrides) -> ManagedCERelease:
    defaults = dict(
        visible_version="7.7",
        artifact_filename="CheatEngine77.exe",
        artifact_url=PINNED_URL,
        allowed_hosts=("cdn.example",),
        sha256=sha256(REVIEWED).hexdigest(),
        size=len(REVIEWED),
        max_size=64 * 1024 * 1024,
        silent_arguments=(),
        reviewed_at="2026-08-22",
        reviewed_authenticode_subject="CN=Cheat Engine EZ",
        publisher_key_sha256=KEY,
        rediscovery=managed_ce.FallbackPolicy(
            page_url=PAGE_URL,
            page_hosts=("www.cheatengine.org",),
            max_page_bytes=1024 * 1024,
            max_stub_bytes=32 * 1024 * 1024,
        ),
    )
    defaults.update(overrides)
    return ManagedCERelease(**defaults)


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _stub(url: str) -> bytes:
    payload = url.encode("utf-16le")
    framed = struct.pack("<I", _crc(payload)) + payload
    header = struct.pack("<qB", len(framed), 0)
    block = struct.pack("<I", _crc(header)) + header + framed
    prefix = b"MZ" + b"\x00" * 62
    setup0 = b"Inno Setup Setup Data (6.7.0)".ljust(inno_reader.SETUP_ID_SIZE, b"\x00") + block
    offset = len(prefix) + 64
    fields = struct.pack("<IqqIiqqI", 2, 0, 0, 0, 0, offset, offset + len(setup0), 0)
    table = inno_reader.LOADER_TABLE_ID + fields
    table += struct.pack("<I", _crc(table))
    return prefix + table + setup0


class _Network:
    """Serves the download page and every artifact by exact URL."""

    def __init__(self, *, pinned: bytes | Exception, artifacts: dict[str, bytes], page: str | None = None) -> None:
        self.pinned = pinned
        self.artifacts = artifacts
        self.page = page if page is not None else f'<a href="{STUB_URL}">Download</a>'
        self.downloads: list[str] = []

    async def get(self, url: str, *, allowed_hosts, max_bytes, **_kwargs):
        return NetworkResponse(url, 200, {}, self.page.encode("utf-8"))

    async def download(self, url: str, destination: Path, *, allowed_hosts, max_bytes, **_kwargs):
        self.downloads.append(url)
        if url == PINNED_URL:
            if isinstance(self.pinned, Exception):
                raise self.pinned
            destination.write_bytes(self.pinned)
            return
        destination.write_bytes(self.artifacts[url])


def _manager(tmp_path: Path, network, release: ManagedCERelease) -> ManagedCEManager:
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    manager = ManagedCEManager(
        paths.user_home, paths.ce_root, paths.temp_root, network, logging.getLogger("test"),
    )
    manager.release = release
    manager.release_error = None
    return manager


def _obtain(manager: ManagedCEManager, release: ManagedCERelease, *, force: bool = False):
    work = manager.temp_root / "managed-ce" / "operation"
    work.mkdir(parents=True, exist_ok=True)
    return asyncio.run(manager._obtain_installer("operation", release, work, force=force))


def _signature(monkeypatch, facts: SignatureFacts | Exception) -> None:
    seen: list[bytes | None] = []

    async def fake(path: Path, _key: str, *, data: bytes | None = None) -> SignatureFacts:
        # The artifact is already in memory here; reading it again would double
        # the peak for a file measured in tens of megabytes.
        assert data == path.read_bytes()
        seen.append(data)
        if isinstance(facts, Exception):
            raise facts
        return facts
    monkeypatch.setattr(managed_ce, "verify_publisher", fake)


SIGNED = SignatureFacts(
    subject="CN=Cheat Engine EZ,O=Cheat Engine EZ,C=NL",
    common_name="Cheat Engine EZ",
    key_sha256=KEY,
    digest_algorithm="sha1",
)


def test_the_reviewed_link_is_used_and_rediscovery_never_runs(tmp_path: Path):
    network = _Network(pinned=REVIEWED, artifacts={})
    release = _release()
    effective, provenance, installer = _obtain(_manager(tmp_path, network, release), release)

    assert effective is release
    assert network.downloads == [PINNED_URL]
    assert provenance["source"] == "reviewed-url"
    assert provenance["verified_by"] == "reviewed SHA-256"
    assert provenance["origin_host"] == "cdn.example"
    assert installer.read_bytes() == REVIEWED


def test_an_intact_cached_artifact_is_reused_without_any_download(tmp_path: Path):
    network = _Network(pinned=REVIEWED, artifacts={})
    release = _release()
    manager = _manager(tmp_path, network, release)
    _obtain(manager, release)
    network.downloads.clear()

    _effective, provenance, _installer = _obtain(manager, release)
    assert network.downloads == []
    # Reuse keeps the route that filled the cache rather than inventing one.
    assert provenance["source"] == "reviewed-url"


def test_a_forced_reinstall_refuses_the_cache_and_downloads_again(tmp_path: Path):
    network = _Network(pinned=REVIEWED, artifacts={})
    release = _release()
    manager = _manager(tmp_path, network, release)
    _obtain(manager, release)
    network.downloads.clear()

    _effective, provenance, _installer = _obtain(manager, release, force=True)
    assert network.downloads == [PINNED_URL]
    assert provenance["source"] == "reviewed-url"


def test_a_dead_reviewed_link_rediscovers_the_same_reviewed_artifact(tmp_path: Path, monkeypatch):
    # The ordinary rotation: the link moved, the release did not. The reviewed
    # hash still settles it, so the artifact identity is unchanged.
    _signature(monkeypatch, SIGNED)
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(ROTATED_URL), ROTATED_URL: REVIEWED},
    )
    release = _release()
    effective, provenance, installer = _obtain(_manager(tmp_path, network, release), release)

    assert effective is release
    assert network.downloads == [PINNED_URL, STUB_URL, ROTATED_URL]
    assert provenance["source"] == "rediscovered"
    assert provenance["verified_by"] == "reviewed SHA-256"
    assert provenance["helper_host"] == "helper.example"
    assert installer.read_bytes() == REVIEWED


def test_a_different_publisher_release_is_refused_because_it_cannot_be_extracted(
    tmp_path: Path, monkeypatch,
):
    # The native extractor is pinned to the reviewed artifact's exact format, so
    # no other release can become an installation however trustworthy it is.
    # The signature still decides which refusal the user is told about.
    _signature(monkeypatch, SIGNED)
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(OTHER_URL), OTHER_URL: OTHER},
    )
    release = _release()
    with pytest.raises(managed_ce.PublisherReleaseUnsupported, match="different Cheat Engine release"):
        _obtain(_manager(tmp_path, network, release), release)

    message = managed_ce._managed_setup_error(
        managed_ce.PublisherReleaseUnsupported("different Cheat Engine release")
    )
    assert "came from Cheat Engine's own publisher" in message
    assert "Update CE Decky" in message and "import a Cheat Engine installation" in message
    assert "was kept unchanged" in message
    # The signature proves the publisher, not the ordering, and nothing reads a
    # version out of an artifact it has already refused to parse.
    assert "newer" not in message.lower()


def test_a_different_release_the_publisher_did_not_sign_is_refused(tmp_path: Path, monkeypatch):
    _signature(monkeypatch, ValueError("artifact is signed by a different publisher than the reviewed one"))
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(OTHER_URL), OTHER_URL: OTHER},
    )
    release = _release()
    with pytest.raises(ValueError, match="signature could not be trusted"):
        _obtain(_manager(tmp_path, network, release), release)


def test_a_reviewed_artifact_still_installs_when_the_signature_cannot_be_checked(tmp_path: Path, monkeypatch):
    # openssl may be absent. That must not fail a download the reviewed hash
    # already settled; it only leaves the extra check unrecorded.
    _signature(monkeypatch, ValueError("openssl is unavailable, so the signature cannot be checked"))
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(ROTATED_URL), ROTATED_URL: REVIEWED},
    )
    release = _release()
    _effective, provenance, _installer = _obtain(_manager(tmp_path, network, release), release)

    assert provenance["verified_by"] == "reviewed SHA-256"
    assert provenance["signature_key_sha256"] is None
    assert "openssl is unavailable" in str(provenance["signature_note"])


@pytest.mark.parametrize("failure", [
    OSError("No space left on device"),
    # The production downloader reports a destination it cannot open as a
    # NetworkError subclass, which is exactly the shape that could smuggle a
    # local failure into a decision to contact another site.
    DownloadDestinationError("provider download destination could not be created safely"),
    DownloadDestinationError("provider download destination already exists"),
])
def test_a_local_write_failure_is_reported_instead_of_contacting_the_website(
    tmp_path: Path, failure: Exception,
):
    # Rediscovery exists for a download route that stopped working. A local
    # filesystem failure would fail the same way again, and reaching out to a
    # site the user did not ask about would be the wrong reflex.
    class _Unwritable(_Network):
        async def download(self, url: str, destination: Path, *, allowed_hosts, max_bytes, **_kwargs):
            self.downloads.append(url)
            raise failure

    network = _Unwritable(pinned=REVIEWED, artifacts={})
    release = _release()
    with pytest.raises(type(failure)):
        _obtain(_manager(tmp_path, network, release), release)
    assert network.downloads == [PINNED_URL]


def test_a_cache_never_keeps_provenance_from_an_acquisition_that_did_not_land(
    tmp_path: Path, monkeypatch,
):
    # The cache is content-addressed, so a failed replacement leaves the same
    # bytes behind - but the record beside them would have described the route
    # of a download that never arrived.
    network = _Network(pinned=REVIEWED, artifacts={})
    release = _release()
    manager = _manager(tmp_path, network, release)
    _obtain(manager, release)
    source = manager.ce_root / "installers" / release.sha256 / managed_ce.INSTALLER_SOURCE_NAME
    assert managed_ce._valid_provenance(json.loads(source.read_text(encoding="utf-8")))

    def fail_replace(_source, _target):
        raise OSError("simulated interruption")

    monkeypatch.setattr(managed_ce.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        manager._cache(release, tmp_path / "absent.exe", _provenance_for(release, "rediscovered"))
    assert not source.exists()

    # What survives reads as "route not recorded", never as the wrong route.
    monkeypatch.undo()
    _effective, provenance, _installer = _obtain(manager, release)
    assert provenance["source"] == "cache"


def _provenance_for(release, source: str) -> dict[str, object]:
    return managed_ce._provenance(release, source=source)


def test_a_cache_hit_keeps_the_route_that_actually_filled_it(tmp_path: Path, monkeypatch):
    # Reuse is a timing detail, not a route. Synthesizing provenance here let a
    # rediscovered artifact be shown afterwards as though the reviewed link had
    # served it.
    _signature(monkeypatch, SIGNED)
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(ROTATED_URL), ROTATED_URL: REVIEWED},
    )
    release = _release()
    manager = _manager(tmp_path, network, release)
    _obtain(manager, release)
    network.downloads.clear()

    _effective, provenance, _installer = _obtain(manager, release)
    assert network.downloads == []
    assert provenance["source"] == "rediscovered"
    assert provenance["helper_host"] == "helper.example"


def test_a_cache_with_no_record_says_so_instead_of_naming_the_reviewed_link(tmp_path: Path):
    network = _Network(pinned=REVIEWED, artifacts={})
    release = _release()
    manager = _manager(tmp_path, network, release)
    _obtain(manager, release)
    (manager.ce_root / "installers" / release.sha256 / managed_ce.INSTALLER_SOURCE_NAME).unlink()

    _effective, provenance, _installer = _obtain(manager, release)
    assert provenance["source"] == "cache"


def test_a_failed_download_of_the_current_artifact_names_that_route(tmp_path: Path, monkeypatch):
    # The reviewed link already failed. Repeating its name here would send
    # troubleshooting to the wrong host entirely.
    _signature(monkeypatch, SIGNED)

    class _ArtifactGone(_Network):
        async def download(self, url: str, destination: Path, *, allowed_hosts, max_bytes, **_kwargs):
            self.downloads.append(url)
            if url == ROTATED_URL:
                raise NetworkError("provider download returned HTTP 503")
            return await super().download(
                url, destination, allowed_hosts=allowed_hosts, max_bytes=max_bytes,
            )

    network = _ArtifactGone(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(ROTATED_URL), ROTATED_URL: REVIEWED},
    )
    release = _release()
    with pytest.raises(managed_ce.RediscoveryError) as failure:
        _obtain(_manager(tmp_path, network, release), release)
    message = managed_ce._managed_setup_error(failure.value)
    assert "Current Cheat Engine download" in message
    assert "Reviewed Cheat Engine download" not in message


def test_a_build_without_a_rediscovery_policy_reports_the_original_failure(tmp_path: Path):
    network = _Network(pinned=NetworkError("provider request failed (HTTP 404)"), artifacts={})
    release = _release(rediscovery=None)
    with pytest.raises(NetworkError, match="HTTP 404"):
        _obtain(_manager(tmp_path, network, release), release)
    assert network.downloads == [PINNED_URL]


def test_an_ambiguous_download_page_refuses_rather_than_choosing(tmp_path: Path):
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={},
        page=f'<a href="{STUB_URL}">A</a><a href="https://other.example/Other.exe">B</a>',
    )
    release = _release()
    with pytest.raises(ValueError, match="offers 2 download links"):
        _obtain(_manager(tmp_path, network, release), release)


def test_a_reviewed_link_serving_the_wrong_bytes_also_falls_back(tmp_path: Path, monkeypatch):
    # A rotated path can answer 200 with a parked page rather than 404.
    _signature(monkeypatch, SIGNED)
    network = _Network(
        pinned=b"<html>not an installer</html>",
        artifacts={STUB_URL: _stub(ROTATED_URL), ROTATED_URL: REVIEWED},
    )
    release = _release()
    effective, provenance, _installer = _obtain(_manager(tmp_path, network, release), release)
    assert provenance["source"] == "rediscovered"
    assert effective is release


def test_a_rediscovered_artifact_that_is_not_an_executable_is_refused(tmp_path: Path, monkeypatch):
    _signature(monkeypatch, SIGNED)
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(OTHER_URL), OTHER_URL: b"<html>parked</html>"},
    )
    release = _release()
    with pytest.raises(ValueError, match="not a PE/MZ executable"):
        _obtain(_manager(tmp_path, network, release), release)


def test_no_recorded_provenance_carries_a_download_route(tmp_path: Path, monkeypatch):
    _signature(monkeypatch, SIGNED)
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={STUB_URL: _stub(ROTATED_URL), ROTATED_URL: REVIEWED},
    )
    release = _release()
    _effective, provenance, _installer = _obtain(_manager(tmp_path, network, release), release)
    rendered = str(provenance)
    assert ROTATED_URL not in rendered and STUB_URL not in rendered and PAGE_URL not in rendered
    assert _valid_provenance(provenance)


def test_recorded_provenance_survives_promotion_and_reads_back(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    stage = tmp_path / "stage"
    stage.mkdir()
    executable = stage / "cheatengine-x86_64.exe"
    payload = bytearray(300_000)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\x00\x00"
    executable.write_bytes(bytes(payload))

    release = _release()
    provenance = managed_ce._provenance(release, source="rediscovered")
    installed = managed_ce._promote_installation(
        stage, paths.ce_root, release, provenance=provenance,
    )
    assert read_managed_provenance(Path(installed.root)) == provenance
    # The tree is content-addressed by its own artifact SHA and validates on
    # that alone, without being told which release the manifest currently pins.
    managed_ce.validate_managed_installation(Path(installed.root), paths.ce_root)


def test_a_website_failure_and_an_extraction_failure_are_told_apart(tmp_path: Path):
    # Both mention the installer format. Reporting an extraction failure as
    # though cheatengine.org were at fault would send the user to check a
    # website that answered perfectly well.
    website = managed_ce._managed_setup_error(
        managed_ce.RediscoveryError("download helper names 0 Cheat Engine installers instead of exactly one")
    )
    assert "could not be read from cheatengine.org" in website

    extraction = managed_ce._managed_setup_error(
        RuntimeError("Expected Inno loader revision 1 for this artifact, got 2")
    )
    assert "cheatengine.org" not in extraction
    assert "format this version of CE Decky cannot read" in extraction
    assert "Import a Cheat Engine installation instead" in extraction


@pytest.mark.parametrize("detail, expected", [
    ("provider download destination could not be created safely", "free space on this device"),
    ("provider download destination already exists", "leftover file is in the way"),
])
def test_a_local_storage_failure_is_not_reported_as_a_network_problem(detail, expected):
    # It is a NetworkError subclass, so without its own branch a full or
    # unwritable disk was answered with "check the connection".
    message = managed_ce._managed_setup_error(DownloadDestinationError(detail))
    assert expected in message
    assert "network or TLS" not in message
    assert "Check the network connection" not in message


def test_being_offline_is_not_reported_as_the_website_having_changed():
    # Both download attempts fail when the device has no network, and telling
    # the user cheatengine.org changed would send them to check something fine.
    offline = managed_ce.RediscoveryError("page fetch failed")
    offline.__cause__ = NetworkError("provider host resolution failed")
    message = managed_ce._managed_setup_error(offline)
    assert "cheatengine.org" not in message
    assert "Check the network connection" in message


def test_a_rediscovery_failure_is_reported_as_one_end_to_end(tmp_path: Path):
    network = _Network(
        pinned=NetworkError("provider request failed (HTTP 404)"),
        artifacts={},
        page='<a href="https://a.example/A.exe">A</a><a href="https://b.example/B.exe">B</a>',
    )
    release = _release()
    with pytest.raises(managed_ce.RediscoveryError, match="offers 2 download links"):
        _obtain(_manager(tmp_path, network, release), release)


def test_the_shipped_manifest_pins_a_publisher_key_and_a_rediscovery_policy():
    release = load_release_manifest()
    assert release.publisher_key_sha256 == (
        "c6b1fd6c80b1687e348bf4d08bfc5234ea793f4e832c112b7774a4b959ba98c1"
    )
    assert release.rediscovery is not None
    assert release.rediscovery.page_url == "https://www.cheatengine.org/downloads.php"
    assert release.rediscovery.max_stub_bytes <= release.max_size
