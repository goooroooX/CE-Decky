from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
import io
import os
import re
import tempfile
import stat
import time
import xml.etree.ElementTree as ET
import unicodedata
from typing import Callable
from urllib.parse import parse_qsl, urlsplit

from .archive_import import (
    ArchiveHasNoTable, ArchiveImportError, MAX_ARCHIVE_FILE_BYTES, SEVENZIP_ARCHIVE_FORMATS,
    extract_ct_member, inspect_archive,
)
from .atomic import DurabilityUnknownError, durable_unlink, atomic_write_json, fsync_directory, load_json, read_regular_bytes
from .activity_log import log_activity, log_failure
from .table_blocklist import CAUSE_UNUSABLE, BlockedTableError
from .table_markers import is_auto_assembler_marker, is_embedded_file, is_form_marker, is_lua_marker, local_tag
from .text import utf8_len

# What may be selected here, derived from what the archive layer can actually
# open rather than retyped: `.rar` was added there and stayed missing here, so a
# row the search now offers would have been refused at the import.
ARCHIVE_SUFFIXES = frozenset({".zip", *SEVENZIP_ARCHIVE_FORMATS})
SELECT_A_TABLE_MESSAGE = "select a .CT, .zip, .7z, or .rar file"

MAX_CT_BYTES = 32 * 1024 * 1024



MAX_XML_ELEMENTS = 250_000
MAX_TABLE_METADATA_ENTRIES = 10_000
MAX_ORIGINS = 32
_FORBIDDEN_XML_MARKERS = (b"<!DOCTYPE", b"<!ENTITY")


class TableContentError(ValueError):
    """The bytes are readable but are not a usable Cheat Engine table.

    This separates a damaged or hostile artifact from a transport, storage or
    selection failure, so a caller can report the table itself as unusable
    instead of blaming the provider it came from.
    """


class ArchiveWithoutTable(TableContentError):
    """The archive opened and holds no `.CT` at all.

    A content error like any other, so every caller that retires an artifact
    keeps working unchanged. It is its own type so the one caller that has to
    word this for a user can ask what happened instead of reading the sentence
    back: nothing here is damaged, and saying so sends the user looking for a
    broken upload.
    """


# The on-disk shape of a table's metadata record, in one place.
#
# It was written from three: the dataclass default, the normalizer's output and
# the rewrite `add_origin` performs. The third still said 2 after the other two
# had moved to 3, so an ordinary download took a schema-3 record, kept its new
# field, and stamped it as the schema that does not have one. The reader happens
# to tolerate that, which is exactly what makes it a trap: nothing is visibly
# wrong until something migrates on the marker.
TABLE_METADATA_SCHEMA = 3


