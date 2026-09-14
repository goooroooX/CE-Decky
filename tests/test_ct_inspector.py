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
