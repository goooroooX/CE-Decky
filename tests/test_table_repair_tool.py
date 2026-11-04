from hashlib import sha256
from pathlib import Path

import pytest

from scripts import table_repair as tool


def source(tmp_path, extra=""):
    blob = (f'<CheatTable><CheatEntries><CheatEntry><ID>7</ID><Description>Example</Description>'
            f'<VariableType>4 Bytes</VariableType><Address>Game.exe+0x123450</Address>'
            f'</CheatEntry></CheatEntries>{extra}</CheatTable>').encode()
    path = tmp_path / "source.CT"
    path.write_bytes(blob)
    return path, blob, sha256(blob).hexdigest()


def recipe(digest, **change):
    return {"schema": 1, "source_sha256": digest,
            "substitutions": [{"old": "0x123450", "new": "0x123890", "count": 1, **change}]}


def test_derivation_keeps_original_bytes_and_produces_exact_reviewable_copy(tmp_path):
    path, original, digest = source(tmp_path)
    output = tmp_path / "repaired.CT"
    report = tool.derive(path, recipe(digest), output)
    assert path.read_bytes() == original
    assert output.read_bytes() == original.replace(b"0x123450", b"0x123890")
    assert report["derived_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert report["diff_complete"] and "-" in report["diff"]
    assert report["source_sha256"] == digest


@pytest.mark.parametrize("failure", ["digest", "count", "metadata", "existing", "same-path", "signed"])
def test_unreviewed_edits_never_publish_or_overwrite_a_table(tmp_path, failure):
    path, original, digest = source(tmp_path, '<Signature>not-a-real-signature</Signature>' if failure == "signed" else "")
    output = path if failure == "same-path" else tmp_path / "repaired.CT"
    edit = recipe(digest)
    if failure == "digest":
        edit["source_sha256"] = "b" * 64
    if failure == "count":
        edit["substitutions"][0]["count"] = 2
    if failure == "metadata":
        edit["substitutions"] = [{"old": "Example", "new": "Changed", "count": 1}]
    if failure == "existing":
        output.write_bytes(b"keep me")
    with pytest.raises(ValueError):
        tool.derive(path, edit, output)
    assert path.read_bytes() == original
    if failure == "existing":
        assert output.read_bytes() == b"keep me"
    elif failure != "same-path":
        assert not output.exists()
    assert not list(tmp_path.glob(".table-repair-*"))


def test_static_inventory_does_not_execute_table_code(tmp_path):
    path, blob, digest = source(tmp_path, '<LuaScript>error("must never run")</LuaScript>')
    report = tool.inventory(path, digest)
    assert report["records"][0]["id"] == 7
    assert report["literal_candidates"][0]["literal"] == "0x123450"
    assert path.read_bytes() == blob


def test_script_export_decodes_entities_without_executing_or_creating_partial_missing_ids(tmp_path):
    path, blob, _ = source(tmp_path)
    blob = blob.replace(b'<Address>Game.exe+0x123450</Address>',
        b'<AssemblerScript>if 1 &lt; 2 then error("must not execute") end</AssemblerScript>')
    path.write_bytes(blob)
    digest = sha256(blob).hexdigest()
    output = tmp_path / "scripts"
    with pytest.raises(ValueError, match="missing"):
        tool.extract_records(path, digest, [7, 8], output)
    assert not output.exists()
    report = tool.extract_records(path, digest, [7], output)
    exported = (output / "record-7.txt").read_text()
    assert "1 < 2" in exported and "must not execute" in exported
    assert report["source_sha256"] == digest
    assert path.read_bytes() == blob
    assert report["scripts"][0]["sha256"] == sha256((output / "record-7.txt").read_bytes()).hexdigest()


def test_namespaced_fields_are_inventoried_and_only_address_text_changes(tmp_path):
    path, blob, _ = source(tmp_path)
    blob = blob.replace(b"<CheatTable>", b'<CheatTable xmlns="urn:example">')
    path.write_bytes(blob)
    digest = sha256(blob).hexdigest()
    assert tool.inventory(path, digest)["literal_candidates"][0]["literal"] == "0x123450"
    output = tmp_path / "repaired.CT"
    tool.derive(path, recipe(digest), output)
    assert output.read_bytes() == blob.replace(b"0x123450", b"0x123890")


def test_xml_comments_cannot_open_fake_editable_regions(tmp_path):
    path, blob, _ = source(tmp_path, '<!-- <Address>0x123450</Address> -->')
    digest = sha256(blob).hexdigest()
    output = tmp_path / "repaired.CT"
    with pytest.raises(ValueError, match="outside script/address"):
        tool.derive(path, recipe(digest, count=2), output)
    assert not output.exists() and path.read_bytes() == blob


def test_cdata_and_quoted_attributes_do_not_move_edit_boundaries(tmp_path):
    path, blob, _ = source(tmp_path)
    blob = blob.replace(b'<Address>Game.exe+0x123450</Address>',
        b'<AssemblerScript note="a > b"><![CDATA[local v="<Address>0x123450</Address>"]]></AssemblerScript>')
    path.write_bytes(blob)
    output = tmp_path / "repaired.CT"
    tool.derive(path, recipe(sha256(blob).hexdigest()), output)
    assert output.read_bytes() == blob.replace(b"0x123450", b"0x123890")