@dataclass(frozen=True)
class TableArtifact:
    sha256: str
    filename: str
    size: int
    table_version: str | None
    has_lua: bool
    has_auto_assembler: bool
    has_embedded_files: bool
    executable_content: bool
    entry_count: int
    blob_path: str
    origins: tuple[dict[str, object], ...] = ()
    schema_version: int = TABLE_METADATA_SCHEMA
    # When these bytes first arrived on this device, as a UTC timestamp.
    #
    # A downloaded table already has one: the origin its acquisition wrote
    # carries `retrieved_at`. A table opened from a file has no origin at all,
    # so it had no date of any kind, and a screen listing what is stored could
    # say when half of it arrived. Recorded on the promotion that first stores
    # the bytes and carried forward unchanged by any later one, because a table
    # imported again from a second copy of the same file arrived here once.
    #
    # Absent from metadata written before this was tracked, which reads as None
    # rather than being invented from a file timestamp: when the bytes were
    # written is not when the user chose them.
    imported_at: str | None = None
    # A designed window stored in the table itself, which Cheat Engine
    # instantiates on load and whose controls carry Lua handlers. Absent from
    # metadata written before this was recognised, which reads as false there;
    # the bytes are re-inspected whenever they are imported again.
    has_forms: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class TableStore:
    def __init__(
        self,
        root: Path,
        *,
        assert_importable: Callable[[str, tuple[str, ...]], None] | None = None,
        record_unusable: Callable[[str, str, str, tuple[str, ...]], None] | None = None,
    ):
        self.root = root
        self.blob_root = root / "sha256"
        self.meta_root = root / "metadata"
        self.staging_root = root / ".staging"
        # Asked once per import, with the exact digest of the bytes about to be
        # promoted, and expected to raise for bytes the user already marked as
        # not working. It lives here rather than at each caller because this is
        # the one place every route - local file, archive member, provider
        # download - has the content identity in hand.
        self._assert_importable = assert_importable
        # Called with the digest of bytes that turned out not to be a usable
        # table at all. Content identity is known before the content is
        # validated, which is what lets a damaged download be remembered by what
        # it is rather than by which post it came from.
        #
        # The AppID every route below carries is handed to it and never read
        # here: this store is content-addressed and knows nothing about games.
        # It travels because the record the hook writes is a list a user reads
        # months later, and a file name alone does not say what a table is for.
        self._record_unusable = record_unusable

    def reconcile_orphan_blobs(self, logger) -> None:
        """Recover legacy unlisted bytes at startup without guessing provenance.

        Only real owned digest directories and exact validated CT bytes qualify.
        Existing metadata, including unreadable records, is never overwritten.
        The scan and its diagnostics are bounded; a failure preserves the bytes
        and is retried on the next startup.
        """
        if not self.blob_root.exists():
            return
        try:
            self._ensure_roots(self.blob_root, self.meta_root)
            fsync_directory(self.root)
            count = 0
            recovered_bytes = 0
            with os.scandir(self.blob_root) as shards:
                for shard_count, shard in enumerate(shards, 1):
                    if shard_count > 256:
                        raise ValueError("table blob shard catalog exceeds entry limit")
                    if not re.fullmatch(r"[0-9a-f]{2}", shard.name):
                        continue
                    self._ensure_roots(Path(shard.path), create=False)
                    with os.scandir(shard.path) as entries:
                        for entry in entries:
                            count += 1
                            if count > MAX_TABLE_METADATA_ENTRIES:
                                raise ValueError("table blob reconciliation exceeds entry limit")
                            digest = entry.name
                            if not re.fullmatch(r"[0-9a-f]{64}", digest) or not digest.startswith(shard.name):
                                continue
                            metadata = self.meta_root / f"{digest}.json"
                            if metadata.exists() or metadata.is_symlink():
                                continue
                            try:
                                blob = self._verified_blob_path(digest)
                                if not blob.exists() and not blob.is_symlink():
                                    continue
                                info = blob.lstat()
                                if not stat.S_ISREG(info.st_mode):
                                    raise ValueError("orphan blob is not a regular managed file")
                                size = info.st_size
                                if recovered_bytes + size > 128 * 1024 * 1024:
                                    raise OverflowError("table orphan recovery byte budget exhausted; remaining entries await the next startup")
                                data = read_regular_bytes(blob, max_bytes=min(MAX_CT_BYTES, 128 * 1024 * 1024 - recovered_bytes))
                                recovered_bytes += len(data or b"")
                                if data is None or sha256(data).hexdigest() != digest:
                                    raise ValueError("orphan blob failed SHA-256 verification")
                                _reject_dtd_and_entities_bytes(data)
                                version, lua, assembler, embedded, forms, records = _inspect_xml_bytes(data)
                                artifact = TableArtifact(
                                    sha256=digest, filename=f"Recovered-{digest[:12]}.CT", size=len(data),
                                    table_version=version, has_lua=lua, has_auto_assembler=assembler,
                                    has_embedded_files=embedded, has_forms=forms,
                                    executable_content=lua or assembler or embedded or forms,
                                    entry_count=records, blob_path=str(blob),
                                )
                                atomic_write_json(metadata, artifact.as_dict())
                                log_activity(logger, "info", "table.orphan_recovered", table_sha=digest)
                            except (OSError, ValueError) as exc:
                                log_failure(logger, "table.orphan_recovery_failed", exc, expected=True, table_sha=digest)
        except (OSError, ValueError, OverflowError) as exc:
            log_failure(logger, "table.reconciliation_failed", exc, expected=True)

    def _ensure_roots(self, *paths: Path, create: bool = True) -> None:
        root_abs = self.root.expanduser().absolute()
        root_resolved = self.root.expanduser().resolve(strict=False)
        if root_resolved != root_abs:
            raise ValueError("table store root resolves through a symlink")
        for path in paths:
            absolute = path.expanduser().absolute()
            try:
                absolute.relative_to(root_abs)
            except ValueError as exc:
                raise ValueError("table store path escaped managed root") from exc
            if path.is_symlink():
                raise ValueError(f"table store directory must not be a symlink: {path.name}")
            if path.exists() and not path.is_dir():
                raise ValueError(f"table store directory path is not a directory: {path.name}")
            if not path.exists():
                if not create:
                    raise ValueError(f"table store directory is missing: {path.name}")
                missing = []
                parent = path
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                path.mkdir(parents=True, exist_ok=True)
                for created in reversed(missing):
                    fsync_directory(created.parent)
            if path.is_symlink() or path.resolve(strict=True) != absolute:
                raise ValueError(f"table store directory resolves through a symlink: {path.name}")

    def _blob_to_remove(self, digest: str) -> Path | None:
        """The owned blob entry for a digest, or `None` where there is none left.

        `_verified_blob_path` is the boundary every read uses, and it cannot be
        reused here for one reason: it refuses a directory that is missing, and
        a missing directory is the ordinary state after a removal that got part
        way. So this walks the same components with the same rule and treats
        absence as nothing left to remove rather than as an error.

        The rule is the whole point. Checking only the final `table.CT` for a
        link leaves every directory above it unchecked, and a digest directory
        that is itself a link to somewhere else contains a perfectly ordinary
        file: the leaf is not a symlink, `is_file()` follows the link and says
        yes, and the unlink lands on a file outside this store entirely, which
        is a user's own file in a directory this plugin does not own.
        """
        root = self.root.expanduser().absolute()
        if self.root.expanduser().resolve(strict=False) != root:
            raise ValueError("table store root resolves through a symlink")
        walked = root
        for name in ("sha256", digest[:2], digest):
            walked = walked / name
            if not walked.exists() and not walked.is_symlink():
                # Already gone, wholly or in part. There is nothing here to
                # unlink and nothing to refuse.
                return None
            absolute = walked.expanduser().absolute()
            if walked.is_symlink() or not walked.is_dir() or walked.resolve(strict=True) != absolute:
                raise ValueError(f"table store directory resolves through a symlink: {name}")
        return walked / "table.CT"

    def _verified_blob_path(self, digest: str) -> Path:
        shard = self.blob_root / digest[:2]
        directory = shard / digest
        self._ensure_roots(self.blob_root, shard, directory, create=False)
        return directory / "table.CT"

    def inspect_source(self, source_text: str, *, sevenzip: str | None = None) -> dict[str, object]:
        source = Path(source_text).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise ValueError("selected table source does not exist")
        suffix = source.suffix.lower()
        if suffix == ".ct":
            size = source.stat().st_size
            if size <= 0 or size > MAX_CT_BYTES:
                raise ValueError(f"table size must be between 1 byte and {MAX_CT_BYTES} bytes")
            # Inspect the table structure here rather than at import: a damaged
            # .CT must be reported while the user is still choosing a file or a
            # provider result, not after the acquisition claims it is ready.
            snapshot = read_regular_bytes(source, max_bytes=MAX_CT_BYTES)
            if not snapshot:
                raise ValueError("selected table source could not be read safely")
            _reject_dtd_and_entities_bytes(snapshot)
            _inspect_xml_bytes(snapshot)
            return {
                "format": "ct",
                "members": [{
                    "path": source.name,
                    "size": size,
                    "packed_size": None,
                    "encrypted": False,
                    "format": "ct",
                }],
            }
        if suffix in ARCHIVE_SUFFIXES:
            try:
                return inspect_archive(source, sevenzip).as_dict()
            except ArchiveHasNoTable as exc:
                # These exact bytes hold no table and never will, which is the
                # same answer as a damaged one: the caller retires the row
                # rather than offering the download again.
                raise ArchiveWithoutTable(str(exc)) from exc
            except ArchiveImportError as exc:
                raise ValueError(str(exc)) from exc
        raise ValueError(SELECT_A_TABLE_MESSAGE)

    def import_selection(
        self,
        source_text: str,
        *,
        member_path: str | None = None,
        password: str | None = None,
        sevenzip: str | None = None,
        source_filename: str | None = None,
        origins: tuple[str, ...] = (),
        app_id: int | None = None,
    ) -> TableArtifact:
        """Import a table, remembering which provider row it was served from.

        `origins` is the provider rows to record if these bytes turn out not to
        be a table at all. A member extracted from an archive is validated and
        deleted here, so nothing downstream can discover where it came from, and
        a provider result almost never advertises the digest of a file inside an
        archive: without this the record existed and search could not recognise
        the post that produced it, so the same archive was offered, downloaded
        and refused again. A local import passes none, because nothing offered
        it and there is no row to recognise.
        """
        source = Path(source_text).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise ValueError("selected table source does not exist or is not a regular file")
        if source.suffix.lower() == ".ct":
            if member_path not in {None, "", source.name}:
                raise ValueError("member_path is not valid for a direct .CT file")
            return self.import_ct(str(source), display_filename=source_filename, origins=origins, app_id=app_id)

        if source.suffix.lower() not in ARCHIVE_SUFFIXES:
            raise ValueError(SELECT_A_TABLE_MESSAGE)
        source_identity = _source_identity(source)
        self._ensure_roots(self.staging_root)
        archive_snapshot: Path | None = None
        staged: Path | None = None
        try:
            archive_snapshot, _, _ = _snapshot_source(
                source,
                self.staging_root,
                max_bytes=MAX_ARCHIVE_FILE_BYTES,
                prefix=".archive.",
                suffix=source.suffix,
                field="archive source",
            )
            try:
                inspection = inspect_archive(archive_snapshot, sevenzip).as_dict()
            except ArchiveHasNoTable as exc:
                raise ArchiveWithoutTable(str(exc)) from exc
            except ArchiveImportError as exc:
                raise ValueError(str(exc)) from exc
            members = inspection["members"]
            assert isinstance(members, list)
            if member_path is None or not member_path.strip():
                if len(members) != 1:
                    raise ValueError("archive contains multiple .CT files; choose one archive member")
                selected = str(members[0]["path"])
            else:
                selected = member_path.strip()
                if sum(1 for item in members if item.get("path") == selected) != 1:
                    raise ValueError("selected archive member is not a listed .CT candidate")

            fd, temp_name = tempfile.mkstemp(prefix=".archive-ct.", suffix=".CT", dir=self.staging_root)
            os.close(fd)
            staged = Path(temp_name)
            staged.unlink(missing_ok=True)
            try:
                extract_ct_member(
                    archive_snapshot,
                    selected,
                    staged,
                    sevenzip=sevenzip,
                    password=password,
                )
            except ArchiveImportError as exc:
                raise ValueError(str(exc)) from exc
            if _source_identity(source) != source_identity:
                raise ValueError("archive source changed during import; retry from a stable file")
            return self._validate_and_promote(staged, PurePosixPath(selected).name, origins, app_id)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            if archive_snapshot is not None:
                archive_snapshot.unlink(missing_ok=True)

    def import_ct(
        self, source_text: str, *, display_filename: str | None = None, origins: tuple[str, ...] = (),
        app_id: int | None = None,
    ) -> TableArtifact:
        source = Path(source_text).expanduser().absolute()
        if source.is_symlink() or not source.is_file():
            raise ValueError("selected table does not exist or is not a regular file")
        if source.suffix.lower() != ".ct":
            raise ValueError("only .CT files are accepted by import_ct")

        self._ensure_roots(self.staging_root)
        staged: Path | None = None
        try:
            staged, _, _ = _snapshot_source(source, self.staging_root)
            artifact = self._validate_and_promote(staged, display_filename or source.name, origins, app_id)
            staged = None
            return artifact
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)

    def note_unusable_bytes(
        self, digest: str, reason: str, filename: str, origins: tuple[str, ...] = (),
        app_id: int | None = None, cause: str = CAUSE_UNUSABLE,
    ) -> None:
        """Record bytes that are not a usable table but were never promoted.

        A provider download is inspected where it was staged and deleted there
        when it turns out not to be a table, so it never reaches the import that
        would otherwise record it. Without this the most ordinary bad download
        is the one case the record misses.

        The caller passes the provider rows the bytes came from, because these
        bytes never enter the catalog and nothing else can discover them later.
        Without them the record exists but search cannot recognise the row that
        produced it, which is the one place it needs to.
        """
        if self._record_unusable is None:
            return
        self._record_unusable(_normalize_digest(digest), reason, filename, origins, app_id, cause)

    def _validate_and_promote(
        self, staged: Path, filename: str, origins: tuple[str, ...] = (),
        app_id: int | None = None,
    ) -> TableArtifact:
        filename = _display_filename(filename)
        if not staged.is_file():
            raise ValueError("staged table is missing")
        try:
            snapshot = read_regular_bytes(staged, max_bytes=MAX_CT_BYTES)
        except ValueError as exc:
            raise ValueError("staged table could not be read safely") from exc
        assert snapshot is not None
        size = len(snapshot)
        if size <= 0 or size > MAX_CT_BYTES:
            raise ValueError(f"table size must be between 1 byte and {MAX_CT_BYTES} bytes")
        digest = sha256(snapshot).hexdigest()
        refusal = None
        if self._assert_importable is not None:
            try:
                self._assert_importable(digest, origins)
            except BlockedTableError as exc:
                if exc.entry.cause == CAUSE_UNUSABLE:
                    raise
                # A failed cheat is usable table identity only after structural
                # validation. Keep the advisory, but never cache damaged bytes
                # as a resolved CT just because a user named their digest.
                refusal = exc
        try:
            _reject_dtd_and_entities_bytes(snapshot)
            table_version, has_lua, has_auto_assembler, has_embedded_files, has_forms, entry_count = _inspect_xml_bytes(snapshot)
        except TableContentError as exc:
            # These exact bytes are not a table and never will be. Remembering
            # that by content is what stops the same file being found, paid for
            # with another provider countdown and downloaded again.
            if self._record_unusable is not None:
                self._record_unusable(digest, str(exc), filename, origins, app_id, CAUSE_UNUSABLE)
            raise

        if refusal is not None:
            raise refusal

        self._ensure_roots(self.blob_root, self.meta_root)
        shard_root = self.blob_root / digest[:2]
        self._ensure_roots(shard_root)
        blob_dir = shard_root / digest
        self._ensure_roots(blob_dir)
        blob_path = blob_dir / "table.CT"
        metadata_path = self.meta_root / f"{digest}.json"
        # Retry can encounter directories created before an earlier sync failed.
        # Commit their entries too, not just the eventual files inside them.
        for parent in (self.root.parent, self.root, self.blob_root, shard_root):
            fsync_directory(parent)

        origins: tuple[dict[str, object], ...] = ()
        # When these bytes first arrived here, kept across every later import of
        # the same file: a second copy of one table is not a second arrival.
        imported_at: str | None = None
        if metadata_path.is_file() and not metadata_path.is_symlink():
            try:
                previous = _normalize_table_metadata(load_json(metadata_path, None, max_bytes=256 * 1024), digest)
                origins = tuple(previous.get("origins", []))
                kept = previous.get("imported_at")
                imported_at = kept if isinstance(kept, str) else None
            except (OSError, ValueError, TypeError):
                origins = ()
        if imported_at is None:
            imported_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        artifact = TableArtifact(
            sha256=digest,
            filename=filename,
            size=size,
            table_version=table_version,
            has_lua=has_lua,
            has_auto_assembler=has_auto_assembler,
            has_embedded_files=has_embedded_files,
            has_forms=has_forms,
            executable_content=has_lua or has_auto_assembler or has_embedded_files or has_forms,
            entry_count=entry_count,
            blob_path=str(blob_path),
            origins=origins,
            imported_at=imported_at,
        )
        atomic_write_json(metadata_path, artifact.as_dict())
        # Publish the recoverable catalogue entry before any blob mutation.
        # A crash leaves an unavailable row, never unnamed durable bytes.
        try:
            if blob_path.is_symlink():
                # Managed content-addressed blobs must never resolve through a link,
                # even if the link target happens to contain matching bytes. Replace
                # the directory entry with the verified staged regular file.
                blob_path.unlink()
            if blob_path.exists():
                if not blob_path.is_file() or blob_path.stat().st_size != size or _sha256_file(blob_path, max_bytes=MAX_CT_BYTES) != digest:
                    os.replace(staged, blob_path)
                    fsync_directory(blob_dir)
                else:
                    staged.unlink(missing_ok=True)
            else:
                os.replace(staged, blob_path)
                fsync_directory(blob_dir)

            if blob_path.stat().st_size != size or _sha256_file(blob_path, max_bytes=MAX_CT_BYTES) != digest:
                durable_unlink(blob_path, missing_ok=True)
                raise ValueError("content-addressed table promotion failed integrity verification")
        except (OSError, ValueError) as exc:
            raise DurabilityUnknownError(
                f"table {digest} metadata was published but blob promotion did not finish; import again or remove the catalogue entry"
            ) from exc

        return artifact

    def add_origin(self, digest: str, origin: dict[str, object]) -> dict[str, object]:
        normalized = _normalize_digest(digest)
        metadata_path = self.meta_root / f"{normalized}.json"
        current = self.get_table(normalized)
        checked = _normalize_origin(origin)
        origins = list(current.get("origins", []))
        identity = (checked["provider"], checked["artifact_id"], checked["source_page"], checked["original_filename"])
        origins = [row for row in origins if (row["provider"], row["artifact_id"], row["source_page"], row["original_filename"]) != identity]
        origins.append(checked)
        origins = origins[-MAX_ORIGINS:]
        stored = {key: value for key, value in current.items() if key not in {"available", "blob_path"}}
        stored["schema_version"] = TABLE_METADATA_SCHEMA
        stored["blob_path"] = str(self.blob_root / normalized[:2] / normalized / "table.CT")
        stored["origins"] = origins
        atomic_write_json(metadata_path, stored)
        return self.get_table(normalized)

    def verified_blob(self, digest: str) -> Path:
        """Resolve a content-addressed table only after exact SHA-256 verification.

        UI listing intentionally uses cheap presence/size checks. Any future execution
        path MUST call this method immediately before passing a table to Cheat Engine.
        """
        normalized = _normalize_digest(digest)
        blob = self._verified_blob_path(normalized)
        if not blob.is_file() or blob.is_symlink():
            raise ValueError("table blob is missing or is not a regular managed file")
        if _sha256_file(blob, max_bytes=MAX_CT_BYTES) != normalized:
            raise ValueError("table blob failed SHA-256 verification")
        return blob

    def get_table(self, digest: str) -> dict[str, object]:
        normalized = _normalize_digest(digest)
        self._ensure_roots(self.meta_root)
        metadata = self.meta_root / f"{normalized}.json"
        if not metadata.is_file() or metadata.is_symlink():
            raise ValueError("table metadata is missing")
        try:
            raw = load_json(metadata, None, max_bytes=256 * 1024)
        except (OSError, ValueError) as exc:
            raise ValueError("table metadata is unreadable") from exc
        item = _normalize_table_metadata(raw, normalized)
        expected_blob = self.blob_root / normalized[:2] / normalized / "table.CT"
        item["blob_path"] = str(expected_blob)
        item["available"] = _blob_matches_metadata(expected_blob, int(item["size"]))
        return item

    def delete_table(self, digest: str) -> dict[str, object]:
        """Remove one table's bytes and its record, on an explicit user press.

        Content-addressed and irreversible: the same bytes can be imported again
        from a file or a provider, and on a device with neither they are gone.
        Nothing here decides whether that is allowed, because this store knows
        nothing about games; the caller checks that no game is using the table
        and is the one that can say which.

        The bytes go first and the record last, which is the order the two
        failures decide. A record with no blob beside it lists as a table whose
        file is missing, which the panel already describes and the user can act
        on; a blob with no record is invisible to every route here and is
        storage nobody can name, reach or remove. Removing the record first put
        a failure between the two exactly on the way to the second, and left a
        retry answering that there is no such table while the bytes were still
        on the disk.

        Each step is therefore also safe to repeat. A blob already gone is not
        an error, so a delete that failed at the record can be finished by
        pressing again.
        """
        normalized = _normalize_digest(digest)
        self._ensure_roots(self.meta_root)
        metadata = self.meta_root / f"{normalized}.json"
        if not metadata.is_file() or metadata.is_symlink():
            raise ValueError("no such table is stored")
        size = 0
        try:
            size = int(_normalize_table_metadata(load_json(metadata, None, max_bytes=256 * 1024), normalized)["size"])
        except (OSError, ValueError):
            # An unreadable record is still a record to remove: refusing here
            # would make a corrupt entry the one thing that can never be tidied.
            size = 0
        shard = self.blob_root / normalized[:2]
        directory = shard / normalized
        # Every component checked, not only the last. The owned directory entry
        # is this store's to remove; whatever is at the other end of a link is
        # not, and following one would delete a file the user owns elsewhere.
        blob = self._blob_to_remove(normalized)
        if blob is not None:
            if blob.is_dir() and not blob.is_symlink():
                # A directory where the blob belongs is not this store's shape
                # and is not something to remove blindly. Refusing leaves the
                # record, which is what keeps the table nameable.
                raise ValueError("the stored table's path is not a managed table file")
            if blob.is_symlink() or blob.exists():
                durable_unlink(blob)
            else:
                # A retry must commit an earlier unlink before losing its record.
                fsync_directory(blob.parent)
        else:
            durable_unlink(directory / "table.CT", missing_ok=True)
        try:
            durable_unlink(metadata)
        except OSError as exc:
            raise DurabilityUnknownError(
                f"table {normalized} bytes are absent; metadata removal did not finish durably: {exc}"
            ) from exc
        for path in (directory, shard):
            try:
                path.rmdir()
            except OSError:
                # Not empty, or already gone. Either is fine: the shard is
                # shared with every digest that starts the same way.
                break
        return {"sha256": normalized, "size": size}

    def list_tables(self) -> list[dict[str, object]]:
        return self.list_tables_with_errors()[0]

    def list_tables_with_errors(self) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
        """Return usable entries plus bounded corruption diagnostics."""
        if not self.meta_root.exists():
            return [], []
        self._ensure_roots(self.meta_root)
        paths: list[Path] = []
        try:
            with os.scandir(self.meta_root) as entries:
                count = 0
                for entry in entries:
                    count += 1
                    if count > MAX_TABLE_METADATA_ENTRIES:
                        raise ValueError("table metadata catalog exceeds entry limit")
                    if entry.name.endswith(".json"):
                        paths.append(Path(entry.path))
        except OSError as exc:
            raise ValueError(f"table metadata catalog is unreadable: {exc}") from exc
        items: list[dict[str, object]] = []
        errors: list[dict[str, str]] = []
        for path in sorted(paths, key=lambda item: item.name):
            try:
                if path.is_symlink():
                    continue
                digest = _normalize_digest(path.stem)
                raw = load_json(path, None, max_bytes=256 * 1024)
                item = _normalize_table_metadata(raw, digest)
                expected_blob = self.blob_root / digest[:2] / digest / "table.CT"
                item["blob_path"] = str(expected_blob)
                item["available"] = _blob_matches_metadata(expected_blob, int(item["size"]))
                items.append(item)
                # The table is listed and usable; say that some of its recorded
                # provenance was not, because a source whose metadata stops
                # being readable is otherwise invisible.
                stored_origins = raw.get("origins") if isinstance(raw, dict) else None
                dropped = len(stored_origins) - len(item["origins"]) if isinstance(stored_origins, list) else 0
                if dropped > 0 and len(errors) < 64:
                    errors.append({"path": path.name, "error": f"{dropped} unreadable origin entry(s) dropped; the table is unaffected"})
            except (OSError, ValueError, TypeError) as exc:
                if len(errors) < 64:
                    errors.append({"path": path.name, "error": str(exc)[:512]})
        return items, errors


