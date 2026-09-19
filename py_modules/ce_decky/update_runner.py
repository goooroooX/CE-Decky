"""Install one verified CE Decky archive, after this plugin has stopped existing.

Decky replaces the plugin tree and stops its backend as part of the install, so
the process that asks for the install cannot be that backend: it would be killed
in the middle of its own request and could never ask for the frontend reload
that follows. This module is therefore run as a detached child of the backend,
in its own session, and it outlives the plugin that started it.

It decides nothing. The archive it installs was chosen, downloaded and verified
against the release's own checksum file before it was spawned, and it verifies
that digest again off the disk it is about to hand to Decky, because the file
could otherwise have changed between the check and the install. A mismatch
installs nothing.

It leaves two things behind for the backend that comes after it: a result file
saying what happened, which that backend folds into the durable update record
and then deletes, and, when the install failed, the verified archive itself
where the user can reach it, so a failed automatic install still leaves a
working manual route.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import argparse
import json
import os
import shutil
import sys
import time

from .decky_control import (
    DEFAULT_DECKY_URL,
    DeckyWebSocket,
    DeckyWebSocketClosed,
    auth_token,
    install_and_confirm,
    loader_plugin_matches,
    request_frontend_reload,
)

# What one install may cost before it is reported as not having finished.
INSTALL_TIMEOUT_SECONDS = 180.0
# How long Decky's own inventory may take to show the new version. Decky loads
# a replaced plugin more than once, so this is a wait on a settled answer
# rather than on the first one.
READBACK_TIMEOUT_SECONDS = 90.0
READBACK_POLL_SECONDS = 0.5
CONNECT_TIMEOUT_SECONDS = 10.0
# The longest this will hold before installing, to keep Steam's webhelper from
# being replaced twice inside Decky's own tolerance. Three replacements in a
# minute make Decky stop its own service on this project's device, and only
# root can start it again.
MAX_SPACING_WAIT_SECONDS = 120.0
MAX_LOG_BYTES = 256 * 1024


class _Log:
    """A bounded plain-text record of one install, written as it happens.

    Its own file, because the plugin's log belongs to a backend that is about
    to be stopped and replaced: anything written there during the install is
    written by a process Decky is in the middle of killing. The support bundle
    collects this file by name.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._written = 0

    def __call__(self, event: str, **fields: object) -> None:
        line = json.dumps(
            {"at": round(time.time(), 3), "event": event, **fields},
            separators=(",", ":"), sort_keys=True, default=str,
        )
        if self.path is None or self._written >= MAX_LOG_BYTES:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            self._written += len(line) + 1
        except OSError:
            # The install is what matters, and it is not failed over a log.
            self.path = None


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _await_installed_version(decky_url: str, version: str, log: _Log) -> bool:
    """Whether Decky's own inventory settles on this version. Never raises."""
    deadline = time.monotonic() + READBACK_TIMEOUT_SECONDS
    last: str | None = None
    while time.monotonic() < deadline:
        try:
            token = auth_token(decky_url, CONNECT_TIMEOUT_SECONDS)
            with DeckyWebSocket.connect(decky_url, token, CONNECT_TIMEOUT_SECONDS) as ws:
                matches = loader_plugin_matches(ws, 3)
            if len(matches) == 1 and matches[0].get("version") == version and matches[0].get("disabled") is not True:
                return True
            last = f"loader reports {len(matches)} entries, version {matches[0].get('version') if matches else None}"
        except (OSError, RuntimeError) as exc:
            last = str(exc)[:200]
        time.sleep(READBACK_POLL_SECONDS)
    log("update_runner.readback_unsettled", detail=last)
    return False


def _wait_out_spacing(not_before: float, log: _Log) -> None:
    owed = min(max(not_before - time.time(), 0.0), MAX_SPACING_WAIT_SECONDS)
    if owed <= 0:
        return
    log("update_runner.spacing_wait", seconds=round(owed, 1))
    time.sleep(owed)


def _preserve_for_manual_install(
    archive: Path, destination: Path | None, log: _Log, *, verified: bool,
) -> str | None:
    """Keep the verified archive where the user can install it by hand.

    The automatic route has just failed, and the bytes it failed with are known
    good: they carry the digest the release itself states. Throwing them away
    would leave the user with nothing but a download to repeat, so they are
    moved somewhere reachable and the path is reported. One file, replaced by
    the next attempt.

    `verified` is what separates that from the other failure. Bytes that did
    not match the release's digest are not an update, and offering them for a
    manual install would hand the user the one thing every check here exists to
    refuse. They are deleted instead.
    """
    if not verified:
        archive.unlink(missing_ok=True)
        return None
    if destination is None or not archive.is_file():
        return None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            destination.unlink()
        shutil.move(str(archive), str(destination))
        return str(destination)
    except OSError as exc:
        log("update_runner.preserve_failed", detail=str(exc)[:200])
        return None


