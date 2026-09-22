#!/usr/bin/env python3
"""Run the smallest CE Decky validation profile and emit a bounded summary.

This orchestrator uses only the Python standard library. It captures complete
stage output under build/qa and prints only the information needed to decide the
next action. Node and pnpm are never probed unless the selected work touches the
frontend, and pnpm is used only by the explicit bootstrap path.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
QA_ROOT = ROOT / "build" / "qa"
# There is one full gate, and it is `release`. It was two, running the same
# stages with half a second of packaging between them, and the names invited
# running both: one read as the ordinary one and the other as something kept
# for releases, while every production change here ends in an install and so
# wants the package. A name for a choice nobody should make is worse than no
# name, so the second one is gone rather than aliased.
PROFILES = ("auto", "repo", "backend", "frontend", "browser", "release", "store", "live", "direct")
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INCOMPLETE = 2
FAILURE_TAIL = 1600
# What a failing stage prints of the runner's own account of the failure. Two
# thousand characters is the first assertion with its diff and the file and line
# it is on, which is what a reader acts on; everything after it is the same
# thing again for the next test, and the log holds all of it.
FAILURE_EXCERPT = 2000
# How many failing test names one stage prints before it says how many more.
FAILURE_NAMES = 12
# What `pytest` exits with when its selection collected no test at all. It says
# nothing else: with `-q` and nothing to run, both streams are empty.
PYTEST_NOTHING_COLLECTED = 5

# `pytest`'s own last line under `-q`: the counts and the time, with nothing
# else on it. The repository's `pytest.ini` already asks for that brevity, and
# passing `-q` again on top of it took this line away entirely, which is what
# left the count reachable only by counting dots.
_PYTEST_TOTAL = re.compile(r"^\d+ (passed|failed|skipped|error)")


def _case_count(text: str) -> int | None:
    """How many test cases a stage actually ran, in each runner's own words.

    A stage that passed says only that, and how long it took, which leaves the
    one question a narrowed selection is asked with: did it cover what I named?
    `--name` matching nothing is caught on its own, but a file whose cases were
    renamed, a path that holds fewer than the reader thinks and a whole suite
    are all `PASSED` in the same eight characters, and the count was reachable
    only by opening the stage log and counting the runner's dots.

    Both runners state it and neither is parsed for anything else here: the
    number of cases that ran, which is what the reader is checking, rather than
    the number collected or skipped. Nothing is printed where a stage is not a
    test run or its runner said nothing this recognises.
    """
    found: int | None = None
    for line in text.splitlines():
        stripped = line.strip()
        # `vitest` names its own verdict; `pytest` ends `-q` output with the
        # same words and nothing before them.
        if stripped.startswith("Tests ") or _PYTEST_TOTAL.match(stripped):
            match = re.search(r"(\d+) passed", stripped)
            if match:
                found = int(match.group(1))
    return found


def _ran_no_cases(text: str) -> bool:
    """Whether a narrowed run matched nothing, in each runner's own words.

    Read from what they say rather than from what they do not: `pytest` prints
    `no tests ran`, and `vitest` reports every test in the file as skipped,
    which is what a `-t` nothing matched looks like. Anything else counts as a
    run, because a wrong refusal here stops a green selection for no reason.
    """
    lowered = text.lower()
    if "no tests ran" in lowered or "no test files found" in lowered:
        return True
    for line in text.splitlines():
        stripped = line.strip()
        # `vitest`'s own summary line, which carries a count in brackets. The
        # brackets are what keeps a test that prints a line beginning `Tests `
        # from being read as the runner's verdict about itself.
        if stripped.startswith("Tests ") and "(" in stripped:
            return "skipped" in stripped and "passed" not in stripped and "failed" not in stripped
    return False

# Where each runner starts saying why, in its own words.
#
# A tail is the wrong end of a test run. `pytest` ends with a summary a tail
# does show; `vitest` ends with a count and a footer, while the assertion, its
# diff and its file and line are hundreds of lines earlier - so every frontend
# failure in this repository was read by running the same selection a second
# time with a filter on the output, which costs exactly as long as the run did.
# These are the runners' own banners, matched as text because that is what they
# are: a marker that stops matching prints the tail instead, which is what this
# did before.
_FAILURE_BANNERS = (
    "Failed Tests",          # vitest, above the assertion and its diff
    "=== FAILURES ===",      # pytest, above the traceback
    "=== ERRORS ===",        # pytest, a collection error rather than a test
)


def _failure_digest(text: str) -> dict[str, object]:
    """Which tests failed, and the first thing the runner said about one.

    Both halves are the runner's own output rather than this file's reading of
    it: the names come from the lines each runner prints as its own list, and
    the excerpt begins at its own banner. Where neither is there - a stage that
    is not a test run, or one that died before it could report - the caller
    falls back to the tail, which is the right answer for a command that failed
    rather than a test that did.
    """
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        # `pytest` short summary: `FAILED path::test - AssertionError: ...`.
        # The word is dropped for a failure, because the line above already says
        # these are failures, and kept for an error, because a test that could
        # not be collected is a different thing to go and look at.
        if stripped.startswith("FAILED "):
            names.append(stripped[len("FAILED "):].split(" - ", 1)[0])
        elif stripped.startswith("ERROR "):
            names.append(stripped.split(" - ", 1)[0])
        # `vitest`: ` FAIL  tests/x.test.tsx > describe > name`
        elif stripped.startswith("FAIL "):
            names.append(stripped[len("FAIL "):].strip())
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    at = min(
        (found for found in (text.find(banner) for banner in _FAILURE_BANNERS) if found >= 0),
        default=-1,
    )
    excerpt = ""
    if at >= 0:
        start = text.rfind("\n", 0, at) + 1
        excerpt = text[start:start + FAILURE_EXCERPT]
        # On a whole line: the bound is arbitrary and half an assertion reads
        # like a different one.
        if len(excerpt) == FAILURE_EXCERPT and "\n" in excerpt:
            excerpt = excerpt[:excerpt.rfind("\n")]
        excerpt = excerpt.strip()
    return {"failed_tests": seen, "excerpt": excerpt}


@dataclass
class Stage:
    key: str
    command: tuple[str, ...] = ()
    timeout: float = 900.0
    env: dict[str, str] = field(default_factory=dict)
    incomplete_reason: str | None = None
    rerun_command: str | None = None
    # Whether the rest of the run is worth starting once this one has failed.
    # Only the cheap repository rules are preconditions: they read the tree and
    # nothing else, so a tree they refuse is a tree no suite, build or package
    # is going to be judged on. Everything after one is reported as not run
    # rather than silently dropped.
    precondition: bool = False
    # Whether this stage was narrowed to named cases, and therefore has to have
    # run at least one. A filter that matches nothing is the failure mode of
    # narrowing: `vitest -t` skips every test and reports a green run, so a
    # reader who mistyped a name is told their case passes when it never ran.
    named_cases: bool = False


LINUX_ONLY_TEST_FILES = {
    "tests/test_archive_import.py",
    "tests/test_atomic.py",
    "tests/test_ce_import.py",
    "tests/test_ce_runtime.py",
    "tests/test_ct_inspector.py",
    "tests/test_paths.py",
    "tests/test_providers.py",
    "tests/test_service.py",
    "tests/test_service_workflow.py",
    "tests/test_session_protocol.py",
    "tests/test_session_retirement_safety.py",
    "tests/test_shortcuts_vdf_prod.py",
    "tests/test_table_store.py",
}
FRONTEND_CONTROL_FILES = {
    "pnpm-lock.yaml",
    "rollup.config.js",
    "tsconfig.json",
    "tsconfig.sandbox.json",
    "vitest.config.ts",
    "tests/typecheck-shims.d.ts",
}
WSL_STAGE_KEYS = {"backend-full", "backend-linux-focused", "backend-linux-explicit"}
NATIVE_PYTHON_STAGE_KEYS = {"backend-focused", "backend-explicit", "provider-live", "provider-direct"}


def _is_frontend_change(name: str) -> bool:
    return (
        name.startswith("src/")
        or name.endswith(".tsx")
        or name.endswith(".test.ts")
        or name in FRONTEND_CONTROL_FILES
    )


def _run_probe(command: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _hash_files(*paths: Path) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _venv_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _python_has(python: Path, modules: Iterable[str]) -> bool:
    imports = ";".join(f"import {module}" for module in modules)
    probe = _run_probe([str(python), "-c", imports])
    return bool(probe and probe.returncode == 0)


def _pinned_python_requirements_satisfied(python: Path) -> bool:
    expected: dict[str, str] = {}
    for raw in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            return False
        name, version = line.split("==", 1)
        expected[name.strip()] = version.strip()
    script = (
        "import importlib.metadata as m,json,sys;"
        "e=json.loads(sys.argv[1]);"
        "raise SystemExit(0 if all(m.version(k)==v for k,v in e.items()) else 1)"
    )
    probe = _run_probe([str(python), "-c", script, json.dumps(expected, sort_keys=True)])
    return bool(probe and probe.returncode == 0)


@lru_cache(maxsize=1)
def _store_container_engine() -> str | None:
    for engine in ("podman", "docker"):
        if shutil.which(engine):
            return engine
    return None


def _development_python() -> Path:
    candidate = _venv_python(ROOT / ".venv")
    if candidate.is_file() and _pinned_python_requirements_satisfied(candidate):
        return candidate
    return Path(sys.executable)


def _bootstrap_python() -> Path:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11+ is required to bootstrap the development environment")
    current = Path(sys.executable)
    if _pinned_python_requirements_satisfied(current):
        return current
    venv = ROOT / ".venv"
    python = _venv_python(venv)
    if not python.is_file():
        completed = subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT)
        if completed.returncode:
            raise RuntimeError("could not create .venv")
    fingerprint = _hash_files(ROOT / "requirements-dev.txt")
    marker = venv / ".ce-decky-requirements.sha256"
    marker_matches = marker.is_file() and marker.read_text(encoding="ascii").strip() == fingerprint
    if not marker_matches or not _pinned_python_requirements_satisfied(python):
        log = ROOT / "build" / "bootstrap-python.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(ROOT / "requirements-dev.txt")],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
        if completed.returncode:
            raise RuntimeError(f"Python dependency restore failed; see {log.relative_to(ROOT)}")
        marker.write_text(fingerprint + "\n", encoding="ascii")
    return python


def _node() -> str | None:
    return shutil.which("node")


def _frontend_ready() -> bool:
    return bool(
        _node()
        and (ROOT / "node_modules" / "typescript" / "bin" / "tsc").is_file()
        and (ROOT / "node_modules" / "vitest" / "vitest.mjs").is_file()
        and (ROOT / "node_modules" / "rollup" / "dist" / "bin" / "rollup").is_file()
    )


def _frontend_pins_satisfied() -> bool:
    if not _frontend_ready():
        return False
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    expected = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    for name, version in expected.items():
        manifest = ROOT / "node_modules" / Path(*name.split("/")) / "package.json"
        if not manifest.is_file():
            return False
        installed = json.loads(manifest.read_text(encoding="utf-8"))
        if installed.get("version") != version:
            return False
    return True


def _bootstrap_frontend() -> None:
    node = _node()
    if not node:
        raise RuntimeError("Node.js 18+ is required only for frontend validation and Decky bundling")
    fingerprint = _hash_files(ROOT / "package.json", ROOT / "pnpm-lock.yaml")
    marker = ROOT / "node_modules" / ".ce-decky-lock.sha256"
    marker_matches = marker.is_file() and marker.read_text(encoding="ascii").strip() == fingerprint
    if marker_matches and _frontend_pins_satisfied():
        return
    if _frontend_pins_satisfied():
        marker.write_text(fingerprint + "\n", encoding="ascii")
        return
    corepack = shutil.which("corepack")
    if not corepack:
        raise RuntimeError("Corepack is required for the one-time pinned frontend dependency restore")
    log = ROOT / "build" / "bootstrap-frontend.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [corepack, "pnpm@9.15.9", "install", "--frozen-lockfile"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    log.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
    if completed.returncode or not _frontend_pins_satisfied():
        raise RuntimeError(f"frontend dependency restore failed; see {log.relative_to(ROOT)}")
    marker.write_text(fingerprint + "\n", encoding="ascii")


def _changed_files() -> tuple[list[str], str]:
    dirty = _run_probe(["git", "diff", "--name-only", "--diff-filter=ACMRD", "HEAD", "--"])
    untracked = _run_probe(["git", "ls-files", "--others", "--exclude-standard"])
    names: set[str] = set()
    if dirty and dirty.returncode == 0:
        names.update(line.strip().replace("\\", "/") for line in dirty.stdout.splitlines() if line.strip())
    if untracked and untracked.returncode == 0:
        names.update(line.strip().replace("\\", "/") for line in untracked.stdout.splitlines() if line.strip())
    if names:
        return sorted(names), "working tree"
    previous = _run_probe(["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD"])
    if previous and previous.returncode == 0:
        return sorted(line.strip().replace("\\", "/") for line in previous.stdout.splitlines() if line.strip()), "HEAD"
    return [], "none"


def _tests_for_changes(changed: Iterable[str]) -> list[str]:
    selected: set[str] = set()
    module_map = {
        "__init__": {"tests/test_plugin_lifecycle.py"},
        "network": {"tests/test_network_prod.py", "tests/test_network_tls.py", "tests/test_catalog_prod.py"},
        "catalog": {"tests/test_catalog_prod.py", "tests/test_acquisition_prod.py"},
        "acquisition": {"tests/test_acquisition_prod.py", "tests/test_import_snapshot_safety.py"},
        "providers": {"tests/test_providers.py", "tests/test_catalog_prod.py"},
        "game_identity": {"tests/test_game_identity.py", "tests/test_catalog_prod.py"},
        "table_store": {
            "tests/test_table_store.py",
            "tests/test_acquisition_prod.py",
            "tests/test_import_snapshot_safety.py",
            "tests/test_table_snapshot_safety.py",
        },
        "atomic": {"tests/test_atomic.py"},
        # The extractor is the managed installer's only content producer, so a
        # change there has to run the orchestration suite that mocks it away.
        "ce77_extractor": {"tests/test_ce77_extractor.py", "tests/test_managed_ce.py"},
        "authenticode": {"tests/test_authenticode.py", "tests/test_managed_ce_rediscovery.py"},
        "inno_reader": {"tests/test_inno_reader.py", "tests/test_ce_installer_source.py"},
        "ce_installer_source": {
            "tests/test_ce_installer_source.py",
            "tests/test_managed_ce_rediscovery.py",
        },
        "managed_ce": {
            "tests/test_managed_ce.py",
            "tests/test_managed_ce_rediscovery.py",
        },
        "profiles": {"tests/test_profiles.py", "tests/test_state_recovery.py", "tests/test_startup_plan.py"},
        "session_protocol": {
            "tests/test_session_protocol.py",
            "tests/test_bridge_lua.py",
            "tests/test_runtime_command_guards.py",
            "tests/test_startup_plan.py",
            "tests/test_state_recovery.py",
        },
        "ct_inspector": {"tests/test_ct_inspector.py", "tests/test_ct_inspector_prod.py", "tests/test_startup_plan.py"},
        "ce_launch": {
            "tests/test_ce_launch.py",
            "tests/test_session_retirement_safety.py",
            "tests/test_state_recovery.py",
            "tests/test_startup_plan.py",
        },
        "guarded_service": {"tests/test_service_workflow.py", "tests/test_runtime_command_guards.py"},
        "plugin": {"tests/test_plugin_lifecycle.py", "tests/test_rpc_contract.py"},
        "service": {
            "tests/test_service.py",
            "tests/test_service_workflow.py",
            "tests/test_rpc_contract.py",
            "tests/test_session_retirement_safety.py",
            "tests/test_state_recovery.py",
            "tests/test_startup_plan.py",
            "tests/test_runtime_command_guards.py",
        },
    }
    for name in changed:
        path = Path(name)
        if name.startswith("tests/test_") and name.endswith(".py"):
            if (ROOT / name).exists():
                selected.add(name)
        elif name.startswith("tests/fixtures/"):
            # A fixture is a captured provider answer, and re-capturing one after
            # a site changes its markup is exactly when the parsers that read it
            # have to run. Selecting nothing here would pass that change.
            basename = path.name
            for candidate in sorted((ROOT / "tests").glob("test_*.py")):
                if basename in candidate.read_text(encoding="utf-8"):
                    selected.add(f"tests/{candidate.name}")
        elif name == "main.py":
            selected.update(("tests/test_plugin_lifecycle.py", "tests/test_rpc_contract.py"))
        elif name.startswith("py_modules/ce_decky/") and path.suffix == ".py":
            mapped = module_map.get(path.stem, {f"tests/test_{path.stem}.py"})
            if not any((ROOT / item).exists() for item in mapped):
                return ["tests"]
            selected.update(mapped)
        elif name == "docs/FIELD_NOTES.md":
            # `check_release.py` refuses a stable tag while this document still
            # lists unverified work, so its shape is release-critical.
            selected.add("tests/test_qa.py")
        elif name in {".github/workflows/release.yml", "scripts/package_plugin.py"}:
            # The workflow names archives by hand; only the packaging script
            # decides which ones exist. A rename on either side is invisible
            # until a `v*` tag runs the release job for real.
            selected.add("tests/test_release_workflow.py")
        elif name == "CHANGELOG.md":
            # The changelog is hand-edited at the top of every round and its
            # mistakes render rather than break, so the one change that can
            # introduce them has to select the check for them.
            selected.add("tests/test_changelog_shape.py")
        elif name == "scripts/qa_baseline_check.py":
            selected.add("tests/test_qa_baseline_check.py")
        elif name == "scripts/verify_repo.py":
            # The repository stage runs this file on every route, so a defect in
            # a guard fails there. What it cannot say is which guard: these hold
            # each one to the exact shape it exists to refuse, and to the prose
            # and assertions that legitimately name the same thing.
            own = sorted(f"tests/{candidate.name}" for candidate in (ROOT / "tests").glob("test_verify_repo_*.py"))
            if not own:
                # A rule that has stopped naming anything is worse than none: it
                # reads as covered and selects nothing.
                return ["tests"]
            selected.update(own)
        elif name.startswith("scripts/target_") and path.suffix == ".py":
            own = f"tests/test_{path.stem}.py"
            if (ROOT / own).exists():
                selected.add(own)
        elif name.startswith("tools/") and path.suffix == ".py":
            # Standalone maintainer tools parse untrusted third-party artifacts,
            # so their format regressions have to run with them.
            own = f"tests/test_{path.stem}.py"
            if (ROOT / own).exists():
                selected.add(own)
        elif name == "py_modules/ce_decky/managed_ce_manifest.json":
            # The manifest is data the loader validates and the capability RPC
            # publishes, so a hand edit has to run both.
            selected.update((
                "tests/test_managed_ce.py",
                "tests/test_managed_ce_rediscovery.py",
            ))
        elif name == "py_modules/ce_decky/ce_decky_bridge.lua":
            selected.update(("tests/test_bridge_lua.py", "tests/test_bridge_asset.py"))
        elif name.startswith("tests/lua/") and path.suffix == ".lua":
            selected.add("tests/test_bridge_lua.py")
        elif name in {"pytest.ini", "requirements-dev.txt"}:
            return ["tests"]
    # Deliberately unfiltered. A name that reached here is either a literal in
    # a routing rule or a derived name already checked at the point it was
    # derived, so dropping a missing one would turn a rule that has stopped
    # naming anything into a rule that looks like it still works. That happened:
    # a rule kept selecting a test deleted with its runbook, and the filter made
    # a change under `scripts/target_` look routed when it selected nothing.
    # `tests/test_qa.py` holds every literal here to a file that exists.
    return sorted(selected)


def _pytest_file(selection: str) -> str:
    return selection.split("::", 1)[0].replace("\\", "/")


def _linux_test_selection(selection: str) -> bool:
    path = _pytest_file(selection)
    return path == "tests" or path in LINUX_ONLY_TEST_FILES


def _decode_wsl_distros(raw: bytes) -> list[str]:
    if not raw:
        return []
    text = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode(errors="ignore")
    return [line.strip().strip("\x00") for line in text.splitlines() if line.strip().strip("\x00")]


def _wsl_distros() -> tuple[str | None, list[str]]:
    wsl = shutil.which("wsl.exe")
    if not wsl:
        return None, []
    try:
        listed = subprocess.run([wsl, "--list", "--quiet"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return wsl, []
    return wsl, _decode_wsl_distros(listed.stdout)


def _wsl_root(wsl: str, distro: str) -> str | None:
    try:
        converted = subprocess.run(
            [wsl, "-d", distro, "--", "wslpath", "-a", ROOT.as_posix()],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return converted.stdout.strip() if converted.returncode == 0 else None


def _wsl_venv_relative(distro: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in distro).strip("-").lower()
    return f"build/wsl/{safe or 'default'}/venv"


def _bootstrap_wsl_dependencies() -> None:
    wsl, distros = _wsl_distros()
    if not wsl or not distros:
        raise RuntimeError("WSL is unavailable; use Linux CI/SteamOS for the full backend gate")
    fingerprint = _hash_files(ROOT / "requirements-dev.txt")
    for distro in distros:
        wsl_root = _wsl_root(wsl, distro)
        if not wsl_root:
            continue
        version = subprocess.run(
            [wsl, "-d", distro, "--", "python3", "-c", "import sys; assert sys.version_info >= (3,11)"],
            capture_output=True,
            timeout=20,
        )
        if version.returncode:
            continue
        relative = _wsl_venv_relative(distro)
        venv_python = f"{relative}/bin/python"
        marker = ROOT / relative / ".ce-decky-requirements.sha256"
        marker_matches = marker.is_file() and marker.read_text(encoding="ascii").strip() == fingerprint
        probe = subprocess.run(
            [wsl, "-d", distro, "--cd", wsl_root, "--", venv_python, "-c", "import sys,pytest,httpx,bs4,defusedxml; assert sys.version_info >= (3,11)"],
            capture_output=True,
            timeout=20,
        ) if marker_matches else None
        if not marker_matches or probe is None or probe.returncode:
            created = subprocess.run(
                [wsl, "-d", distro, "--cd", wsl_root, "--", "python3", "-m", "venv", "--copies", relative],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if created.returncode:
                continue
            installed = subprocess.run(
                [wsl, "-d", distro, "--cd", wsl_root, "--", venv_python, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-dev.txt"],
                capture_output=True,
                text=True,
                timeout=1200,
            )
            log = ROOT / "build" / f"bootstrap-wsl-{Path(relative).parts[2]}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text((installed.stdout or "") + (installed.stderr or ""), encoding="utf-8")
            if installed.returncode:
                raise RuntimeError(f"WSL dependency restore failed; see {log.relative_to(ROOT)}")
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(fingerprint + "\n", encoding="ascii")
        return
    raise RuntimeError("no WSL distro provides Python 3.11+; install it there or use Linux CI/SteamOS")


def _wsl_backend_command(python_args: tuple[str, ...]) -> tuple[tuple[str, ...] | None, str | None]:
    if os.name != "nt":
        return (str(_development_python()), *python_args), None
    wsl, distros = _wsl_distros()
    if not wsl:
        return None, "WSL is not installed; run the Linux gate in CI or on SteamOS"
    if not distros:
        return None, "WSL distribution discovery failed"
    for distro in distros:
        wsl_root = _wsl_root(wsl, distro)
        if not wsl_root:
            continue
        for python in (f"{_wsl_venv_relative(distro)}/bin/python", "python3"):
            probe = subprocess.run(
                [wsl, "-d", distro, "--cd", wsl_root, "--", python, "-c", "import sys,pytest,httpx,bs4,defusedxml; assert sys.version_info >= (3,11)"],
                capture_output=True,
                timeout=20,
            )
            if probe.returncode == 0:
                return (wsl, "-d", distro, "--cd", wsl_root, "--", python, *python_args), None
    return None, "no WSL distro has Python 3.11+ with the pinned dev dependencies; use Linux CI/SteamOS"


def _frontend_stage(key: str, relative_module: str, *arguments: str) -> Stage:
    node = _node()
    module = ROOT / "node_modules" / relative_module
    if not node or not module.is_file():
        return Stage(key, incomplete_reason="frontend toolchain is absent; rerun with --bootstrap")
    return Stage(key, (node, str(module), *arguments), timeout=1200.0)


def _explicit_vitest_stages(selections: list[str], name: str | None = None) -> list[Stage]:
    """One focused component run, the frontend counterpart of `--pytest`.

    The whole component stage is half a minute and covers eighteen files, so
    iterating on one of them paid for the other seventeen every time. This runs
    exactly what is named and nothing else, which also means it type-checks
    nothing: vitest compiles through esbuild, so `--profile frontend` stays the
    gate for a frontend change, exactly as the backend profile does after a
    focused `--pytest`.
    """
    if not selections:
        return []
    # `-t` is vitest's own, for the same reason: the component suite is 45
    # seconds and one case of it is under two.
    narrowed = (*selections, "-t", name) if name else tuple(selections)
    stage = _frontend_stage("frontend-explicit", "vitest/vitest.mjs", "run", "--reporter=dot", *narrowed)
    stage.named_cases = bool(name)
    return [stage]


def _frontend_stages(*, component: bool = True, core: bool = True, build: bool = True) -> list[Stage]:
    stages = [_frontend_stage("frontend-typecheck", "typescript/bin/tsc", "--noEmit")]
    if component:
        stages.append(_frontend_stage("frontend-component", "vitest/vitest.mjs", "run", "--reporter=dot"))
    if core:
        node = _node()
        tsc = ROOT / "node_modules" / "typescript" / "bin" / "tsc"
        if not node:
            stages.append(Stage("frontend-core", incomplete_reason="Node.js is unavailable"))
        elif not tsc.is_file():
            stages.append(Stage("frontend-core", incomplete_reason="frontend toolchain is absent; rerun with --bootstrap"))
        else:
            stages.append(Stage("frontend-core", (node, str(ROOT / "scripts" / "test_ts_core.mjs"))))
    if build:
        stages.append(_frontend_stage("frontend-build", "rollup/dist/bin/rollup", "-c"))
    node = _node()
    stages.append(Stage("frontend-bundle-smoke", (node, str(ROOT / "scripts" / "test_dist_fallback.mjs")) if node else (), incomplete_reason=None if node else "Node.js is unavailable"))
    return stages


def _browser_stage() -> Stage:
    return Stage("browser-headless-probe", (str(_development_python()), str(ROOT / "scripts" / "browser_probe.py")), timeout=60.0)


def _repo_preflight_stages() -> list[Stage]:
    """The repository rules, in front of everything they can save.

    Both read the tree and nothing else, and together they cost about a second.
    Run last, they found a misplaced JSX comment, an unsynchronized version or a
    stale contract only after the backend suite, the component suite, the build
    and the package had been paid for: the same finding, twenty minutes later
    than it could have been. A rule that fails here fails before any of that
    starts.
    """
    python = str(_development_python())
    return [
        Stage("release-metadata", (python, str(ROOT / "scripts" / "check_release.py")), timeout=120.0, precondition=True),
        Stage("repository", (python, str(ROOT / "scripts" / "verify_repo.py")), timeout=120.0, precondition=True),
    ]


def _repo_final_stages() -> list[Stage]:
    """What can only be judged once the run has produced what it produces.

    `git diff --check` reads the working tree, and `dist/index.js` is in it:
    moved in front of the build, it would check the bundle the previous run
    left behind and pass a tree whose own build is about to break it.
    """
    return [Stage("diff-whitespace", ("git", "diff", "--check"))]


def _repo_stages() -> list[Stage]:
    return [*_repo_preflight_stages(), *_repo_final_stages()]


def _backend_full_stage() -> Stage:
    command, reason = _wsl_backend_command(("-m", "pytest", "--tb=short", "--disable-warnings"))
    return Stage("backend-full", command or (), timeout=1800.0, incomplete_reason=reason)


def _backend_focused_stage(tests: list[str], *, key: str = "backend-focused", linux: bool = False) -> Stage | None:
    if not tests:
        return None
    if "tests" in tests:
        return _backend_full_stage()
    if linux and os.name == "nt":
        command, reason = _wsl_backend_command(("-m", "pytest", "--tb=short", "--disable-warnings", *tests))
        return Stage(key, command or (), timeout=1200.0, incomplete_reason=reason)
    python = _development_python()
    if not _python_has(python, ("pytest", "httpx", "bs4", "defusedxml")):
        return Stage(key, incomplete_reason="Python dev dependencies are absent; rerun with --bootstrap")
    return Stage(
        key,
        (str(python), "-m", "pytest", "--tb=short", "--disable-warnings", *tests),
        timeout=1200.0,
    )


def _backend_focused_stages(tests: list[str]) -> list[Stage]:
    if not tests:
        return []
    if "tests" in tests:
        return [_backend_full_stage()]
    if os.name != "nt":
        stage = _backend_focused_stage(tests)
        return [stage] if stage else []
    safe = [item for item in tests if not _linux_test_selection(item)]
    linux = [item for item in tests if _linux_test_selection(item)]
    stages: list[Stage] = []
    if safe:
        stage = _backend_focused_stage(safe)
        if stage:
            stages.append(stage)
    if linux:
        stage = _backend_focused_stage(linux, key="backend-linux-focused", linux=True)
        if stage:
            stages.append(stage)
    return stages


# What `-k` reads as an operator rather than as part of a name.
_PYTEST_FILTER_SYNTAX = (" and ", " or ", " not ", "(", ")")


def _pytest_filter(name: str) -> str:
    """One `--name` in the syntax `-k` actually parses.

    `pytest` reads `-k` as an expression, so a phrase such as `holds off the
    flags` is a syntax error rather than a filter, while `vitest -t` takes the
    same phrase as text. The words are joined into the expression that means
    what the phrase meant - every one of them is in the name - so one `--name`
    works for both runners. An expression somebody wrote on purpose is passed
    through untouched.
    """
    if any(token in name for token in _PYTEST_FILTER_SYNTAX):
        return name
    return " and ".join(name.split()) or name


def _explicit_pytest_stages(tests: list[str], name: str | None = None) -> list[Stage]:
    if not tests:
        return []
    # `-k` is pytest's own narrowing, passed through rather than reimplemented:
    # a file of a hundred cases is one run either way, and reading one failure
    # by running all of them is the cost this exists to remove. It is added to
    # each command rather than to the selection, because the selection is split
    # by which tests need Linux and a flag is not a test.
    narrow = ("-k", _pytest_filter(name)) if name else ()
    if os.name != "nt":
        stage = _backend_focused_stage([*tests, *narrow], key="backend-explicit")
        if stage:
            stage.named_cases = bool(name)
        return [stage] if stage else []
    safe = [item for item in tests if not _linux_test_selection(item)]
    linux = [item for item in tests if _linux_test_selection(item)]
    stages: list[Stage] = []
    if safe:
        stage = _backend_focused_stage([*safe, *narrow], key="backend-explicit")
        if stage:
            stage.named_cases = bool(name)
            stages.append(stage)
    if linux:
        command, reason = _wsl_backend_command(
            ("-m", "pytest", "--tb=short", "--disable-warnings", *linux, *narrow))
        stages.append(Stage(
            "backend-linux-explicit", command or (), timeout=1200.0,
            incomplete_reason=reason, named_cases=bool(name),
        ))
    return stages


def _auto_stages(changed: list[str]) -> list[Stage]:
    stages: list[Stage] = []
    tests = _tests_for_changes(changed)
    runner_changed = any(
        name in {"scripts/qa.py", "scripts/browser_probe.py", "scripts/browser_harness.py"}
        for name in changed
    )
    if runner_changed:
        tests = sorted(set(tests) | {
            "tests/test_qa.py", "tests/test_browser_probe.py", "tests/test_browser_harness.py",
        })
    stages.extend(_backend_focused_stages(tests))
    frontend_names = [name for name in changed if _is_frontend_change(name)]
    if frontend_names:
        # Any change under `src/`, because that is what the suite covers.
        #
        # It imports two dozen modules from there, screens and helpers alike,
        # so which of them a component test reaches is not a question a list in
        # this file can answer, and keeping one is what got it wrong: naming
        # five core modules meant a changed modal got a type-check and a build
        # and none of the eighteen files that render it. Naming the screens
        # instead would have left the same hole one directory over, under
        # `tableImport`, `providerSelection`, `supportLog` and the rest, every
        # one of which a component test imports directly.
        #
        # It costs half a minute, and only on a frontend change: the reuse
        # record means an unchanged tree does not pay it a second time.
        component = any(
            name.startswith("src/")
            or (name.startswith("tests/") and (name.endswith(".test.ts") or name.endswith(".test.tsx")))
            or name in {"vitest.config.ts", "pnpm-lock.yaml"}
            for name in frontend_names
        )
        core = any(name.startswith("src/steam/") or name in {"src/index.tsx", "src/api.ts", "src/types.ts", "pnpm-lock.yaml"} for name in frontend_names)
        stages.extend(_frontend_stages(component=component, core=core))
    elif any(name.startswith("dist/") for name in changed):
        node = _node()
        stages.append(Stage("frontend-bundle-smoke", (node, str(ROOT / "scripts" / "test_dist_fallback.mjs")) if node else (), incomplete_reason=None if node else "Node.js is unavailable"))
    if any(name in {"scripts/package_plugin.py", "requirements-runtime.lock"} for name in changed):
        stages.append(Stage("plugin-package", (str(_development_python()), str(ROOT / "scripts" / "package_plugin.py")), timeout=1200.0))
    if "scripts/browser_probe.py" in changed:
        stages.append(_browser_stage())
    return _dedupe([*_repo_preflight_stages(), *stages, *_repo_final_stages()])


def _dedupe(stages: Iterable[Stage]) -> list[Stage]:
    result: list[Stage] = []
    seen: set[str] = set()
    for stage in stages:
        if stage.key not in seen:
            result.append(stage)
            seen.add(stage.key)
    return result


def _store_artifact_stage() -> Stage:
    """The artifact the official Decky Store builds, which `release` never sees.

    It is its own profile rather than part of `release` because it needs a
    container engine and a full dependency install inside it, which is minutes
    rather than the second a packaging stage costs. A host without an engine
    leaves this INCOMPLETE for CI to run, the same way the Linux-only stages do.
    """
    python = str(_development_python())
    checker = ROOT / "scripts" / "check_store_artifact.py"
    if not _store_container_engine():
        return Stage("store-artifact", incomplete_reason="no container engine; the Decky CLI needs podman or docker")
    return Stage("store-artifact", (python, str(checker), "--build"), timeout=1800.0)


def _profile_stages(
    profile: str, changed: list[str], explicit_pytest: list[str],
    explicit_vitest: list[str] | None = None, name: str | None = None,
) -> list[Stage]:
    python = str(_development_python())
    explicit_vitest = explicit_vitest or []
    if explicit_pytest or explicit_vitest:
        return [
            *_repo_preflight_stages(),
            *_explicit_pytest_stages(explicit_pytest, name),
            *_explicit_vitest_stages(explicit_vitest, name),
            *_repo_final_stages(),
        ]
    if profile == "auto":
        return _auto_stages(changed)
    if profile == "repo":
        return _repo_stages()
    if profile == "backend":
        return [*_repo_preflight_stages(), _backend_full_stage(), *_repo_final_stages()]
    if profile == "frontend":
        return [*_repo_preflight_stages(), *_frontend_stages(), *_repo_final_stages()]
    if profile == "browser":
        return [*_repo_preflight_stages(), _browser_stage(), *_repo_final_stages()]
    if profile == "release":
        return [
            *_repo_preflight_stages(),
            _backend_full_stage(),
            *_frontend_stages(),
            Stage("plugin-package", (python, str(ROOT / "scripts" / "package_plugin.py")), timeout=1200.0),
            Stage("target-package", (python, str(ROOT / "scripts" / "target_package_probe.py")), timeout=120.0),
            *_repo_final_stages(),
        ]
    if profile == "store":
        return [*_repo_preflight_stages(), _store_artifact_stage(), *_repo_final_stages()]
    if profile == "live":
        return [
            Stage(
                "provider-live",
                (python, "-m", "pytest", "-m", "live_provider", "tests/live"),
                timeout=1800.0,
                env={"CE_DECKY_RUN_LIVE_PROVIDER_TESTS": "1"},
                rerun_command="python scripts/qa.py --profile live",
            )
        ]
    if profile == "direct":
        return [
            Stage(
                "provider-direct",
                (python, "-m", "pytest", "-m", "direct_provider", "tests/live"),
                timeout=1800.0,
                env={"CE_DECKY_RUN_LIVE_PROVIDER_TESTS": "1"},
                rerun_command="python scripts/qa.py --profile direct",
            )
        ]
    raise AssertionError(profile)


def _command_text(command: tuple[str, ...]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _qa_rerun_command(
    profile: str, explicit_pytest: list[str], stage: Stage,
    explicit_vitest: list[str] | None = None, name: str | None = None,
) -> str:
    command: list[str] = ["python", "scripts/qa.py", "--profile", profile]
    for node in explicit_pytest:
        command.extend(("--pytest", node))
    for node in explicit_vitest or []:
        command.extend(("--vitest", node))
    if name:
        command.extend(("--name", name))
    if stage.incomplete_reason and "rerun with --bootstrap" in stage.incomplete_reason:
        command.append("--bootstrap")
    command.extend(("--stage", stage.key))
    return _command_text(tuple(command))


def _with_rerun_commands(
    profile: str, explicit_pytest: list[str], stages: list[Stage],
    explicit_vitest: list[str] | None = None, name: str | None = None,
) -> list[Stage]:
    for stage in stages:
        if stage.rerun_command is None:
            stage.rerun_command = _qa_rerun_command(profile, explicit_pytest, stage, explicit_vitest, name)
    return stages


def _select_stage(stages: list[Stage], keys: list[str]) -> list[Stage]:
    """The named stages, in the order the profile schedules them.

    Several may be named. It used to take exactly one, and argparse keeps the
    last of a repeated option rather than complaining about it, so naming two
    ran the second and said nothing about the first: a run that looked like it
    had rebuilt the bundle and had not.
    """
    if not keys:
        return stages
    wanted = list(dict.fromkeys(keys))
    selected = [stage for stage in stages if stage.key in wanted]
    unknown = [key for key in wanted if not any(stage.key == key for stage in stages)]
    if unknown:
        available = ", ".join(stage.key for stage in stages) or "none"
        raise ValueError(
            f"QA stage(s) {unknown} not selected by this profile; available: {available}"
        )
    return selected


def _bootstrap_needs(stages: list[Stage]) -> tuple[bool, bool, bool]:
    needs_frontend = any(stage.key.startswith("frontend-") for stage in stages)
    needs_wsl = os.name == "nt" and any(stage.key in WSL_STAGE_KEYS for stage in stages)
    needs_native_python = any(stage.key in NATIVE_PYTHON_STAGE_KEYS for stage in stages)
    if os.name != "nt" and any(stage.key == "backend-full" for stage in stages):
        needs_native_python = True
    return needs_native_python, needs_wsl, needs_frontend


# Where a passing stage records what the tree looked like when it passed.
REUSE_RECORD = QA_ROOT / "reuse.json"
# Stages that produce something a later command reads, so their work is the
# point rather than their verdict. Packaging costs half a second and writes the
# artifact a deployment installs; skipping it would leave a summary naming a
# file this run did not build.
NEVER_REUSED = frozenset({
    # Their work is the point rather than their verdict. Packaging costs half a
    # second and writes the artifact a deployment installs; skipping it would
    # leave a summary naming a file this run did not build.
    "plugin-package",
    "target-package",
    # And these are not about this tree at all. They exist to observe somebody
    # else's server as it is right now, which is the one thing an unchanged
    # tree says nothing about: a provider that has started refusing, changed
    # its markup or moved a route would be reported as fine by a run that never
    # contacted it.
    "provider-live",
    "provider-direct",
})


@lru_cache(maxsize=1)
def _tree_fingerprint() -> str:
    """What every tracked file contains, cheaply enough to ask on every run.

    Contents rather than size and modification time, which is not a refinement
    but the difference between working and not: the gate itself rewrites
    `dist/index.js`, a tracked file, on every frontend build, and the bytes it
    writes are identical because the bundler is deterministic. Keyed on
    modification time, the gate invalidated its own reuse every time it ran.
    Measured at 12 ms for this tree's 439 files and 11.4 MB.

    Deliberately the whole tree rather than the files a stage is thought to
    touch. A guess about what a suite reads is exactly the kind of thing that
    is wrong once and then hides a real failure; any edit anywhere invalidates
    everything, which is the conservative direction.
    """
    digest = hashlib.sha256()
    listing = _run_probe(["git", "ls-files", "-z"])
    if listing is None or listing.returncode != 0:
        # An empty answer means this run neither reuses nor records: a tree it
        # cannot describe is one it cannot recognise later either, and writing
        # something unrepeatable would replace what a run that could describe
        # the tree had established about the same stages.
        return ""
    for name in listing.stdout.split("\0"):
        if not name:
            continue
        digest.update(f"{name}:".encode("utf-8"))
        try:
            with (ROOT / name).open("rb") as handle:
                while chunk := handle.read(1 << 20):
                    digest.update(chunk)
        except OSError:
            digest.update(b"missing")
    # The restored dependency trees are not tracked, and both record their own
    # identity where this can read it.
    for marker in (ROOT / ".venv" / ".ce-decky-requirements.sha256", ROOT / "node_modules" / ".ce-decky-lock.sha256"):
        try:
            digest.update(marker.read_bytes())
        except OSError:
            digest.update(b"absent")
    return digest.hexdigest()


def _stage_fingerprint(stage: Stage) -> str:
    """This exact stage, on this exact tree, under this exact interpreter.

    Empty when the tree cannot be described, which is what stops a run in that
    state from either reusing or recording anything.
    """
    tree = _tree_fingerprint()
    if not tree:
        return ""
    digest = hashlib.sha256()
    digest.update(stage.key.encode("utf-8"))
    digest.update(_command_text(stage.command).encode("utf-8"))
    digest.update(json.dumps(sorted(stage.env.items()), ensure_ascii=False).encode("utf-8"))
    digest.update(sys.version.encode("utf-8"))
    digest.update(str(_development_python()).encode("utf-8"))
    digest.update(tree.encode("utf-8"))
    return digest.hexdigest()


def _load_reuse_record() -> dict[str, str]:
    try:
        raw = json.loads(REUSE_RECORD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if isinstance(value, str)}


def _save_reuse_record(record: dict[str, str]) -> None:
    try:
        REUSE_RECORD.parent.mkdir(parents=True, exist_ok=True)
        REUSE_RECORD.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        # A record that cannot be written costs a rerun, which is the same
        # thing this project did before there was one.
        pass


def _run_stage(stage: Stage, run_root: Path) -> dict[str, object]:
    started = time.monotonic()
    stdout_path = run_root / "logs" / f"{stage.key}.out.txt"
    stderr_path = run_root / "logs" / f"{stage.key}.err.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    if stage.incomplete_reason:
        return {
            "key": stage.key,
            "status": "incomplete",
            "reason": stage.incomplete_reason,
            "duration_seconds": 0.0,
            "command": list(stage.command),
            "rerun_command": stage.rerun_command,
            "stdout_log": None,
            "stderr_log": None,
        }
    environment = os.environ.copy()
    environment.update(stage.env)
    environment["PYTHONPATH"] = str(ROOT / "py_modules")
    try:
        completed = subprocess.run(
            stage.command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=stage.timeout,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        status = (
            "passed" if completed.returncode == EXIT_OK
            else "incomplete" if completed.returncode == EXIT_INCOMPLETE
            else "failed"
        )
        reason = None if completed.returncode == EXIT_OK else f"exit {completed.returncode}"
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        status = "failed"
        reason = f"timeout after {stage.timeout:g}s"
        completed = None
    except OSError as exc:
        stdout = ""
        stderr = str(exc)
        status = "incomplete"
        reason = str(exc)
        completed = None
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined = (stdout + "\n" + stderr).strip()
    # A narrowed run that ran nothing is not a run that passed. `vitest -t` with
    # a name nothing matches skips every test in the file and reports a green
    # run, which tells a reader who mistyped a name that their case is fine.
    # A narrowed run that matched nothing, whichever way its runner reported it:
    # `vitest` skips every test and exits green, `pytest` collects none, prints
    # not one character and exits 5. One sentence for both, because what a
    # reader does about them is the same.
    collected_nothing = completed is not None and completed.returncode == PYTEST_NOTHING_COLLECTED
    if stage.named_cases and (_ran_no_cases(combined) or collected_nothing):
        status = "failed"
        reason = "--name matched no test, so nothing ran"
    return {
        "key": stage.key,
        "status": status,
        "reason": reason,
        "duration_seconds": round(time.monotonic() - started, 3),
        "command": list(stage.command),
        "rerun_command": stage.rerun_command or _command_text(stage.command),
        "stdout_log": str(stdout_path.relative_to(ROOT)),
        "stderr_log": str(stderr_path.relative_to(ROOT)),
        "output_tail": combined[-4000:] if status != "passed" else "",
        "exit_code": completed.returncode if completed else None,
        **({} if (cases := _case_count(combined)) is None else {"cases": cases}),
        **({} if status == "passed" else _failure_digest(combined)),
    }


def _packaged_artifact(results: list[dict[str, object]]) -> dict[str, str]:
    """The package this run built, and its exact identity.

    Both facts are already produced by the run and were only written into two
    different stage logs, so every deployment began by reading them back out:
    the command that comes next takes the digest as a mandatory argument, never
    as a discovery guess, and had nowhere to take it from but a log file. This
    is the same two values on the summary the run already prints.

    Never a failure path. A convenience line that cannot be produced is left
    out; the logs it would have come from are still where they were.
    """
    artifact: dict[str, str] = {}
    by_key = {item["key"]: item for item in results if item["status"] == "passed"}
    package = by_key.get("plugin-package")
    if package is not None:
        try:
            log = ROOT / str(package.get("stdout_log", ""))
            lines = [line.strip() for line in log.read_text(encoding="utf-8").splitlines()]
        except (OSError, ValueError):
            lines = []
        path = next((line for line in reversed(lines) if line.endswith(".zip")), "")
        if path:
            # Repo-relative like every other path this summary prints, when it
            # is inside the repository at all.
            candidate = Path(path)
            try:
                artifact["package"] = str(candidate.relative_to(ROOT))
            except ValueError:
                artifact["package"] = path
    probe = by_key.get("target-package")
    if probe is not None:
        try:
            report = json.loads((ROOT / str(probe.get("stdout_log", ""))).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = None
        digest = report.get("sha256") if isinstance(report, dict) else None
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            artifact["sha256"] = digest
    return artifact


def _write_results(profile: str, source: str, changed: list[str], results: list[dict[str, object]], run_root: Path) -> int:
    failed = [item for item in results if item["status"] == "failed"]
    incomplete = [item for item in results if item["status"] == "incomplete"]
    reused = [item for item in results if item["status"] == "reused"]
    exit_code = EXIT_FAILED if failed else EXIT_INCOMPLETE if incomplete else EXIT_OK
    artifact = _packaged_artifact(results)
    summary = {
        "profile": profile,
        "change_source": source,
        "changed_files": changed,
        "exit_code": exit_code,
        "stages": results,
    }
    if artifact:
        summary["artifact"] = artifact
    summary_path = run_root / "qa-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failures_path: Path | None = None
    if failed or incomplete:
        failures_path = run_root / "qa-failures.json"
        failures_path.write_text(
            json.dumps({"profile": profile, "stages": failed + incomplete}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print(f"QA {profile}: {len(changed)} changed file(s) from {source}")
    for item in results:
        duration = f"{item['duration_seconds']:.1f}s"
        # What a test stage covered, beside what it cost. A reused stage has no
        # count of its own: it did not run, and printing the one from the run
        # that did would say this one proved it.
        cases = item.get("cases")
        covered = f", {cases} case{'' if cases == 1 else 's'}" if isinstance(cases, int) else ""
        reason = f" - {item['reason']}" if item.get("reason") else ""
        print(f"  {str(item['status']).upper():10} {item['key']} ({duration}{covered}){reason}")
    counts = {status: sum(item["status"] == status for item in results) for status in ("passed", "failed", "incomplete", "skipped")}
    reused_note = f", {len(reused)} reused" if reused else ""
    skipped_note = f", {counts['skipped']} not run" if counts["skipped"] else ""
    print(
        f"  result: {counts['passed']} passed, {counts['failed']} failed, "
        f"{counts['incomplete']} incomplete{reused_note}{skipped_note}; exit {exit_code}"
    )
    # Both kinds, not whichever came first. A run can fail one stage and leave
    # another unable to start, and only the failure was ever described: a red
    # CI run whose real cause was a collection error in one backend module
    # printed the frontend's tail and nothing at all about the module, and the
    # only other copy of that output is `qa-failures.json` in a run directory
    # the runner throws away. Naming the stage matters for the same reason.
    for actionable in failed[:1] + incomplete[:1]:
        # What failed, by the names the runner itself printed. A stage that
        # failed a dozen tests used to print whichever of them the tail happened
        # to reach, so the first thing a reader did was run it again to find out
        # what to look at.
        names = [str(name) for name in (actionable.get("failed_tests") or [])]
        if names:
            shown = ", ".join(names[:FAILURE_NAMES])
            more = f" and {len(names) - FAILURE_NAMES} more" if len(names) > FAILURE_NAMES else ""
            print(f"  {actionable['key']} failed: {shown}{more}")
        # And why, from where the runner started saying it. The tail is kept for
        # a stage that is not a test run and for one that died before it could
        # report: there the last thing said is the whole of what happened.
        excerpt = str(actionable.get("excerpt") or "")
        body = excerpt or str(actionable.get("output_tail") or "")[-FAILURE_TAIL:].strip()
        if body:
            print(f"  {actionable['key']} {'why' if excerpt else 'tail'}:")
            for line in body.splitlines():
                print(f"    {line}")
        if actionable.get("rerun_command"):
            print(f"  rerun: {actionable['rerun_command']}")
    print(f"  summary: {summary_path.relative_to(ROOT)}")
    if failures_path:
        print(f"  failures: {failures_path.relative_to(ROOT)}")
    for key, value in artifact.items():
        print(f"  {key}: {value}")
    # The one command this artifact and this digest exist for. They were printed
    # without it, so the next step was retyped from memory against a helper
    # whose flags had to be read first. Only for a run that passed: a package
    # exists as soon as the packaging stage does, and a stage after it can still
    # refuse the tree, so offering the command for a red run is offering to
    # install exactly what the gate has just rejected. Left off on a host that
    # cannot install anything either.
    install = _install_command(artifact) if exit_code == EXIT_OK else None
    if install:
        print(f"  install: {install}")
    return exit_code


def _install_command(artifact: dict[str, object]) -> str | None:
    """The exact deployment command for what this run packaged, where one applies."""
    if os.name == "nt":
        return None
    package = artifact.get("package")
    digest = artifact.get("sha256")
    if not isinstance(package, str) or not isinstance(digest, str):
        return None
    return (
        f"python3 scripts/target_plugin_install.py install {package} "
        f"--sha256 {digest} --replace"
    )


def _lua() -> str | None:
    """Locate a stock Lua interpreter for the resident-bridge conformance run."""
    for name in ("lua", "lua5.4", "lua54", "lua5.3", "lua53", "luajit"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _check_environment() -> int:
    try:
        from scripts.browser_probe import discover_browser
    except ModuleNotFoundError:  # Direct `python scripts/qa.py` execution.
        from browser_probe import discover_browser

    python = _development_python()
    wsl_command, wsl_reason = _wsl_backend_command(("--version",))
    values = {
        "python": f"{python} ({platform.python_version()})",
        "python_dev": "ready" if _python_has(python, ("pytest", "httpx", "bs4", "defusedxml")) else "missing",
        "node": _node() or "not detected (needed only for frontend)",
        "frontend": "ready" if _frontend_ready() else "not restored",
        "browser": discover_browser() or "not detected",
        "lua": _lua() or "not detected (bridge conformance is skipped)",
        "linux_gate": _command_text(wsl_command) if wsl_command else (wsl_reason or "not available"),
    }
    for key, value in values.items():
        print(f"  {key:11} {value}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, default="auto")
    parser.add_argument("--bootstrap", action="store_true", help="restore only dependencies needed by the selected profile/stage")
    parser.add_argument("--pytest", action="append", default=[], metavar="NODE_OR_PATH", help="run an explicit focused pytest selection")
    parser.add_argument("--vitest", action="append", default=[], metavar="PATH_OR_PATTERN", help="run an explicit focused vitest selection")
    parser.add_argument(
        "--name", metavar="EXPR",
        help="narrow a --pytest or --vitest selection to the cases whose name matches,"
             " through the runner's own -k and -t",
    )
    parser.add_argument(
        "--stage", action="append", default=[], metavar="STAGE",
        help="run only this stage of the profile; repeat it to name several (used by bounded reruns)",
    )
    parser.add_argument(
        "--no-reuse", action="store_true",
        help="run every selected stage even where nothing it could read has changed since it passed",
    )
    parser.add_argument("--check-env", action="store_true", help="detect tools without installing or testing")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)
    if args.check_env:
        return _check_environment()

    changed, source = _changed_files()
    if args.name and not (args.pytest or args.vitest):
        # It narrows a selection, so without one it would quietly narrow
        # nothing: a run that looks focused and is the whole gate.
        parser.error("--name narrows --pytest or --vitest; name one of those too")
    preliminary = _profile_stages(args.profile, changed, args.pytest, args.vitest, args.name)
    try:
        preliminary = _select_stage(preliminary, args.stage)
    except ValueError as exc:
        parser.error(str(exc))
    needs_native_python, needs_wsl, needs_frontend = _bootstrap_needs(preliminary)
    if args.bootstrap:
        try:
            if needs_wsl:
                _bootstrap_wsl_dependencies()
            if needs_native_python:
                _bootstrap_python()
            if needs_frontend:
                _bootstrap_frontend()
        except RuntimeError as exc:
            print(f"bootstrap: INCOMPLETE - {exc}")
            return EXIT_INCOMPLETE
        _development_python.cache_clear()

    stages = _profile_stages(args.profile, changed, args.pytest, args.vitest, args.name)
    stages = _with_rerun_commands(args.profile, args.pytest, stages, args.vitest, args.name)
    try:
        stages = _select_stage(stages, args.stage)
    except ValueError as exc:
        parser.error(str(exc))

    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%S")
    run_root = QA_ROOT / run_id
    suffix = 1
    while run_root.exists():
        run_root = QA_ROOT / f"{run_id}-{suffix}"
        suffix += 1
    run_root.mkdir(parents=True)
    # What passed last time, and on what tree. A stage whose inputs have not
    # moved since it passed is not run again: the contract already says to
    # reuse a pass until a covered input changes, and this is that, applied by
    # the runner rather than left to whoever is typing the commands.
    # Naming a stage or a test file is an instruction to run it: somebody who
    # typed the exact selection is asking for the answer again, not for the one
    # from before. Reuse is for the profile that selects work on its own.
    explicit = bool(args.stage or args.pytest or args.vitest)
    # Loaded either way, because this run still has to say what it proved about
    # the stages it ran. Only whether it is consulted changes: dropping it
    # instead would let one focused selection throw away what every other stage
    # had established about the same tree.
    record = _load_reuse_record()
    consult = not (args.no_reuse or explicit)
    results: list[dict[str, object]] = []
    passed_here: list[Stage] = []
    for index, stage in enumerate(stages):
        fingerprint = _stage_fingerprint(stage)
        if consult and fingerprint and stage.key not in NEVER_REUSED and record.get(stage.key) == fingerprint:
            results.append({
                "key": stage.key,
                "status": "reused",
                "reason": "unchanged since it passed",
                "duration_seconds": 0.0,
                "command": list(stage.command),
                "rerun_command": stage.rerun_command or _command_text(stage.command),
                "output_tail": "",
                "exit_code": 0,
            })
            continue
        result = _run_stage(stage, run_root)
        results.append(result)
        if result["status"] == "passed":
            passed_here.append(stage)
        else:
            record.pop(stage.key, None)
        # Failed, not merely "not passed". An INCOMPLETE stage is one that could
        # not run, which says nothing about the tree: treating it as a refusal
        # would turn a missing dependency into a run that quietly does nothing
        # at all, and the suites it skipped would be reported as not owed.
        if stage.precondition and result["status"] == "failed":
            # Nothing after this is worth the wait. The rest is named rather
            # than dropped, because a run that simply stops looks the same as a
            # run that was never going to include them.
            for skipped in stages[index + 1:]:
                results.append({
                    "key": skipped.key,
                    "status": "skipped",
                    "reason": f"not run: {stage.key} has to pass first",
                    "duration_seconds": 0.0,
                    "command": list(skipped.command),
                    "rerun_command": skipped.rerun_command or _command_text(skipped.command),
                    "output_tail": "",
                    "exit_code": 0,
                })
            break
    # Recorded against the tree as this run leaves it, not as it found it. A
    # gate rewrites `dist/index.js` while it runs, so the tree a stage passed
    # against and the tree the next run sees differ by what this run itself
    # produced: keyed on the earlier one, the run after any frontend change
    # reused nothing, which is exactly the pair of full runs this is for. What
    # is different between the two is this run's own output, and a stage that
    # reads it read the new one.
    _tree_fingerprint.cache_clear()
    for stage in passed_here:
        fingerprint = _stage_fingerprint(stage)
        if fingerprint:
            record[stage.key] = fingerprint
    _save_reuse_record(record)
    return _write_results(args.profile, source, changed, results, run_root)


if __name__ == "__main__":
    raise SystemExit(main())
