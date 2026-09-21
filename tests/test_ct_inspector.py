from pathlib import Path

import pytest



def test_inspector_rejects_symlinked_blob(tmp_path):
    from hashlib import sha256
    from ce_decky.ct_inspector import inspect_table
    real = tmp_path / "real.CT"
    data = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'
    real.write_bytes(data)
    link = tmp_path / "link.CT"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="missing or is not a regular file"):
        inspect_table(link, sha256(data).hexdigest())


def test_inspector_rejects_dtd_from_same_snapshot(tmp_path):
    from hashlib import sha256
    from ce_decky.ct_inspector import inspect_table
    path = tmp_path / "bad.CT"
    data = b'<!DOCTYPE CheatTable [<!ENTITY x "boom">]><CheatTable><CheatEntries/></CheatTable>'
    path.write_bytes(data)
    with pytest.raises(ValueError, match="DTD/entity"):
        inspect_table(path, sha256(data).hexdigest())


def test_process_hints_come_from_assembler_scripts_and_aobscanmodule(tmp_path: Path):
    from ce_decky.ct_inspector import inspect_table
    from ce_decky.table_store import TableStore

    """Real tables name their process in scripts far more often than in Address.

    Both forms are taken from tables downloaded for the reviewed target: Cheat
    Engine's own `{ Game : <exe> }` script header, and the `aobscanmodule`
    idiom where the module sits between two commas.
    """
    source = tmp_path / "hints.CT"
    source.write_text(
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"Header"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        '<AssemblerScript>{ Game   : Lumenhollow-Win64-Shipping.exe\n  Version: \n}\n[ENABLE]\n</AssemblerScript>'
        '</CheatEntry>'
        '<CheatEntry><ID>2</ID><Description>"Scan"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        '<AssemblerScript>[ENABLE]\naobscanmodule(jumpsig,LumenHollow-Win64-Shipping.exe,89 81 78 04)\n</AssemblerScript>'
        '</CheatEntry>'
        '<CheatEntry><ID>3</ID><Description>"Module only"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        '<AssemblerScript>[ENABLE]\naobscanmodule(rtx,VCRUNTIME140.dll,47 8B 8C 82)\n</AssemblerScript>'
        '</CheatEntry>'
        '</CheatEntries></CheatTable>'
    )
    artifact = TableStore(tmp_path / "store").import_ct(str(source))
    inspection = inspect_table(Path(artifact.blob_path), artifact.sha256)
    # A DLL module is not a process and must not become a target candidate.
    assert inspection.process_candidates == ("LumenHollow-Win64-Shipping.exe", "Lumenhollow-Win64-Shipping.exe")


def _inspect(data: bytes, tmp_path):
    from hashlib import sha256
    from ce_decky.ct_inspector import inspect_table

    tmp_path.mkdir(parents=True, exist_ok=True)
    blob = tmp_path / "table.CT"
    blob.write_bytes(data)
    return inspect_table(blob, sha256(data).hexdigest())


def test_a_table_that_carries_its_own_window_is_executable_content(tmp_path):
    # Cheat Engine instantiates a designed form stored in the table when the
    # table is opened, and that form's controls carry Lua event handlers. Such a
    # table runs code and puts a window on the running game while carrying no
    # `<LuaScript>` at all, and Review used to describe it as having no
    # executable-content markers at all.
    data = b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
  </CheatEntries>
  <Forms><CustomForm Name="Trainer">TFVLTQ==</CustomForm></Forms>
</CheatTable>"""
    inspection = _inspect(data, tmp_path)
    assert inspection.has_forms is True
    assert inspection.has_lua is False
    assert inspection.as_dict()["has_forms"] is True


def test_a_table_without_a_designed_window_says_so(tmp_path):
    data = b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
  </CheatEntries>
</CheatTable>"""
    assert _inspect(data, tmp_path).has_forms is False


def test_an_empty_window_container_is_not_executable_window_content(tmp_path):
    # `<Forms>` can be present and hold nothing, exactly as `<Files>` can.
    # Counting the container itself made Review call such a table executable
    # window content while **Look inside this table**, which lists the windows
    # themselves, had nothing at all to show: the one screen where the answer
    # has to be the same, because it is what justifies the consent beside it.
    data = b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
  </CheatEntries>
  <Forms/>