def run(args: argparse.Namespace) -> dict[str, object]:
    log = _Log(Path(args.log) if args.log else None)
    archive = Path(args.archive)
    keep = Path(args.keep_on_failure) if args.keep_on_failure else None
    result: dict[str, object] = {
        "version": args.version,
        "finished_at": None,
        "ok": False,
        "error": None,
        "restart_requested": False,
        "archive_kept_at": None,
    }
    log("update_runner.started", version=args.version, archive=archive.name, pid=os.getpid())
    verified = False
    try:
        if not archive.is_file() or archive.is_symlink():
            raise RuntimeError("the staged update archive is missing")
        observed = _digest(archive)
        if observed != args.digest:
            raise RuntimeError("the staged update archive no longer matches its verified digest")
        verified = True
        # Decky is handed a `file://` URI and opens the path inside it. A path
        # this cannot represent without percent-encoding is one the loader may
        # open somewhere else or not at all, so it is refused here, where the
        # archive can still be kept for a manual install, rather than discovered
        # as an install that silently did nothing.
        if archive.as_uri()[len("file://"):] != str(archive):
            raise RuntimeError(f"this path cannot be handed to Decky as a file URI: {archive}")
        _wait_out_spacing(args.not_before, log)
        token = auth_token(args.decky_url, CONNECT_TIMEOUT_SECONDS)
        started = time.monotonic()
        try:
            with DeckyWebSocket.connect(args.decky_url, token, INSTALL_TIMEOUT_SECONDS) as ws:
                install_and_confirm(ws, archive.as_uri(), args.version, args.digest, True)
            log("update_runner.install_replied", waited_ms=int((time.monotonic() - started) * 1000))
        except DeckyWebSocketClosed:
            # Decky routinely closes this socket while replacing the very
            # plugin that opened it. The inventory below is what says whether
            # the install happened, and it is the only thing that can.
            log("update_runner.install_socket_closed", waited_ms=int((time.monotonic() - started) * 1000))
        if not _await_installed_version(args.decky_url, args.version, log):
            raise RuntimeError("Decky did not report this plugin at the new version")
        result["ok"] = True
        try:
            token = auth_token(args.decky_url, CONNECT_TIMEOUT_SECONDS)
            with DeckyWebSocket.connect(args.decky_url, token, CONNECT_TIMEOUT_SECONDS) as ws:
                request_frontend_reload(ws)
            result["restart_requested"] = True
        except DeckyWebSocketClosed:
            # Decky closes the calling socket while performing this restart, so
            # a closed socket here is the ordinary outcome of a request that
            # was accepted.
            result["restart_requested"] = True
        except (OSError, RuntimeError) as exc:
            log("update_runner.restart_failed", detail=str(exc)[:200])
        archive.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001 - every outcome is reported, never raised at a dead parent
        result["error"] = str(exc)[:400]
        result["archive_kept_at"] = _preserve_for_manual_install(archive, keep, log, verified=verified)
    result["finished_at"] = time.time()
    log("update_runner.finished", ok=result["ok"], error=result["error"], restart=result["restart_requested"])
    _write_result(Path(args.result), result, log)
    return result


def _write_result(path: Path, result: dict[str, object], log: _Log) -> None:
    """Leave the outcome where the next backend will look for it.

    Written with a temporary file and a rename so the backend that reads it
    never sees half of it, and deliberately not through the update record the
    backend owns: the new backend is loading while this is finishing, and two
    writers of one file is how one of them loses what the other said.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        staging.write_text(json.dumps({"schema": 1, **result}, sort_keys=True), encoding="utf-8")
        os.replace(staging, path)
    except OSError as exc:
        log("update_runner.result_not_written", detail=str(exc)[:200])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install one verified CE Decky archive through Decky's own loader.")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--log")
    parser.add_argument("--keep-on-failure")
    parser.add_argument("--decky-url", default=DEFAULT_DECKY_URL)
    parser.add_argument("--not-before", type=float, default=0.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    return 0 if run(args)["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