def _normalize_table_metadata(raw: object, digest: str) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("table metadata root is invalid")
    allowed = {
        "schema_version", "sha256", "filename", "size", "table_version",
        "has_lua", "has_auto_assembler", "has_embedded_files", "has_forms",
        "executable_content", "entry_count", "blob_path", "origins",
        "imported_at",
    }
    if set(raw) - allowed:
        raise ValueError("table metadata contains unknown fields")
    if raw.get("sha256") != digest:
        raise ValueError("table metadata identity mismatch")
    schema = raw.get("schema_version", 1)
    if isinstance(schema, bool) or not isinstance(schema, int) or schema not in {1, 2, TABLE_METADATA_SCHEMA}:
        raise ValueError("unsupported table metadata schema")
    filename = _display_filename(raw.get("filename"))
    size = raw.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or size > MAX_CT_BYTES:
        raise ValueError("table metadata size is invalid")
    version = raw.get("table_version")
    if version is not None:
        if not isinstance(version, str) or any(ch in version for ch in ("\x00", "\r", "\n")) or utf8_len(version, "table metadata version") > 256:
            raise ValueError("table metadata version is invalid")
    flags: dict[str, bool] = {}
    for key in ("has_lua", "has_auto_assembler", "has_embedded_files", "has_forms"):
        value = raw.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"table metadata {key} must be boolean")
        flags[key] = value
    explicit_exec = raw.get("executable_content")
    if explicit_exec is not None and not isinstance(explicit_exec, bool):
        raise ValueError("table metadata executable_content must be boolean")
    entry_count = raw.get("entry_count", 0)
    if isinstance(entry_count, bool) or not isinstance(entry_count, int) or entry_count < 0 or entry_count > MAX_XML_ELEMENTS:
        raise ValueError("table metadata entry_count is invalid")
    executable = bool(explicit_exec) or any(flags.values())
    raw_origins = raw.get("origins", [])
    if not isinstance(raw_origins, list) or len(raw_origins) > MAX_ORIGINS:
        raise ValueError("table metadata origins are invalid")
    # An origin is provider-described history about where these bytes came from.
    # The bytes themselves are verified independently, so an entry that cannot
    # be validated is dropped and the table stays listed and usable: refusing
    # the whole entry hid a stored, verified table behind a catalog error.
    origins = []
    for item in raw_origins:
        try:
            origins.append(_normalize_origin(item))
        except (ValueError, TypeError):
            continue
    imported_at = raw.get("imported_at")
    if imported_at is not None and not _is_timestamp(imported_at):
        # An unreadable date is dropped rather than refusing the record: it is a
        # thing a row says, never a thing a table's identity rests on.
        imported_at = None
    return {
        "schema_version": TABLE_METADATA_SCHEMA,
        "sha256": digest,
        "filename": filename,
        "size": size,
        "table_version": version,
        **flags,
        "executable_content": executable,
        "entry_count": entry_count,
        "origins": origins,
        "imported_at": imported_at,
    }