</CheatTable>"""
    inspection = _inspect(data, tmp_path)
    assert inspection.has_forms is False
    assert inspection.as_dict()["has_forms"] is False


def test_an_unrecognised_child_of_the_window_container_is_still_a_window(tmp_path):
    # The container's children are windows whatever Cheat Engine calls them, so
    # a tag this does not know is classified as one rather than as harmless.
    data = b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <Forms><UDF2><Handler>showMessage('hi')</Handler></UDF2></Forms>
</CheatTable>"""
    assert _inspect(data, tmp_path).has_forms is True


def test_two_labels_that_clean_down_to_the_same_text_stay_distinguishable(tmp_path):
    # A label carrying an invisible character is cleaned rather than refused,
    # and two lines whose labels differ only in what was removed then render as
    # the same controller choice. The value is what gets written, so the value
    # is what tells them apart; both semantic values are carried unchanged.
    data = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"Weapon"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>'
        '<DropDownList>1:Fire\n2:Fi​re\n3:Fire</DropDownList>'
        '</CheatEntry></CheatEntries></CheatTable>'
    ).encode("utf-8")
    inspection = _inspect(data, tmp_path)
    control = inspection.controls[0]
    assert [value for value, _ in control.dropdown_values] == ["1", "2", "3"]
    labels = [label for _, label in control.dropdown_values]
    assert len(set(labels)) == 3
    assert labels[0] == "Fire"


def test_a_dropdown_that_ends_exactly_on_the_limit_reports_no_dropped_value(tmp_path):
    # The marker used to be raised as soon as the accepted list reached the
    # limit, before anything proved another value existed, and one marker stood
    # for the whole tail however long it was. Review presents this number as an
    # exact count of values the table could not be taken with.
    from ce_decky.ct_inspector import MAX_DROPDOWN_VALUES

    def table(count: int) -> bytes:
        lines = "\n".join(f"{index}:Value {index}" for index in range(count))
        return (
            '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
            '<CheatEntry><ID>1</ID><Description>"Weapon"</Description>'
            '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>'
            f'<DropDownList>{lines}</DropDownList>'
            '</CheatEntry></CheatEntries></CheatTable>'
        ).encode("utf-8")

    exact = _inspect(table(MAX_DROPDOWN_VALUES), tmp_path)
    assert len(exact.controls[0].dropdown_values) == MAX_DROPDOWN_VALUES
    assert exact.dropped_values == 0

    over = _inspect(table(MAX_DROPDOWN_VALUES + 5), tmp_path / "over")
    assert len(over.controls[0].dropdown_values) == MAX_DROPDOWN_VALUES
    assert over.dropped_values == 5


