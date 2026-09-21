from pathlib import Path
import json
import re
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import qa


def _touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def test_changed_backend_modules_map_to_direct_dependents(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    _touch(tmp_path, "tests/test_catalog_prod.py", "tests/test_network_prod.py", "tests/test_network_tls.py", "tests/test_acquisition_prod.py")
    assert qa._tests_for_changes(["py_modules/ce_decky/network.py"]) == [
        "tests/test_catalog_prod.py",
        "tests/test_network_prod.py",
        "tests/test_network_tls.py",
    ]
    assert qa._tests_for_changes(["py_modules/ce_decky/catalog.py"]) == [
        "tests/test_acquisition_prod.py",
        "tests/test_catalog_prod.py",
    ]


def test_safety_regressions_are_routed_by_subject_not_audit_provenance(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    _touch(
        tmp_path,
        "tests/test_profiles.py",
        "tests/test_state_recovery.py",
        "tests/test_startup_plan.py",
        "tests/test_table_store.py",
        "tests/test_acquisition_prod.py",
        "tests/test_import_snapshot_safety.py",
        "tests/test_table_snapshot_safety.py",
    )
    assert qa._tests_for_changes(["py_modules/ce_decky/profiles.py"]) == [
        "tests/test_profiles.py",
        "tests/test_startup_plan.py",
        "tests/test_state_recovery.py",
    ]
    assert qa._tests_for_changes(["py_modules/ce_decky/table_store.py"]) == [
        "tests/test_acquisition_prod.py",
        "tests/test_import_snapshot_safety.py",
        "tests/test_table_snapshot_safety.py",
        "tests/test_table_store.py",
    ]


def test_native_extractor_change_also_runs_the_installer_that_mocks_it(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    _touch(tmp_path, "tests/test_ce77_extractor.py", "tests/test_managed_ce.py")
    assert qa._tests_for_changes(["py_modules/ce_decky/ce77_extractor.py"]) == [
        "tests/test_ce77_extractor.py",
        "tests/test_managed_ce.py",
    ]


def test_changed_test_is_its_own_stable_selection(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    _touch(tmp_path, "tests/test_game_identity.py")
    assert qa._tests_for_changes(["tests/test_game_identity.py"]) == ["tests/test_game_identity.py"]


def test_changelog_selects_its_own_shape_check():
    """A changelog edit is the only change that can break the changelog.

    Its mistakes render rather than fail, so the routing has to select the
    check or a hand-edited entry reaches a push unread.
    """
    assert qa._tests_for_changes(["CHANGELOG.md"]) == ["tests/test_changelog_shape.py"]


def test_release_packaging_selects_the_workflow_check():
    """Either side of the artifact-name agreement selects the check for it."""
    assert qa._tests_for_changes([".github/workflows/release.yml"]) == ["tests/test_release_workflow.py"]
    assert qa._tests_for_changes(["scripts/package_plugin.py"]) == ["tests/test_release_workflow.py"]


def test_dependency_manifest_escalates_to_full_backend():
    assert qa._tests_for_changes(["requirements-dev.txt"]) == ["tests"]


def test_frontend_ts_component_tests_are_classified_as_frontend_changes():
    assert qa._is_frontend_change("tests/runtimeClient.test.ts") is True
    assert qa._is_frontend_change("tests/uiModel.test.ts") is True


def test_frontend_core_is_incomplete_instead_of_failed_when_typescript_is_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    monkeypatch.setattr(qa, "_node", lambda: "/usr/bin/node")
    stages = qa._frontend_stages(component=False, core=True, build=False)
    core = next(stage for stage in stages if stage.key == "frontend-core")
    assert core.command == ()
    assert core.incomplete_reason == "frontend toolchain is absent; rerun with --bootstrap"


def test_runtime_client_and_ts_test_changes_select_component_suite(monkeypatch):
    monkeypatch.setattr(qa, "_tests_for_changes", lambda _changed: [])
    monkeypatch.setattr(qa, "_backend_focused_stages", lambda _tests: [])
    monkeypatch.setattr(qa, "_repo_preflight_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_final_stages", lambda: [])
    selected: list[tuple[bool, bool]] = []

    def frontend_stages(*, component: bool, core: bool, build: bool = True):
        selected.append((component, core))
        return [qa.Stage("frontend-fixture")]

    monkeypatch.setattr(qa, "_frontend_stages", frontend_stages)
    qa._auto_stages(["src/runtimeClient.ts"])
    qa._auto_stages(["tests/uiModel.test.ts"])
    assert selected == [(True, False), (True, False)]


def test_unknown_backend_module_escalates_instead_of_silently_skipping(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    assert qa._tests_for_changes(["py_modules/ce_decky/unknown.py"]) == ["tests"]


def test_backend_version_change_is_focused(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    _touch(tmp_path, "tests/test_plugin_lifecycle.py")
    assert qa._tests_for_changes(["py_modules/ce_decky/__init__.py"]) == ["tests/test_plugin_lifecycle.py"]


def test_changed_files_includes_deletions_and_root_commit(monkeypatch):
    commands = []

    def probe(command, timeout=20.0):
        commands.append(command)
        if command[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(command, 0, "py_modules/ce_decky/network.py\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(qa, "_run_probe", probe)
    changed, source = qa._changed_files()
    assert changed == ["py_modules/ce_decky/network.py"] and source == "working tree"
    assert "--diff-filter=ACMRD" in commands[0]

    commands.clear()

    def clean_probe(command, timeout=20.0):
        commands.append(command)
        if command[:2] == ["git", "diff-tree"]:
            return subprocess.CompletedProcess(command, 0, "README.md\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(qa, "_run_probe", clean_probe)
    assert qa._changed_files() == (["README.md"], "HEAD")
    assert "--root" in next(command for command in commands if command[:2] == ["git", "diff-tree"])


def test_wsl_distribution_output_is_decoded_without_nul_noise():
    raw = "Ubuntu\r\nQLEANbuild\r\n".encode("utf-16-le")
    assert qa._decode_wsl_distros(raw) == ["Ubuntu", "QLEANbuild"]


def test_wsl_venv_path_is_stable_and_shell_safe():
    assert qa._wsl_venv_relative("Ubuntu 24.04 LTS") == "build/wsl/ubuntu-24-04-lts/venv"


def test_duplicate_stage_keys_are_run_once():
    stages = qa._dedupe([qa.Stage("one"), qa.Stage("one"), qa.Stage("two")])
    assert [stage.key for stage in stages] == ["one", "two"]


def test_direct_profile_is_one_bounded_opt_in_stage(monkeypatch):
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    stages = qa._profile_stages("direct", [], [])
    assert len(stages) == 1
    assert stages[0].key == "provider-direct"
    assert "direct_provider" in stages[0].command
    assert "-q" not in stages[0].command
    assert stages[0].env == {"CE_DECKY_RUN_LIVE_PROVIDER_TESTS": "1"}
    assert stages[0].rerun_command == "python scripts/qa.py --profile direct"


def test_any_frontend_source_routes_to_the_tests_written_for_it(monkeypatch):
    """The component suite covers `src/`, so a change under it runs the suite.

    The ordinary route selected it only for a test file, the catalog, or a
    short list of core modules, so changing a modal ran a type-check and a
    build and none of the tests that render it. A pass that skips the only
    stage covering the changed file says nothing about the change, and the
    helpers below are imported by those tests exactly as the screens are.
    """
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    covered = (
        "src/modals/TableReviewModal.tsx",
        "src/components/PanelDensity.tsx",
        "src/tableImport.ts",
        "src/providerSelection.ts",
        "src/supportLog.ts",
        "src/steam/client.ts",
    )
    for name in covered:
        keys = [stage.key for stage in qa._auto_stages([name])]
        assert "frontend-component" in keys, f"{name} did not route to the component suite"

    # And not for a change that reaches no frontend file at all.
    assert "frontend-component" not in [stage.key for stage in qa._auto_stages(["README.md"])]


def test_there_is_one_full_gate_and_it_packages(monkeypatch):
    # Two profiles ran the same stages, the second adding half a second of
    # packaging, and the names invited running both: one read as the ordinary
    # one and the other as something kept for releases. Every production change
    # here ends in an install that wants the package, so there is nothing to
    # choose between them and no second name to choose wrongly.
    assert "standard" not in qa.PROFILES
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    keys = [stage.key for stage in qa._profile_stages("release", [], [], [])]
    assert "backend-full" in keys and "frontend-component" in keys
    assert "plugin-package" in keys and "target-package" in keys


class _Orchestration:
    """The real decision flow with only the stage execution replaced.

    Everything the reuse decision is made from is exercised: the record is
    loaded and written where it really is, the fingerprint is computed by the
    real function over a tree this controls, and `main` decides. Only running a
    stage is stubbed, because what it would run is not what is under test.
    """

    def __init__(self, monkeypatch, tmp_path: Path, stages: list[qa.Stage]):
        self.ran: list[str] = []
        self.status = "passed"
        self.tree = "tree-one"
        monkeypatch.setattr(qa, "ROOT", tmp_path)
        monkeypatch.setattr(qa, "QA_ROOT", tmp_path / "build" / "qa")
        monkeypatch.setattr(qa, "REUSE_RECORD", tmp_path / "build" / "qa" / "reuse.json")
        monkeypatch.setattr(qa, "_changed_files", lambda: ([], "HEAD"))
        monkeypatch.setattr(qa, "_profile_stages", lambda *args, **kwargs: list(stages))
        monkeypatch.setattr(qa, "_with_rerun_commands", lambda *args, **kwargs: list(stages))
        monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
        # A stand-in for a cached function has to carry the same surface: the
        # run clears the cache before recording what it proved.
        def fingerprint() -> str:
            return self.tree

        fingerprint.cache_clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(qa, "_tree_fingerprint", fingerprint)
        monkeypatch.setattr(qa, "_run_stage", self._stage)

    def _stage(self, stage: qa.Stage, run_root: Path) -> dict:
        self.ran.append(stage.key)
        return {
            "key": stage.key, "status": self.status, "reason": None, "duration_seconds": 0.1,
            "command": list(stage.command), "rerun_command": "rerun", "output_tail": "", "exit_code": 0,
            "stdout_log": f"build/qa/run/logs/{stage.key}.out.txt",
        }

    def run(self, *argv: str) -> int:
        self.ran.clear()
        return qa.main(["--profile", "release", *argv])


def test_the_run_reuses_a_stage_only_while_its_fingerprint_holds(monkeypatch, tmp_path: Path, capsys):
    # The decision itself, not the helpers underneath it: load the record,
    # decide, run or skip, and write back what this run proved.
    stage = qa.Stage("backend-full", (sys.executable, "-c", "pass"))
    flow = _Orchestration(monkeypatch, tmp_path, [stage])

    assert flow.run() == qa.EXIT_OK
    assert flow.ran == ["backend-full"]
    capsys.readouterr()

    # Nothing moved, so it is not asked again.
    assert flow.run() == qa.EXIT_OK
    assert flow.ran == []
    assert "REUSED     backend-full" in capsys.readouterr().out

    # The tree moved, so it is.
    flow.tree = "tree-two"
    assert flow.run() == qa.EXIT_OK
    assert flow.ran == ["backend-full"]
    capsys.readouterr()

    # And --no-reuse asks regardless.
    assert flow.run("--no-reuse") == qa.EXIT_OK
    assert flow.ran == ["backend-full"]


def test_a_stage_that_failed_is_asked_again_on_the_same_tree(monkeypatch, tmp_path: Path, capsys):
    # Leaving an earlier pass in the record would reuse it on the very next run
    # and report a green gate for a stage that is failing now.
    stage = qa.Stage("backend-full", (sys.executable, "-c", "pass"))
    flow = _Orchestration(monkeypatch, tmp_path, [stage])

    assert flow.run() == qa.EXIT_OK
    flow.status = "failed"
    assert flow.run("--no-reuse") == qa.EXIT_FAILED
    capsys.readouterr()

    flow.status = "passed"
    assert flow.run() == qa.EXIT_OK
    assert flow.ran == ["backend-full"]


def test_a_refused_tree_ends_the_run_instead_of_being_proved_against(monkeypatch, tmp_path: Path, capsys):
    """The repository rules read the tree and nothing else, so they come first.

    A misplaced JSX comment, an unsynchronized version or a stale contract used
    to be found after the backend suite, the component suite, the build and the
    package had all been paid for - the same finding, twenty minutes later. The
    rules now run first and the run stops on one, which takes a release profile
    from about two minutes to under a second. What was not run is named, so a
    run that stops cannot be mistaken for one that never included them.
    """
    stages = [
        qa.Stage("repository", (sys.executable, "-c", "pass"), precondition=True),
        qa.Stage("backend-full", (sys.executable, "-c", "pass")),
        qa.Stage("diff-whitespace", (sys.executable, "-c", "pass")),
    ]
    flow = _Orchestration(monkeypatch, tmp_path, stages)
    flow.status = "failed"

    assert flow.run() == qa.EXIT_FAILED
    assert flow.ran == ["repository"]
    printed = capsys.readouterr().out
    assert "SKIPPED    backend-full" in printed
    assert "not run: repository has to pass first" in printed
    assert "2 not run" in printed

    # A precondition that could not run is not a tree that was refused. The
    # run still owes an answer about everything after it, or a missing
    # dependency would turn the whole gate into a run that quietly does nothing.
    flow.status = "incomplete"
    assert flow.run("--no-reuse") == qa.EXIT_INCOMPLETE
    assert flow.ran == ["repository", "backend-full", "diff-whitespace"]
    capsys.readouterr()

    # A stage that is not a precondition takes nothing down with it: the run
    # still owes an answer about everything after it.
    flow.status = "passed"
    assert flow.run("--no-reuse") == qa.EXIT_OK
    assert flow.ran == ["repository", "backend-full", "diff-whitespace"]


def test_a_provider_profile_contacts_the_provider_however_still_the_tree_is(monkeypatch, tmp_path: Path, capsys):
    # These exist to observe somebody else's server as it is right now, which
    # is the one thing an unchanged tree says nothing about.
    stages = [
        qa.Stage("provider-live", (sys.executable, "-c", "pass")),
        qa.Stage("provider-direct", (sys.executable, "-c", "pass")),
        qa.Stage("plugin-package", (sys.executable, "-c", "pass")),
        qa.Stage("repository", (sys.executable, "-c", "pass")),
    ]
    flow = _Orchestration(monkeypatch, tmp_path, stages)

    assert flow.run() == qa.EXIT_OK
    assert flow.ran == ["provider-live", "provider-direct", "plugin-package", "repository"]
    capsys.readouterr()

    assert flow.run() == qa.EXIT_OK
    # Everything that is about this tree is reused; everything that is about
    # somebody else, or that produces an artifact, runs again.
    assert flow.ran == ["provider-live", "provider-direct", "plugin-package"]


def test_a_stage_is_not_run_again_while_nothing_it_reads_has_moved(monkeypatch, tmp_path: Path):
    # The contract already says to reuse a pass until a covered input changes.
    # Nothing implemented it, so the full gate was run twice in a row for the
    # same answer whenever a package was wanted after it.
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    monkeypatch.setattr(qa, "QA_ROOT", tmp_path / "build" / "qa")
    monkeypatch.setattr(qa, "REUSE_RECORD", tmp_path / "build" / "qa" / "reuse.json")
    qa._tree_fingerprint.cache_clear()
    tracked = tmp_path / "one.py"
    tracked.write_text("first", encoding="utf-8")
    monkeypatch.setattr(qa, "_run_probe", lambda command, **kwargs: SimpleNamespace(returncode=0, stdout="one.py\0"))
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    stage = qa.Stage("fixture", (sys.executable, "-c", "pass"))

    first = qa._stage_fingerprint(stage)
    assert qa._stage_fingerprint(stage) == first

    # Any edit anywhere invalidates it: the fingerprint is the whole tree, not
    # a guess at which files this stage would have read.
    qa._tree_fingerprint.cache_clear()
    tracked.write_text("second", encoding="utf-8")
    assert qa._stage_fingerprint(stage) != first

    # And rewriting a file with the same bytes does not, which is what the gate
    # does to `dist/index.js` on every frontend build: keyed on a timestamp it
    # invalidated its own reuse each time it ran.
    qa._tree_fingerprint.cache_clear()
    changed = qa._stage_fingerprint(stage)
    tracked.write_text("second", encoding="utf-8")
    os.utime(tracked, (1, 1))
    qa._tree_fingerprint.cache_clear()
    assert qa._stage_fingerprint(stage) == changed


def test_a_focused_selection_does_not_throw_away_what_the_gate_established(monkeypatch, tmp_path: Path):
    # Naming a test file is an instruction to run it, so the record is not
    # consulted. Dropping the record instead of not reading it would let one
    # focused selection erase what every other stage had proved about the same
    # tree, and the next full gate would run all of it again.
    monkeypatch.setattr(qa, "REUSE_RECORD", tmp_path / "reuse.json")
    qa._save_reuse_record({"backend-full": "aaa", "frontend-component": "bbb"})

    record = qa._load_reuse_record()
    assert record == {"backend-full": "aaa", "frontend-component": "bbb"}

    # What a focused run does to it: adds its own, keeps the rest.
    record["backend-explicit"] = "ccc"
    qa._save_reuse_record(record)

    assert qa._load_reuse_record() == {
        "backend-full": "aaa", "frontend-component": "bbb", "backend-explicit": "ccc",
    }


def test_a_failing_stage_forgets_what_it_had_proved(monkeypatch, tmp_path: Path):
    # The tree has not changed, and the stage now says no. Leaving the earlier
    # pass in the record would reuse it on the very next run and report a green
    # gate for a stage that is failing.
    monkeypatch.setattr(qa, "REUSE_RECORD", tmp_path / "reuse.json")
    qa._save_reuse_record({"backend-full": "aaa"})

    record = qa._load_reuse_record()
    record.pop("backend-full", None)
    qa._save_reuse_record(record)

    assert qa._load_reuse_record() == {}


def test_a_record_that_cannot_be_read_is_no_record_rather_than_a_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(qa, "REUSE_RECORD", tmp_path / "reuse.json")
    (tmp_path / "reuse.json").write_text("{not json", encoding="utf-8")

    assert qa._load_reuse_record() == {}


def test_a_tree_that_cannot_be_described_is_neither_reused_nor_recorded(monkeypatch, tmp_path: Path):
    # Without git there is nothing to recognise the tree by later, so a run in
    # that state must not write something unrepeatable over what a run that
    # could describe the tree had established about the same stages.
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    monkeypatch.setattr(qa, "_run_probe", lambda command, **kwargs: None)
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    qa._tree_fingerprint.cache_clear()

    assert qa._tree_fingerprint() == ""
    assert qa._stage_fingerprint(qa.Stage("fixture", (sys.executable, "-c", "pass"))) == ""
    qa._tree_fingerprint.cache_clear()


def test_the_thing_a_stage_produces_is_never_reused(monkeypatch):
    # Packaging writes the artifact a deployment installs. Skipping it would
    # leave a summary naming a file this run did not build.
    assert "plugin-package" in qa.NEVER_REUSED
    assert "target-package" in qa.NEVER_REUSED
    assert "backend-full" not in qa.NEVER_REUSED


def test_a_reused_stage_is_reported_as_reused_rather_than_as_a_pass(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    run_root = tmp_path / "build" / "qa" / "run"
    run_root.mkdir(parents=True)
    results = [
        {"key": "backend-full", "status": "reused", "reason": "unchanged since it passed",
         "duration_seconds": 0.0, "output_tail": ""},
        {"key": "repository", "status": "passed", "duration_seconds": 0.8, "output_tail": ""},
    ]

    exit_code = qa._write_results("release", "HEAD", [], results, run_root)

    printed = capsys.readouterr().out
    assert exit_code == qa.EXIT_OK
    assert "REUSED     backend-full" in printed
    assert "1 reused" in printed


def test_release_profile_probes_the_exact_plugin_package(monkeypatch):
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    monkeypatch.setattr(qa, "_backend_full_stage", lambda: qa.Stage("backend-full"))
    monkeypatch.setattr(qa, "_frontend_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_preflight_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_final_stages", lambda: [])

    stages = qa._profile_stages("release", [], [])

    assert [stage.key for stage in stages] == [
        "backend-full",
        "plugin-package",
        "target-package",
    ]
    target_package = stages[2]
    assert target_package.command == (
        sys.executable,
        str(qa.ROOT / "scripts" / "target_package_probe.py"),
    )


def test_full_backend_selection_dominates_extra_runner_tests(monkeypatch):
    monkeypatch.setattr(qa, "_backend_full_stage", lambda: qa.Stage("backend-full", incomplete_reason="fixture gate"))
    stages = qa._backend_focused_stages(["tests", "tests/test_qa.py"])
    assert len(stages) == 1
    assert stages[0].key == "backend-full"
    assert stages[0].incomplete_reason == "fixture gate"


def test_venv_python_is_platform_specific(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "os", SimpleNamespace(name="nt"))
    assert qa._venv_python(tmp_path) == tmp_path / "Scripts" / "python.exe"


def test_explicit_linux_node_is_routed_through_wsl_on_windows(monkeypatch):
    monkeypatch.setattr(qa, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(qa, "_wsl_backend_command", lambda args: (("wsl.exe", *args), None))
    monkeypatch.setattr(qa, "_repo_preflight_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_final_stages", lambda: [])
    stages = qa._profile_stages("auto", [], ["tests/test_service.py::test_fixture"])
    assert [stage.key for stage in stages] == ["backend-linux-explicit"]
    assert stages[0].command[0] == "wsl.exe"
    assert "tests/test_service.py::test_fixture" in stages[0].command


def test_explicit_safe_and_linux_nodes_are_split_on_windows(monkeypatch):
    monkeypatch.setattr(qa, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(qa, "_wsl_backend_command", lambda args: (("wsl.exe", *args), None))
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    monkeypatch.setattr(qa, "_python_has", lambda *_args: True)
    monkeypatch.setattr(qa, "_repo_preflight_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_final_stages", lambda: [])
    stages = qa._profile_stages("auto", [], ["tests/test_qa.py::test_one", "tests/test_service.py::test_two"])
    assert [stage.key for stage in stages] == ["backend-explicit", "backend-linux-explicit"]


def test_a_focused_frontend_selection_runs_only_what_it_names(monkeypatch):
    # The whole component stage is half a minute across eighteen files, so
    # iterating on one of them paid for the other seventeen every time. It is
    # the frontend counterpart of `--pytest`, and it routes the same way: the
    # named selection and the repository guards, nothing else.
    monkeypatch.setattr(qa, "_node", lambda: "node")
    monkeypatch.setattr(qa.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(qa, "_repo_preflight_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_final_stages", lambda: [])
    stages = qa._profile_stages("auto", [], [], ["tests/uiModel.test.ts"])
    assert [stage.key for stage in stages] == ["frontend-explicit"]
    assert stages[0].command[-1] == "tests/uiModel.test.ts"
    # And the rerun a failure prints names the same selection back.
    qa._with_rerun_commands("auto", [], stages, ["tests/uiModel.test.ts"])
    assert "--vitest tests/uiModel.test.ts" in stages[0].rerun_command


def test_focused_backend_and_frontend_selections_run_together(monkeypatch):
    monkeypatch.setattr(qa, "_node", lambda: "node")
    monkeypatch.setattr(qa.Path, "is_file", lambda _self: True)
    monkeypatch.setattr(qa, "_development_python", lambda: Path(sys.executable))
    monkeypatch.setattr(qa, "_python_has", lambda *_args: True)
    monkeypatch.setattr(qa, "_repo_preflight_stages", lambda: [])
    monkeypatch.setattr(qa, "_repo_final_stages", lambda: [])
    stages = qa._profile_stages("auto", [], ["tests/test_qa.py"], ["tests/uiModel.test.ts"])
    assert [stage.key for stage in stages] == ["backend-explicit", "frontend-explicit"]


def test_failed_stage_records_bounded_output_and_exact_rerun(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    result = qa._run_stage(
        qa.Stage(
            "fixture-failure",
            (sys.executable, "-c", "print('useful failure'); raise SystemExit(7)"),
            rerun_command="python scripts/qa.py --profile auto --stage fixture-failure",
        ),
        tmp_path / "qa-run",
    )
    assert result["status"] == "failed"
    assert result["exit_code"] == 7
    assert "useful failure" in result["output_tail"]
    assert result["rerun_command"] == "python scripts/qa.py --profile auto --stage fixture-failure"
    assert (tmp_path / "qa-run" / "logs" / "fixture-failure.out.txt").is_file()


def test_stage_exit_two_is_reported_as_incomplete(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    result = qa._run_stage(
        qa.Stage(
            "fixture-incomplete",
            (sys.executable, "-c", "print('optional dependency missing'); raise SystemExit(2)"),
        ),
        tmp_path / "qa-run",
    )
    assert result["status"] == "incomplete"
    assert result["exit_code"] == 2
    assert "optional dependency missing" in result["output_tail"]


def test_a_run_describes_both_what_failed_and_what_could_not_run(monkeypatch, tmp_path: Path, capsys):
    # A release run can fail one stage and leave another unable to start, and
    # only the failure was ever described. That is exactly what a collection
    # error looks like: the backend suite could not run because one module did
    # not parse, the summary printed the frontend's tail instead, and the run
    # directory holding `qa-failures.json` does not survive the CI runner, so
    # the cause of the red gate was in no output anywhere.
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    run_root = tmp_path / "build" / "qa" / "run"
    run_root.mkdir(parents=True)
    results = [
        {
            "key": "backend-full", "status": "incomplete", "duration_seconds": 3.6,
            "output_tail": "ERROR tests/test_table_code.py - SyntaxError", "exit_code": 2,
            "rerun_command": "python scripts/qa.py --profile release --stage backend-full",
        },
        {
            "key": "frontend-component", "status": "failed", "duration_seconds": 55.6,
            "output_tail": "expected false received true", "exit_code": 1,
            "rerun_command": "python scripts/qa.py --profile release --stage frontend-component",
        },
    ]

    exit_code = qa._write_results("release", "HEAD", [], results, run_root)

    printed = capsys.readouterr().out
    assert exit_code == qa.EXIT_FAILED
    # Each tail is headed by the stage it belongs to, because two of them side
    # by side are unreadable otherwise, and each carries its own rerun.
    assert "frontend-component tail:" in printed
    assert "backend-full tail:" in printed
    assert "SyntaxError" in printed and "expected false received true" in printed
    assert printed.count("  rerun: ") == 2


def test_a_release_run_names_the_package_it_built_and_its_digest(monkeypatch, tmp_path: Path, capsys):
    # The command that comes next is the install, and it takes the digest as a
    # mandatory argument rather than a discovery guess. Both facts were already
    # produced by this run and written into two different stage logs, so every
    # deployment began by reading them back out of those.
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    run_root = tmp_path / "build" / "qa" / "run"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (logs / "plugin-package.out.txt").write_text(
        f"building\n{tmp_path / 'artifacts' / 'CE-Decky-v0.9.14.zip'}\n", encoding="utf-8",
    )
    (logs / "target-package.out.txt").write_text(
        json.dumps({"ok": True, "sha256": "a" * 64, "version": "0.9.14"}), encoding="utf-8",
    )
    results = [
        {
            "key": "plugin-package", "status": "passed", "duration_seconds": 0.5,
            "stdout_log": "build/qa/run/logs/plugin-package.out.txt", "output_tail": "",
        },
        {
            "key": "target-package", "status": "passed", "duration_seconds": 0.1,
            "stdout_log": "build/qa/run/logs/target-package.out.txt", "output_tail": "",
        },
    ]

    exit_code = qa._write_results("release", "HEAD", [], results, run_root)

    printed = capsys.readouterr().out
    assert exit_code == qa.EXIT_OK
    assert "  package: artifacts/CE-Decky-v0.9.14.zip" in printed
    assert f"  sha256: {'a' * 64}" in printed
    summary = json.loads((run_root / "qa-summary.json").read_text(encoding="utf-8"))
    assert summary["artifact"] == {"package": "artifacts/CE-Decky-v0.9.14.zip", "sha256": "a" * 64}


def test_a_run_that_packaged_nothing_says_nothing_about_a_package(monkeypatch, tmp_path: Path, capsys):
    # A convenience line that cannot be produced is left out rather than
    # guessed at, and a profile that never packaged has no package to name.
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    run_root = tmp_path / "build" / "qa" / "run"
    (run_root / "logs").mkdir(parents=True)
    results = [{"key": "repository", "status": "passed", "duration_seconds": 0.4, "output_tail": ""}]

    qa._write_results("repo", "HEAD", [], results, run_root)

    printed = capsys.readouterr().out
    assert "package:" not in printed
    assert "sha256:" not in printed
    assert "artifact" not in json.loads((run_root / "qa-summary.json").read_text(encoding="utf-8"))


def test_a_packaging_stage_whose_log_is_gone_does_not_fail_the_summary(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    run_root = tmp_path / "build" / "qa" / "run"
    (run_root / "logs").mkdir(parents=True)
    results = [{
        "key": "plugin-package", "status": "passed", "duration_seconds": 0.5,
        "stdout_log": "build/qa/run/logs/plugin-package.out.txt", "output_tail": "",
    }]

    assert qa._write_results("release", "HEAD", [], results, run_root) == qa.EXIT_OK
    assert "package:" not in capsys.readouterr().out

    # And a result that does not carry a log path at all is the same answer:
    # this line is a convenience, and one that cannot be produced is left out
    # rather than taking the whole summary down with it.
    assert qa._write_results(
        "release", "HEAD", [],
        [{"key": "plugin-package", "status": "passed", "duration_seconds": 0.5, "output_tail": ""}],
        run_root,
    ) == qa.EXIT_OK
    assert "package:" not in capsys.readouterr().out


def test_generated_rerun_stays_inside_bounded_qa_runner():
    stage = qa.Stage("backend-full", (sys.executable, "-m", "pytest"))
    qa._with_rerun_commands("release", [], [stage])
    assert stage.rerun_command == "python scripts/qa.py --profile release --stage backend-full"
    assert "-m pytest" not in stage.rerun_command


def test_incomplete_dependency_stage_rerun_adds_bootstrap():
    stage = qa.Stage("backend-focused", incomplete_reason="Python dev dependencies are absent; rerun with --bootstrap")
    qa._with_rerun_commands("auto", [], [stage])
    assert stage.rerun_command == "python scripts/qa.py --profile auto --bootstrap --stage backend-focused"


def test_stage_selection_rejects_unrelated_stage():
    try:
        qa._select_stage([qa.Stage("one")], ["two"])
    except ValueError as exc:
        assert "available: one" in str(exc)
    else:
        raise AssertionError("missing stage should fail")


def test_several_stages_can_be_named_and_keep_the_profile_s_order():
    """Naming two used to run the second and say nothing about the first.

    `--stage` took one value, and argparse keeps the last of a repeated option
    rather than refusing it, so a run asked for the build and the smoke test
    ran only the smoke test. Nothing said so, and what looked like a rebuilt
    bundle was the one the previous run had left behind.
    """
    stages = [qa.Stage("first"), qa.Stage("second"), qa.Stage("third")]
    selected = qa._select_stage(stages, ["third", "first"])
    assert [stage.key for stage in selected] == ["first", "third"]
    # Naming one twice is one stage, and one name that is not in the profile is
    # still a refusal that says which.
    assert [stage.key for stage in qa._select_stage(stages, ["second", "second"])] == ["second"]
    try:
        qa._select_stage(stages, ["first", "nope"])
    except ValueError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("an unknown stage should fail even beside a known one")


def test_the_summary_prints_the_one_command_its_artifact_is_for(monkeypatch):
    """The package and its digest exist for a deployment, so it prints that too.

    They were printed alone, and the command they are for was then retyped from
    memory against a helper whose flags had to be read first.
    """
    monkeypatch.setattr(qa.os, "name", "posix")
    command = qa._install_command({
        "package": "artifacts/CE-Decky-v0.9.25.zip",
        "sha256": "f" * 64,
    })
    assert command == (
        "python3 scripts/target_plugin_install.py install artifacts/CE-Decky-v0.9.25.zip "
        f"--sha256 {'f' * 64} --replace"
    )
    # Nothing to install without both halves, and nothing offered on a host that
    # cannot install anything.
    assert qa._install_command({"package": "artifacts/x.zip"}) is None
    assert qa._install_command({}) is None
    monkeypatch.setattr(qa.os, "name", "nt")
    assert qa._install_command({"package": "artifacts/x.zip", "sha256": "a" * 64}) is None


def test_a_run_that_failed_does_not_offer_its_package_for_installation(tmp_path, capsys, monkeypatch):
    """A package exists as soon as packaging does, and a later stage can still refuse.

    Offering the command under a red result is offering to install the exact
    tree the gate has just rejected.
    """
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    monkeypatch.setattr(qa.os, "name", "posix")
    monkeypatch.setattr(qa, "_packaged_artifact", lambda results: {
        "package": "artifacts/CE-Decky-v0.9.25.zip", "sha256": "b" * 64,
    })
    passed = [{"key": "plugin-package", "status": "passed", "duration_seconds": 0.5}]
    failed = passed + [{"key": "diff-whitespace", "status": "failed", "duration_seconds": 0.1}]

    qa._write_results("release", "working tree", [], failed, tmp_path)
    assert "install:" not in capsys.readouterr().out

    qa._write_results("release", "working tree", [], passed, tmp_path)
    assert "install: python3 scripts/target_plugin_install.py" in capsys.readouterr().out


def test_stable_tag_is_refused_while_work_is_still_unverified(tmp_path, monkeypatch):
    """CI proves the package is well formed, not that a device answered for it.

    A tag otherwise walked straight from metadata plus QA to a published
    release. The authority is the one section of `docs/FIELD_NOTES.md` whose
    job is to say what is still owed, so closing its last row is what permits
    a stable tag.
    """
    from scripts import check_release

    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / "package.json").read_text(encoding="utf-8"))["version"]

    # What the section is read as, proven against a written one rather than
    # against whatever the real file holds today. Settling the last row is the
    # whole point of this gate, so a test that needs an unsettled row to prove
    # the gate works is a test that fails on the day it is finally satisfied.
    pretend = tmp_path / "docs"
    pretend.mkdir()
    (pretend / "FIELD_NOTES.md").write_text(
        "## 5. Still worth checking\n\n"
        "| Subject | What would settle it |\n|---|---|\n"
        "| A pretend subject | somebody looks |\n\n"
        "## 6. Upstream references\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_release, "ROOT", tmp_path)
    assert check_release._unverified_subjects() == ["A pretend subject"]
    (pretend / "FIELD_NOTES.md").write_text(
        "## 5. Still worth checking\n\n| Subject | What would settle it |\n|---|---|\n\n## 6. Upstream references\n",
        encoding="utf-8",
    )
    assert check_release._unverified_subjects() == []
    monkeypatch.undo()

    # And that a tag is refused for what that read returns, without needing the
    # repository to be in either state.
    monkeypatch.setattr(check_release, "_unverified_subjects", lambda: ["A pretend subject"])
    monkeypatch.setattr(sys, "argv", ["check_release.py", "--tag", f"v{version}"])
    with pytest.raises(SystemExit) as refused:
        check_release.main()
    assert "still unverified" in str(refused.value) and "A pretend subject" in str(refused.value)
    monkeypatch.undo()

    # The real tree then agrees with its own section either way: a row refuses
    # the tag, an empty section permits it.
    unverified = check_release._unverified_subjects()
    tagged = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_release.py"), "--tag", f"v{version}"],
        capture_output=True, text=True,
    )
    assert tagged.returncode == (1 if unverified else 0)

    # Metadata validation without a tag is unaffected.
    assert subprocess.run(
        [sys.executable, str(root / "scripts" / "check_release.py")],
        capture_output=True, text=True,
    ).returncode == 0


def test_every_test_the_router_names_by_hand_exists():
    """A routing rule that names a deleted test is a rule that does nothing.

    The selection used to be filtered by existence on the way out, so a rule
    whose test had been deleted still looked like a working rule: it simply
    selected nothing, and the change it was meant to cover ran no test at all.
    Derived names are checked where they are derived; every literal here has to
    resolve.
    """
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts" / "qa.py").read_text(encoding="utf-8")
    named = sorted(set(re.findall(r'"(tests/test_[a-z_]+\.py)"', source)))
    assert named, "the router names tests by hand; this test guards those names"
    missing = [name for name in named if not (root / name).is_file()]
    assert not missing, f"routing rules name tests that do not exist: {missing}"


def test_changing_a_fixture_runs_the_tests_that_read_it():
    """A captured provider answer is re-captured when a site changes its markup.

    That is precisely when the parsers reading it have to run, and routing it to
    nothing would pass the change that breaks them.
    """
    root = Path(__file__).resolve().parents[1]
    fixtures = sorted((root / "tests" / "fixtures").iterdir())
    assert fixtures, "this test guards the fixtures the parsers are checked against"
    for fixture in fixtures:
        selected = qa._tests_for_changes([f"tests/fixtures/{fixture.name}"])
        assert selected, f"changing {fixture.name} selects no test"
        for name in selected:
            assert fixture.name in (root / name).read_text(encoding="utf-8")


VITEST_FAILURE = """
 ✓ tests/other.test.ts (4 tests) 12ms
 ❯ tests/uiModel.test.ts (2 tests | 1 failed) 8ms

⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯

 FAIL  tests/uiModel.test.ts > controller UI model > calls a copy Fixed
AssertionError: expected 'Fixed' to be 'Repaired' // Object.is equality

Expected: "Repaired"
Received: "Fixed"

 ❯ tests/uiModel.test.ts:164:46

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 5 passed (6)
   Duration  0.80s
"""

PYTEST_FAILURE = """
........F                                                                [100%]
=================================== FAILURES ===================================
_______________________ test_a_flag_is_not_safe ________________________
tests/test_ct_hooks.py:58: in test_a_flag_is_not_safe
    assert "bEnableVitals" in safe
E   AssertionError: assert 'bEnableVitals' in frozenset({'bEnablePlain'})
=========================== short test summary info ============================
FAILED tests/test_ct_hooks.py::test_a_flag_is_not_safe - AssertionError: assert
"""


def test_a_failing_run_says_what_failed_and_why_without_being_run_again():
    """A tail is the wrong end of a test run.

    `pytest` ends with a summary a tail reaches; `vitest` ends with a count and
    a footer while the assertion, its diff and its line are hundreds of lines
    earlier, so reading a frontend failure meant running the same selection a
    second time with a filter on the output - which costs exactly as long as the
    run did.
    """
    vitest = qa._failure_digest(VITEST_FAILURE)
    assert vitest["failed_tests"] == ["tests/uiModel.test.ts > controller UI model > calls a copy Fixed"]
    assert "expected 'Fixed' to be 'Repaired'" in str(vitest["excerpt"])
    assert 'Expected: "Repaired"' in str(vitest["excerpt"])
    assert "tests/uiModel.test.ts:164:46" in str(vitest["excerpt"])

    backend = qa._failure_digest(PYTEST_FAILURE)
    # Named the way the runner names it, without repeating the word the line
    # above already said.
    assert backend["failed_tests"] == ["tests/test_ct_hooks.py::test_a_flag_is_not_safe"]
    assert "assert 'bEnableVitals' in frozenset" in str(backend["excerpt"])


def test_a_stage_that_is_not_a_test_run_keeps_its_tail(tmp_path, capsys, monkeypatch):
    """The last thing a command said is the whole of what happened to it.

    Only a test run has a banner to start reading from; a packaging step that
    died has one message, and cutting it for a banner it never printed would
    leave the reader with nothing.
    """
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    digest = qa._failure_digest("rollup: could not resolve ./missing\n")
    assert digest == {"failed_tests": [], "excerpt": ""}

    qa._write_results("release", "working tree", [], [{
        "key": "frontend-build", "status": "failed", "duration_seconds": 0.2,
        "output_tail": "rollup: could not resolve ./missing", **digest,
    }], tmp_path)
    printed = capsys.readouterr().out
    assert "frontend-build tail:" in printed
    assert "could not resolve ./missing" in printed


def test_a_failing_stage_prints_the_names_and_the_first_reason(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(qa, "ROOT", tmp_path)
    qa._write_results("auto", "working tree", [], [{
        "key": "frontend-explicit", "status": "failed", "duration_seconds": 0.8,
        "output_tail": VITEST_FAILURE[-1600:], **qa._failure_digest(VITEST_FAILURE),
    }], tmp_path)
    printed = capsys.readouterr().out

    assert "frontend-explicit failed: tests/uiModel.test.ts > controller UI model > calls a copy Fixed" in printed
    assert "frontend-explicit why:" in printed
    assert 'Received: "Fixed"' in printed


def test_narrowing_a_selection_uses_each_runner_s_own_filter():
    """One case of the component suite is under two seconds; the file is 45."""
    [backend] = qa._explicit_pytest_stages(["tests/test_qa.py"], "says what failed")
    # In the syntax `-k` parses: `pytest` reads it as an expression, so the
    # words of a phrase are joined into the one that means the same thing.
    assert backend.command[-2:] == ("-k", "says and what and failed")
    [frontend] = qa._explicit_vitest_stages(["tests/uiModel.test.ts"], "Fixed rather than Local")
    assert frontend.command[-2:] == ("-t", "Fixed rather than Local")
    # And a selection nobody narrowed is the selection itself.
    [plain] = qa._explicit_vitest_stages(["tests/uiModel.test.ts"])
    assert plain.command[-1] == "tests/uiModel.test.ts"


def test_a_narrowed_run_that_matched_nothing_is_not_a_run_that_passed():
    """`vitest -t` skips every test in the file and exits green.

    A reader who mistyped a name would be told their case passes when it never
    ran, which is the failure mode of narrowing and the reason this is checked
    rather than trusted.
    """
    assert qa._ran_no_cases(" Test Files  1 skipped (1)\n      Tests  126 skipped (126)\n")
    # `pytest` says it in words and in an exit code; the words are enough.
    assert qa._ran_no_cases("no tests ran in 0.01s\n")
    # And a run that did something is left alone, including one that only
    # failed, and one whose runner prints no summary line at all.
    assert not qa._ran_no_cases(" Test Files  1 failed (1)\n      Tests  1 failed | 5 passed (6)\n")
    assert not qa._ran_no_cases(".                                            [100%]\n")
    # And a test that prints a line of its own beginning `Tests ` is not the
    # runner's verdict about the run it is part of.
    assert not qa._ran_no_cases("Tests were skipped by the code under test\n")


def test_a_phrase_is_narrowed_the_way_each_runner_reads_it():
    """`-k` is an expression and `-t` is text, so the same `--name` is both.

    A phrase passed to `-k` unchanged is a syntax error rather than a filter,
    which is a run nobody gets an answer from.
    """
    assert qa._pytest_filter("holds off the flags") == "holds and off and the and flags"
    # An expression somebody wrote on purpose is theirs.
    assert qa._pytest_filter("quiesce and not stop") == "quiesce and not stop"
    assert qa._pytest_filter("quiesce") == "quiesce"


def test_a_pytest_selection_that_collected_nothing_says_so_rather_than_a_number(tmp_path, monkeypatch):
    """`pytest` prints not one character when its `-k` matched nothing.

    Both streams are empty and the only signal is the exit code, so a narrowed
    stage that ends that way is named rather than reported as `exit 5`.
    """
    monkeypatch.setattr(qa, "ROOT", tmp_path)

    class Completed:
        returncode = qa.PYTEST_NOTHING_COLLECTED
        stdout = ""
        stderr = ""

    monkeypatch.setattr(qa.subprocess, "run", lambda *args, **kwargs: Completed())
    stage = qa.Stage("backend-explicit", ("python", "-m", "pytest"), named_cases=True)
    result = qa._run_stage(stage, tmp_path)

    assert result["status"] == "failed"
    assert result["reason"] == "--name matched no test, so nothing ran"
