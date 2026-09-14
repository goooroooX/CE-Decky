from __future__ import annotations

from pathlib import Path

from scripts import target_archive_probe


def test_find_sevenzip_uses_production_preference_order(monkeypatch):
    seen: list[str] = []

    def which(name: str):
        seen.append(name)
        return "/usr/bin/7za" if name == "7za" else None

    monkeypatch.setattr(target_archive_probe.shutil, "which", which)
    assert target_archive_probe.find_sevenzip() == str(Path("/usr/bin/7za").resolve())
    assert seen == ["7z", "7za"]


def test_probe_report_contains_only_target_host_7z_contract(monkeypatch):
    monkeypatch.setattr(
        target_archive_probe,
        "_sevenzip_checks",
        lambda _root, _tool: (
            [
                target_archive_probe.Check("sevenzip_info", True, "ok"),
                target_archive_probe.Check("sevenzip_unicode_roundtrip", True, "ok"),
            ],
            "7-Zip fixture",
        ),
    )

    report = target_archive_probe.run_probe("/usr/bin/7z")
    assert report["ok"] is True
    assert report["schema"] == 2
    assert report["sevenzip"] == "/usr/bin/7z"
    assert [item["name"] for item in report["checks"]] == [
        "sevenzip_info",
        "sevenzip_unicode_roundtrip",
    ]
    assert "ZIP/password rejection remains cloud-prevalidated" in report["fixture_policy"]


def test_main_reports_missing_host_tool_as_blocked(monkeypatch, capsys):
    monkeypatch.setattr(target_archive_probe, "find_sevenzip", lambda: None)
    assert target_archive_probe.main() == 2
    output = capsys.readouterr().out
    assert '"blocked": true' in output
    assert '"schema": 2' in output
    assert "7z/7za/7zr not found" in output
