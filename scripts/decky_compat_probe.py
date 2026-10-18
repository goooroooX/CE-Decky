#!/usr/bin/env python3
"""Probe an exact plugin ZIP in an exact Decky executable, without a service.

Unknown runtime binaries run only on disposable GitHub CI. A local device may
use the reviewed supported digest; --python is an ordinary-interpreter control.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import zipfile

if sys.platform == "linux":
    import resource

try:
    from scripts.target_package_probe import inspect_package
except ModuleNotFoundError:
    from target_package_probe import inspect_package

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "scripts" / "decky_compat_pin.json"
REQUIRED_CHECKS = {"entrypoint", "async_backend", "parsers_archives", "offline_table_state",
                   "state_access_refusals", "async_http_download_threads", "async_child_and_worker_drain"}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))


def probe(plugin: Path, runtime: Path, *, expected_sha: str, version: str,
          python_control: bool = False, timeout: float = 50) -> dict:
    result = {"schema": 1, "status": "INCOMPLETE", "decky_version": version,
              "probe_sha256": digest(Path(__file__)),
              "payload_sha256": digest(Path(__file__).with_name("decky_compat_payload.py")),
              "runner_image": {"os": os.environ.get("ImageOS", sys.platform),
                               "version": os.environ.get("ImageVersion", "local")}}
    try:
        result.update(decky_sha256=digest(runtime), plugin_sha256=digest(plugin))
    except OSError as exc:
        return {**result, "error": f"input artifact is unreadable: {type(exc).__name__}"}
    if result["decky_sha256"] != expected_sha:
        return {**result, "error": "Decky runtime digest mismatch"}
    supported = json.loads(PIN.read_text(encoding="utf-8"))
    if not python_control and expected_sha != supported["sha256"] and os.environ.get("GITHUB_ACTIONS") != "true":
        return {**result, "error": "unreviewed Decky entry route requires disposable CI isolation"}
    if sys.platform != "linux":
        return {**result, "error": "Linux x86-64 runtime probe requires Linux"}
    try:
        with zipfile.ZipFile(plugin) as archive:
            metadata = archive.getinfo("CE-Decky/package.json")
            if metadata.file_size > 256 * 1024:
                raise ValueError("plugin metadata exceeds limit")
            plugin_version = json.loads(archive.read(metadata))["version"]
        inspected = inspect_package(plugin, expect_version=plugin_version)
        if not inspected["ok"]:
            raise ValueError(str(inspected["errors"])[:1500])
        result["plugin_version"] = plugin_version
        with tempfile.TemporaryDirectory(prefix="ce-decky-compat-") as directory:
            temporary = Path(directory)
            with zipfile.ZipFile(plugin) as archive:
                archive.extractall(temporary)
            report = temporary / "result.json"
            home = temporary / "home"
            home.mkdir()
            payload = Path(__file__).with_name("decky_compat_payload.py").resolve()
            # PyInstaller v6.16.0 pyi_rth_multiprocessing.py, Apache-2.0:
            # evidence only, no code copied. This exact diversion was verified
            # with the pinned Decky build. It is not a supported Decky API.
            code = ("from multiprocessing.resource_tracker import main; import runpy; "
                    f"runpy.run_path({str(payload)!r}, init_globals="
                    f"{{'compat_plugin_root': {str(temporary / 'CE-Decky')!r}, 'compat_report_path': {str(report)!r}}})")
            environment = {"PATH": os.defpath, "HOME": str(home), "TMPDIR": str(temporary),
                           "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
                           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
            with (temporary / "child.log").open("wb") as output:
                process = subprocess.Popen([str(runtime.resolve()), "-B", "-S", "-I", "-c", code],
                    cwd=home, env=environment, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True, preexec_fn=_limits)
                try:
                    exit_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    raise ValueError("Decky entry route or compatibility check timed out")
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    with (temporary / "child.log").open("rb") as log:
                        log.seek(max(0, (temporary / "child.log").stat().st_size - 2048))
                        tail = log.read(2048).decode("utf-8", errors="replace")
                    if tail:
                        result["diagnostic_tail"] = tail
            if not report.is_file() or report.stat().st_size > 16 * 1024:
                raise ValueError("Decky diagnostic entry route produced no bounded result")
            child = json.loads(report.read_text(encoding="utf-8"))
            if not isinstance(child, dict):
                raise ValueError("Decky result must be an object")
            if child.get("status") in {"FAIL", "INCOMPLETE"} and exit_code == {"FAIL": 1, "INCOMPLETE": 2}[child["status"]]:
                return {**result, "status": child["status"], "error": str(child.get("error", "check failed"))[:1500]}
            if (child.get("status") != "PASS" or exit_code != 0
                    or set(child.get("checks", [])) != REQUIRED_CHECKS
                    or child.get("plugin_version") != plugin_version
                    or not isinstance(child.get("pathlib_types_fallback"), bool)
                    or child.get("frozen") is not (not python_control)):
                raise ValueError("Decky result, process exit or expected checks do not agree")
            return {**result, "status": "PASS", **{key: child[key] for key in
                    ("checks", "python", "frozen", "pathlib_types_fallback")}}
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as exc:
        return {**result, "status": "INCOMPLETE", "error": f"{type(exc).__name__}: {str(exc)[:1500]}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--decky", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", action="store_true", help="ordinary Python control, not Decky evidence")
    args = parser.parse_args()
    result = probe(args.plugin, args.decky, expected_sha=args.sha256, version=args.version, python_control=args.python)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