def test_one_oversized_list_costs_that_list_rather_than_the_whole_table(tmp_path):
    """The last limit in here that still refused a file for one record.

    Observed on this device: a real table of 323 entries, downloaded, verified
    and stored, could not be read at all because one of its three item lists
    was over the byte ceiling. It imported and joined the game's library, so it
    was then offered as a local table in every later search and every press
    ended in the same refusal. Every other limit in this parser already drops
    what it cannot carry and counts it.
    """
    from ce_decky.ct_inspector import MAX_DROPDOWN_BYTES

    line = "1234567:" + "V" * 64
    lines = "\n".join(f"{index}:{line}" for index in range(MAX_DROPDOWN_BYTES // 64))
    data = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"Item"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>'
        f'<DropDownList>{lines}</DropDownList></CheatEntry>'
        '<CheatEntry><ID>2</ID><Description>"Health"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+20</Address>'
        '</CheatEntry></CheatEntries></CheatTable>'
    ).encode("utf-8")

    inspection = _inspect(data, tmp_path)
    # The table is readable, both of its records are there, and what could not
    # be carried is counted rather than silent.
    assert [control.description for control in inspection.controls] == ["Item", "Health"]
    assert inspection.controls[0].dropdown_values == ()
    # As a list rather than as a value: one of these is a whole picker gone from
    # a record, and the list this was added for holds 6508 values.
    assert (inspection.dropped_value_lists, inspection.dropped_values) == (1, 0)


def test_the_store_and_review_classify_the_same_bytes_the_same_way(tmp_path):
    # Two readers used to answer this separately: the importer treated any
    # `<LuaScript>` element as executable content and Review required a script
    # to have something in it, so Review could say that no executable-content
    # marker was found directly above the confirmation that the table can
    # execute code.
    from ce_decky.table_store import TableStore

    def classify(name: str, body: str):
        source = tmp_path / name
        source.write_text(
            '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
            '<CheatEntry><ID>1</ID><Description>"Health"</Description>'
            '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>'
            f'</CheatEntries>{body}</CheatTable>'
        )
        artifact = TableStore(tmp_path / f"store-{name}").import_ct(str(source))
        inspection = _inspect(source.read_bytes(), tmp_path / f"blob-{name}")
        return artifact, inspection

    empty, empty_inspection = classify("empty.CT", "<LuaScript></LuaScript>")
    assert empty.has_lua is False and empty_inspection.has_lua is False
    assert empty.executable_content is False

    real, real_inspection = classify("lua.CT", "<LuaScript>print('hi')</LuaScript>")
    assert real.has_lua is True and real_inspection.has_lua is True
    assert real.executable_content is True

    formed, formed_inspection = classify("form.CT", '<Forms><CustomForm Name="T">TFVLTQ==</CustomForm></Forms>')
    assert formed.has_forms is True and formed_inspection.has_forms is True
    assert formed.executable_content is True

    # The container is not itself a payload, and both readers count the files.
    files, files_inspection = classify("files.CT", '<Files><File Encoding="Ascii85">payload</File></Files>')
    assert files.has_embedded_files is True
    assert files_inspection.embedded_files == 1


def test_a_label_as_long_as_the_file_allows_is_cut_in_one_pass(tmp_path: Path):
    """Every description of every imported table goes through this cut.

    It used to drop one character at a time and re-encode to ask how long the
    rest was now, which is quadratic in a length a stranger's file chooses: 2 MB
    measured at 66 seconds on this device, against a 32 MB file ceiling, on the
    ordinary import route and before any consent. The size is the point of the
    test, and the clock is the assertion, because the old code produced the same
    label and simply took minutes to do it.
    """
    import time
    from hashlib import sha256
    from ce_decky.ct_inspector import MAX_DESCRIPTION_BYTES, inspect_table

    label = "a" * (4 * 1024 * 1024)
    data = (
        '<CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        f'<CheatEntry><ID>1</ID><Description>"{label}"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>'
        '</CheatEntry></CheatEntries></CheatTable>'
    ).encode("utf-8")
    path = tmp_path / "long.CT"
    path.write_bytes(data)

    started = time.monotonic()
    inspection = inspect_table(path, sha256(data).hexdigest())
    assert time.monotonic() - started < 10.0
    assert len(inspection.controls[0].description.encode("utf-8")) <= MAX_DESCRIPTION_BYTES


def test_a_dropped_value_list_is_counted_as_a_list_and_not_as_one_value(tmp_path: Path):
    # A list past the byte ceiling is dropped whole so that one record's picker
    # cannot cost the table, and the list this was added for holds 6508 values.
    # Counted among the values it reported that a table which had lost a whole
    # picker had lost one value, on the screen where the user decides whether
    # the table is worth using.
    from hashlib import sha256
    from ce_decky.ct_inspector import MAX_DROPDOWN_BYTES, inspect_table

    huge = "\n".join(f"{index}:Item {index}" for index in range(200_000))
    assert len(huge.encode("utf-8")) > MAX_DROPDOWN_BYTES
    data = (
        '<CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"Item"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>'
        f'<DropDownList>{huge}</DropDownList>'
        '</CheatEntry></CheatEntries></CheatTable>'
    ).encode("utf-8")
    path = tmp_path / "picker.CT"
    path.write_bytes(data)

    inspection = inspect_table(path, sha256(data).hexdigest())
    assert inspection.dropped_value_lists == 1
    assert inspection.dropped_values == 0
    # The record survives; only its list is gone.
    assert len(inspection.controls) == 1
    assert inspection.controls[0].dropdown_values == ()


def _one_dropdown(entries: str, tmp_path, read_only: str = "") -> object:
    data = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"bEnableGodMode"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>'
        f'<DropDownList>{entries}</DropDownList>{read_only}'
        '</CheatEntry></CheatEntries></CheatTable>'
    ).encode("utf-8")
    return _inspect(data, tmp_path).controls[0]


