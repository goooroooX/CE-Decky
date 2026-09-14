from __future__ import annotations

from pathlib import Path

from scripts import target_ce_launch_probe


def test_probe_process_scan_finds_only_the_exact_owned_descriptor(tmp_path: Path):
    proc = tmp_path / "proc"
    for pid, descriptor, digest in (
        (10, "Z:\\session\\descriptor.txt", "d" * 64),
        (11, "Z:\\other\\descriptor.txt", "d" * 64),
        (12, "Z:\\session\\descriptor.txt", "e" * 64),
    ):
        root = proc / str(pid)
        root.mkdir(parents=True)
        root.joinpath("environ").write_bytes(
            f"CE_DECKY_DESCRIPTOR={descriptor}\0CE_DECKY_DESCRIPTOR_SHA256={digest}\0".encode("utf-8")
        )

    assert target_ce_launch_probe._matching_owned_pids(
        "Z:\\session\\descriptor.txt", "d" * 64, proc
    ) == [10]


def test_probe_prefix_ownership_is_bounded_and_accepts_the_current_owner(tmp_path: Path):
    prefix = tmp_path / "prefix"
    (prefix / "pfx/drive_c").mkdir(parents=True)
    (prefix / "pfx/drive_c/file.txt").write_text("owned", encoding="utf-8")
    expected_uid = prefix.stat().st_uid

    result = target_ce_launch_probe._prefix_ownership(prefix, expected_uid)

    assert result["ok"] is True
    assert result["truncated"] is False
    assert result["foreign"] == []
    assert int(result["scanned"]) >= 4