_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _is_timestamp(value: object) -> bool:
    """A bounded UTC stamp of the shape this store writes, and nothing else."""
    return isinstance(value, str) and bool(_TIMESTAMP_RE.fullmatch(value))


def _normalize_origin(raw: object) -> dict[str, object]:
    required = {
        "provider", "artifact_id", "topic_id", "source_page", "original_filename", "retrieved_at", "advertised_sha256"
    }
    # `member_path` and `member_count` describe which table inside an artifact
    # this is, so an archive digest is not mistaken for the identity of one of
    # several tables it holds. Origins written before they existed omit both.
    # `version` is the release the publisher gave these exact bytes, which is
    # what search offered them under and the only thing telling one revision of
    # a table apart from the next. Origins written before it existed omit it.
    optional = {"member_path", "member_count", "version"}
    if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - required - optional:
        raise ValueError("table origin metadata is invalid")
    for key in ("provider", "artifact_id", "topic_id"):
        value = raw[key]
        if not isinstance(value, str) or not value or any(ord(ch) < 0x20 for ch in value) or utf8_len(value, f"origin {key}") > 4096:
            raise ValueError(f"table origin {key} is invalid")
    source_page = raw["source_page"]
    if not isinstance(source_page, str) or not source_page.startswith("https://") or utf8_len(source_page, "origin source page") > 8192:
        raise ValueError("table origin source page is invalid")
    sensitive = {"token", "signature", "sig", "x-amz-signature", "key", "auth", "sid"}
    if any(key.casefold() in sensitive for key, _ in parse_qsl(urlsplit(source_page).query, keep_blank_values=True)):
        raise ValueError("table origin source page contains a signed or session query")
    filename = _display_filename(raw["original_filename"])
    retrieved = raw["retrieved_at"]
    if not _is_timestamp(retrieved):
        raise ValueError("table origin retrieval time is invalid")
    advertised = raw["advertised_sha256"]
    if advertised is not None:
        advertised = _normalize_digest(advertised)
    member_path = raw.get("member_path")
    if member_path is not None:
        if not isinstance(member_path, str) or not member_path or utf8_len(member_path, "origin member path") > 4096:
            raise ValueError("table origin member path is invalid")
        if any(ord(ch) < 0x20 for ch in member_path):
            raise ValueError("table origin member path is invalid")
    member_count = raw.get("member_count")
    if member_count is not None:
        if isinstance(member_count, bool) or not isinstance(member_count, int) or member_count < 0 or member_count > 65535:
            raise ValueError("table origin member count is invalid")
    version = raw.get("version")
    if version is not None:
        if not isinstance(version, str) or not version.strip() or utf8_len(version, "origin version") > 256:
            raise ValueError("table origin version is invalid")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in version):
            raise ValueError("table origin version is invalid")
    normalized = {
        "provider": raw["provider"], "artifact_id": raw["artifact_id"], "topic_id": raw["topic_id"],
        "source_page": source_page, "original_filename": filename, "retrieved_at": retrieved,
        "advertised_sha256": advertised,
    }
    if member_path is not None:
        normalized["member_path"] = member_path
    if member_count is not None:
        normalized["member_count"] = member_count
    if version is not None:
        normalized["version"] = version
    return normalized