def test_a_two_entry_list_that_says_off_and_on_is_a_switch(tmp_path):
    # The key for on is whatever the author wrote: the corpus carries `1040`/
    # `2400` and three tables whose active side is `0`, so writing `1` for on
    # would switch those records the wrong way.
    assert _one_dropdown("0:Disabled\n1:Enabled", tmp_path / "a").switch_on_value == "1"
    assert _one_dropdown("0:Yes\n1:No", tmp_path / "b").switch_on_value == "0"
    assert _one_dropdown("1:Disabled\n2:Enabled", tmp_path / "c").switch_on_value == "2"
    assert _one_dropdown("1040:Off\n2400:On", tmp_path / "d").switch_on_value == "2400"
    # Decoration is separators, not something to strip by name, and a pair
    # written in another language is the same pair.
    assert _one_dropdown("0:\U0001f512 Locked\n1:\U0001f513 Unlocked", tmp_path / "e").switch_on_value == "1"
    assert _one_dropdown("0:Нет\n1:Да", tmp_path / "f").switch_on_value == "1"
    assert _one_dropdown("0:否 No\n1:是 Yes", tmp_path / "g").switch_on_value == "1"


def test_two_named_alternatives_keep_their_list(tmp_path):
    # 73 of the corpus's 541 two-entry lists are a choice between two named
    # things, and the labels are the information. `No Gravity` is the dangerous
    # one: its negative word names the cheat rather than the off side.
    for entries in (
        "0:Male\n1:Female",
        "0:Set Own Ammo\n1:Unlimited Ammo",
        "0:CMYK\n1:RGB",
        "0:No Gravity\n1:Original Value",
        "0:Zero\n1:Max",
    ):
        control = _one_dropdown(entries, tmp_path / entries[2:6].strip())
        assert control.switch_on_value is None
        assert control.kind == "dropdown"


def test_a_pair_the_vocabulary_cannot_place_is_reported_once(tmp_path):
    inspection = _inspect(
        (
            '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
            '<CheatEntry><ID>1</ID><Description>"A"</Description><VariableType>4 Bytes</VariableType>'
            '<Address>game.exe+10</Address><DropDownList>0:Male\n1:Female</DropDownList></CheatEntry>'
            '<CheatEntry><ID>2</ID><Description>"B"</Description><VariableType>4 Bytes</VariableType>'
            '<Address>game.exe+14</Address><DropDownList>0:Male\n1:Female</DropDownList></CheatEntry>'
            '<CheatEntry><ID>3</ID><Description>"C"</Description><VariableType>4 Bytes</VariableType>'
            '<Address>game.exe+18</Address><DropDownList>0:Off\n1:On</DropDownList></CheatEntry>'
            '</CheatEntries></CheatTable>'
        ).encode("utf-8"),
        tmp_path,
    )
    assert inspection.unrecognised_pairs == (("Male", "Female"),)


def test_a_list_that_is_not_a_pair_is_never_a_switch(tmp_path):
    assert _one_dropdown("0:Off\n1:On\n2:Maybe", tmp_path / "three").switch_on_value is None
    assert _one_dropdown("0:Enabled", tmp_path / "one").switch_on_value is None
    read_only = _one_dropdown("", tmp_path / "empty", read_only="<DropDownReadOnly>1</DropDownReadOnly>")
    assert read_only.kind == "dropdown" and read_only.switch_on_value is None
    # One label carrying both sides is a list the author wrote out, not a side.
    assert _one_dropdown("0:On/Off\n1:Enabled", tmp_path / "both").switch_on_value is None
    # Two entries that write the same key cannot switch anything.
    assert _one_dropdown("1:Disabled\n1:Enabled", tmp_path / "same").switch_on_value is None


def test_a_record_carries_what_its_script_declares_for_its_own_symbol(tmp_path):
    """What the table does on its own, before anybody chooses anything.

    Read from the script that allocates the symbol, so a record named here is
    one that exists once that script has run. A declaration this cannot read
    leaves the record with none, which decides nothing.
    """
    data = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"Enable"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        '<AssemblerScript>[ENABLE]\n'
        'bEnableGodMode:\n  dd 1\n'
        'bEnableQuietMode:\n  dd 0\n'
        'fDamageMod:\n  dd (float)10.0\n</AssemblerScript>'
        '<CheatEntries>'
        '<CheatEntry><ID>2</ID><Description>"God"</Description><VariableType>4 Bytes</VariableType>'
        '<Address>bEnableGodMode</Address><DropDownList>0:Disabled\n1:Enabled</DropDownList></CheatEntry>'
        '<CheatEntry><ID>3</ID><Description>"Quiet"</Description><VariableType>4 Bytes</VariableType>'
        '<Address>bEnableQuietMode</Address><DropDownList>0:Disabled\n1:Enabled</DropDownList></CheatEntry>'
        '<CheatEntry><ID>4</ID><Description>"Damage"</Description><VariableType>Float</VariableType>'
        '<Address>fDamageMod</Address></CheatEntry>'
        '<CheatEntry><ID>5</ID><Description>"Elsewhere"</Description><VariableType>4 Bytes</VariableType>'
        '<Address>game.exe+10</Address><DropDownList>0:Disabled\n1:Enabled</DropDownList></CheatEntry>'
        '</CheatEntries></CheatEntry></CheatEntries></CheatTable>'
    ).encode("utf-8")
    by_id = {control.id: control for control in _inspect(data, tmp_path).controls}
    assert by_id[2].declared_default == "1", "the flag this script switches on by itself"
    assert by_id[3].declared_default == "0", "and the one it leaves off"
    # Taken exactly as written, so a cast never compares equal to a switch key.
    assert by_id[4].declared_default == "(float)10.0"
    # An address this script does not allocate carries no declaration at all.
    assert by_id[5].declared_default is None
    assert by_id[1].declared_default is None


def test_a_signed_table_is_read_as_signed_and_refuses_nothing(tmp_path):
    """Cheat Engine refuses a signed table silently; this says so out loud.

    Read as a structural fact about the bytes: the element, and what it carried,
    which is what a later report needs because which of Cheat Engine's three
    refusals runs is not established and one of them is a Windows call under
    Wine rather than anything about the table.
    """
    signed = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45">'
        '<CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry></CheatEntries>'
        '<Signature><SignedHash>' + "h" * 165 + '</SignedHash><PublicKey>' + "k" * 3774 + '</PublicKey></Signature>'
        '</CheatTable>'
    ).encode("utf-8")
    inspection = _inspect(signed, tmp_path / "signed")
    assert inspection.has_signature is True
    assert inspection.has_signed_hash is True and inspection.has_public_key is True
    assert inspection.public_key_bytes == 3774
    # It is a statement, not a refusal: the table still inspects completely.
    assert inspection.total_entries == 1 and len(inspection.controls) == 1

    unsigned = signed.replace(b"<Signature>", b"<NotASignature>").replace(b"</Signature>", b"</NotASignature>")
    plain = _inspect(unsigned, tmp_path / "unsigned")
    assert plain.has_signature is False
    assert plain.has_signed_hash is False and plain.has_public_key is False and plain.public_key_bytes == 0


def test_an_empty_signature_element_carries_no_parts(tmp_path):
    empty = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries/>'
        '<Signature><SignedHash>  </SignedHash></Signature></CheatTable>'
    ).encode("utf-8")
    inspection = _inspect(empty, tmp_path)
    # The element is there, so the table is signed as far as Cheat Engine's own
    # loader is concerned; what it holds is reported separately and honestly.
    assert inspection.has_signature is True
    assert inspection.has_signed_hash is False and inspection.has_public_key is False