def _display_filename(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("table filename must be a string")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or Path(text).name != text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("table filename is invalid")
    if "\x00" in text or any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in text):
        raise ValueError("table filename contains unsafe control characters")
    if utf8_len(text, "table filename") > 1024:
        raise ValueError("table filename is too long")
    return text


def _blob_matches_metadata(blob: Path, expected_size: int) -> bool:
    try:
        return blob.is_file() and not blob.is_symlink() and blob.stat().st_size == expected_size
    except OSError:
        return False


def _source_identity(source: Path) -> tuple[int, int, int, int, int]:
    try:
        info = source.stat()
    except OSError as exc:
        raise ValueError(f"table source is unreadable: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("table source must be a regular file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _snapshot_source(
    source: Path,
    staging_root: Path,
    *,
    max_bytes: int = MAX_CT_BYTES,
    prefix: str = ".ct.",
    suffix: str = ".part",
    field: str = "table source",
) -> tuple[Path, str, int]:
    """Read one stable regular-file descriptor into a bounded local snapshot."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("snapshot byte limit is invalid")
    fd, temp_name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=staging_root)
    staged = Path(temp_name)
    digest = sha256()
    total = 0
    src_fd = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            src_fd = os.open(source, flags)
        except OSError as exc:
            raise ValueError(f"{field} could not be opened safely") from exc
        before = os.fstat(src_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{field} must be a regular file")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise ValueError(f"{field} size must be between 1 byte and {max_bytes} bytes")
        with os.fdopen(src_fd, "rb", closefd=False) as src, os.fdopen(fd, "wb") as dst:
            fd = -1
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"{field} size must be between 1 byte and {max_bytes} bytes")
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(src_fd)
        try:
            path_after = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"{field} changed while being snapshotted; retry from a stable file") from exc
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if (
            identity_after != identity_before
            or (path_after.st_dev, path_after.st_ino) != (before.st_dev, before.st_ino)
            or total != before.st_size
        ):
            raise ValueError(f"{field} changed while being snapshotted; retry from a stable file")
        return staged, digest.hexdigest(), total
    except Exception:
        if fd >= 0:
            os.close(fd)
        staged.unlink(missing_ok=True)
        raise
    finally:
        if src_fd >= 0:
            os.close(src_fd)


def _inspect_xml_bytes(data: bytes) -> tuple[str | None, bool, bool, bool, bool, int]:
    """Read only bounded metadata from an untrusted CT snapshot without building a full DOM."""
    version: str | None = None
    has_lua = False
    has_auto_assembler = False
    has_embedded_files = False
    has_forms = False
    entry_count = 0
    element_count = 0
    root_seen = False
    # The tag of every element still open, so a window can be recognised from
    # the container it sits in without building the DOM this read exists to
    # avoid. Only the innermost one is ever asked for, and start and end events
    # are balanced, so this is the depth of the document rather than its size.
    open_tags: list[str] = []
    try:
        for event, element in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = local_tag(element.tag)
            if event == "start":
                element_count += 1
                if element_count > MAX_XML_ELEMENTS:
                    raise TableContentError(f".CT XML exceeds element limit ({MAX_XML_ELEMENTS})")
                if not root_seen:
                    root_seen = True
                    if element.tag != "CheatTable":
                        raise TableContentError("the file is not a Cheat Engine table: its XML root is not CheatTable")
                    version = element.attrib.get("CheatEngineTableVersion")
                parent_tag = open_tags[-1] if open_tags else None
                open_tags.append(tag)
                if is_form_marker(tag, parent_tag):
                    has_forms = True
                elif is_embedded_file(tag):
                    has_embedded_files = True
                elif tag == "CheatEntry":
                    entry_count += 1
            else:
                if open_tags:
                    open_tags.pop()
                # A script is a marker because of what it carries, and an
                # element's text is only complete at its end event. Review's
                # inspector has always read it that way, and this is the same
                # question about the same bytes.
                if is_lua_marker(tag, element.text):
                    has_lua = True
                elif is_auto_assembler_marker(tag, element.text):
                    has_auto_assembler = True
                element.clear()
    except ET.ParseError as exc:
        raise TableContentError(f"the file is not a valid Cheat Engine table: its XML is malformed ({exc})") from exc
    if not root_seen:
        raise TableContentError("the file contains no Cheat Engine table XML")
    return version, has_lua, has_auto_assembler, has_embedded_files, has_forms, entry_count


def _reject_dtd_and_entities(path: Path) -> None:
    """CE tables do not need a DTD; rejecting it removes XML entity-expansion ambiguity."""
    data = read_regular_bytes(path, max_bytes=MAX_CT_BYTES)
    assert data is not None
    _reject_dtd_and_entities_bytes(data)


def _reject_dtd_and_entities_bytes(data: bytes) -> None:
    probe = data.upper()
    if any(marker in probe for marker in _FORBIDDEN_XML_MARKERS):
        raise TableContentError(".CT XML DTD/entity declarations are not accepted")


def _normalize_digest(digest: str) -> str:
    if not isinstance(digest, str):
        raise ValueError("table SHA-256 must be a string")
    normalized = digest.strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise ValueError("table SHA-256 must be a 64-character hexadecimal digest")
    return normalized


def _sha256_file(path: Path, *, max_bytes: int = MAX_CT_BYTES) -> str:
    digest = sha256()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError("table file could not be opened safely for hashing") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("table file must be a regular file")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise ValueError("table file size is outside the supported bound")
        total = 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("table file size is outside the supported bound")
            digest.update(chunk)
        after = os.fstat(fd)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if before_identity != after_identity or total != after.st_size:
            raise ValueError("table file changed while hashing")
    finally:
        os.close(fd)
    return digest.hexdigest()