SIGNED_TABLE = (
    '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
    '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description>'
    '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry></CheatEntries>\n'
    '  <UserdefinedSymbols><SymbolEntry><Name>base</Name><Address>game.exe+8</Address></SymbolEntry></UserdefinedSymbols>\n'
    '  <LuaScript>print("hello")</LuaScript>\n'
    '  <Signature><SignedHash>' + "h" * 165 + '</SignedHash><PublicKey>' + "k" * 128 + '</PublicKey></Signature>\n'
    '</CheatTable>\n'
).encode("utf-8")


def test_removing_a_signature_leaves_the_rest_of_the_file_exactly_as_it_was(tmp_path):
    """CE Decky produces executable content here, so the result is proven.

    Proven against the source rather than trusted from the edit that made it:
    every subtree Cheat Engine executes or resolves compares equal, the root
    keeps every other child, and the only element gone is the signature.
    """
    from hashlib import sha256

    from ce_decky.ct_inspector import assert_only_signature_removed, strip_signature

    derived = strip_signature(SIGNED_TABLE)
    assert b"<Signature>" not in derived
    assert b"<LuaScript>print(\"hello\")</LuaScript>" in derived
    assert len(derived) < len(SIGNED_TABLE)
    assert_only_signature_removed(SIGNED_TABLE, derived)
    # The same table, a different identity: it is not the bytes anybody served.
    assert sha256(derived).hexdigest() != sha256(SIGNED_TABLE).hexdigest()
    inspection = _inspect(derived, tmp_path / "derived")
    assert inspection.has_signature is False and inspection.total_entries == 1


def test_a_table_with_no_signature_derives_nothing(tmp_path):
    from ce_decky.ct_inspector import TableTransformError, strip_signature

    plain = SIGNED_TABLE.replace(b"<Signature>", b"<NotASignature>").replace(b"</Signature>", b"</NotASignature>")
    with pytest.raises(TableTransformError, match="no signature"):
        strip_signature(plain)


def test_a_tampered_derivation_is_refused_rather_than_produced(tmp_path):
    """Every way the result could differ, and each one refused by name."""
    from ce_decky.ct_inspector import TableTransformError, assert_only_signature_removed, strip_signature

    honest = strip_signature(SIGNED_TABLE)
    for broken, reason in (
        (honest.replace(b'print("hello")', b'print("goodbye")'), "changed LuaScript"),
        (honest.replace(b"game.exe+10", b"game.exe+20"), "changed CheatEntries"),
        (honest.replace(b"<Address>game.exe+8</Address>", b"<Address>game.exe+9</Address>"), "changed UserdefinedSymbols"),
        (honest.replace(b'CheatEngineTableVersion="45"', b'CheatEngineTableVersion="46"'), "own attributes"),
        (honest.replace(b"</CheatTable>", b"<Forms/></CheatTable>"), "other than the signature"),
        (SIGNED_TABLE, "still carries a signature"),
    ):
        with pytest.raises(TableTransformError, match=reason):
            assert_only_signature_removed(SIGNED_TABLE, broken)


CT_WITH_A_HOOK_THAT_MUST_STAY_ON = (
    '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
    '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Script"</Description>'
    '<VariableType>Auto Assembler Script</VariableType>'
    '<AssemblerScript>[ENABLE]\n'
    'label(bEnablePlain)\n'
    'label(bEnableVitals)\n'
    'lblPlain:\n'
    'cmp dword ptr [bEnablePlain],1\n'
    'jne short lblPlainSkip\n'
    'mulss xmm0,[fPlainMod]\n'
    'lblPlainSkip:\n'
    'readmem(aobPlain,7)\n'
    'jmp lblPlainRet\n'
    'lblVitals:\n'
    'sub rcx,rsi\n'
    'cmp dword ptr [bEnableVitals],1\n'
    'jne lblVitalsSkip\n'
    'add rcx,rsi\n'
    'lblVitalsSkip:\n'
    'readmem(aobVitals,3)\n'
    'jmp lblVitalsRet\n'
    'bEnablePlain:\n'
    'dd 1\n'
    'bEnableVitals:\n'
    'dd 1\n'
    '[DISABLE]\n'
    '</AssemblerScript>'
    '<CheatEntries>'
    '<CheatEntry><ID>2</ID><Description>"Plain"</Description><VariableType>4 Bytes</VariableType>'
    '<Address>bEnablePlain</Address><DropDownList>0:Off\n1:On</DropDownList></CheatEntry>'
    '<CheatEntry><ID>3</ID><Description>"Vitals"</Description><VariableType>4 Bytes</VariableType>'
    '<Address>bEnableVitals</Address><DropDownList>0:Off\n1:On</DropDownList></CheatEntry>'
    '</CheatEntries></CheatEntry></CheatEntries>\n</CheatTable>\n'
).encode("utf-8")


def test_a_record_says_whether_its_own_code_survives_being_switched_off(tmp_path: Path):
    """Switching a cheat off is writing the flag its script gates the patch on.

    One real table's hook turns a pointer into an offset, tests the flag, and on
    the branch taken when the flag is off hands the game back the offset: the
    game reads it as an address and dies. The record carries that answer, so
    nothing writes such a flag on the user's behalf.
    """
    from hashlib import sha256
    from ce_decky.ct_inspector import inspect_table
    path = tmp_path / "hooks.CT"
    path.write_bytes(CT_WITH_A_HOOK_THAT_MUST_STAY_ON)
    found = inspect_table(path, sha256(CT_WITH_A_HOOK_THAT_MUST_STAY_ON).hexdigest())
    safety = {control.description: control.switch_off_is_safe for control in found.controls if control.id in (2, 3)}

    assert safety == {"Plain": True, "Vitals": False}


def test_a_nested_script_answers_for_its_own_symbols(tmp_path: Path):
    """The nearest script wins, exactly as it does for declarations.

    A symbol a script allocates is read by that script's hooks, so an answer
    another script gave for the same name is not about this one - and taking the
    outer answer would let a parent's `safe` stand for a child's unsafe hook.
    """
    from hashlib import sha256
    from ce_decky.ct_inspector import inspect_table
    outer_safe = (
        '[ENABLE]\n'
        'label(bEnableShared)\n'
        'lblOuter:\n'
        'cmp dword ptr [bEnableShared],1\n'
        'jne short lblOuterSkip\n'
        'mulss xmm0,[fOuterMod]\n'
        'lblOuterSkip:\n'
        'readmem(aobOuter,7)\n'
        'jmp lblOuterRet\n'
        'bEnableShared:\n'
        'dd 1\n'
        '[DISABLE]\n'
    )
    inner_unsafe = (
        '[ENABLE]\n'
        'label(bEnableShared)\n'
        'lblInner:\n'
        'sub rcx,rsi\n'
        'cmp dword ptr [bEnableShared],1\n'
        'jne lblInnerSkip\n'
        'add rcx,rsi\n'
        'lblInnerSkip:\n'
        'readmem(aobInner,3)\n'
        'jmp lblInnerRet\n'
        'bEnableShared:\n'
        'dd 1\n'
        '[DISABLE]\n'
    )
    data = (
        '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
        '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Outer"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        f'<AssemblerScript>{outer_safe}</AssemblerScript>'
        '<CheatEntries><CheatEntry><ID>2</ID><Description>"Inner"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        f'<AssemblerScript>{inner_unsafe}</AssemblerScript>'
        '<CheatEntries><CheatEntry><ID>3</ID><Description>"Shared"</Description>'
        '<VariableType>4 Bytes</VariableType><Address>bEnableShared</Address>'
        '<DropDownList>0:Off\n1:On</DropDownList></CheatEntry>'
        '</CheatEntries></CheatEntry></CheatEntries></CheatEntry></CheatEntries>\n</CheatTable>\n'
    ).encode("utf-8")
    path = tmp_path / "nested.CT"
    path.write_bytes(data)

    found = inspect_table(path, sha256(data).hexdigest())
    shared = next(control for control in found.controls if control.id == 3)
    assert shared.switch_off_is_safe is False
