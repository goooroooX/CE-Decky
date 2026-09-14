from pathlib import Path
import hashlib

import pytest

from ce_decky.ct_inspector import inspect_table
from ce_decky.table_store import TableStore

CT = b'''<CheatTable CheatEngineTableVersion="45">
<CheatEntries>
 <CheatEntry><ID>1</ID><Description>"Enable"</Description><GroupHeader>1</GroupHeader><CheatEntries>
   <CheatEntry><ID>2</ID><Description>"Infinite Health"</Description><VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]\nnop\n[DISABLE]</AssemblerScript></CheatEntry>
 </CheatEntries></CheatEntry>
 <CheatEntry><ID>3</ID><Description>"Mode"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+123</Address><DropDownReadOnly>1</DropDownReadOnly><DropDownList>0:Off\n1:On\n2:Turbo</DropDownList></CheatEntry>
</CheatEntries>
<LuaScript>return true</LuaScript>
</CheatTable>'''


def test_inspector_builds_controls_paths_and_capabilities(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "store")
    artifact = store.import_ct(str(source))
    inspection = inspect_table(store.verified_blob(artifact.sha256), artifact.sha256)
    assert inspection.total_entries == 3
    assert inspection.has_lua is True
    assert inspection.has_auto_assembler is True
    assert inspection.process_candidates == ("game.exe",)
    script = inspection.controls[1]
    assert script.path == ("Enable", "Infinite Health")
    assert script.kind == "script"
    mode = inspection.controls[2]
    assert mode.kind == "dropdown"
    assert mode.dropdown_read_only is True
    assert mode.dropdown_values == (("0", "Off"), ("1", "On"), ("2", "Turbo"))


def test_inspector_runs_only_on_verified_exact_blob(tmp_path: Path):
    source = tmp_path / "game.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "store")
    artifact = store.import_ct(str(source))
    blob = Path(artifact.blob_path)
    original = blob.read_bytes()
    blob.write_bytes((b"X" if original[:1] != b"X" else b"Y") + original[1:])
    try:
        store.verified_blob(artifact.sha256)
    except ValueError as exc:
        assert "SHA-256" in str(exc)
    else:
        raise AssertionError("tampered table passed execution-grade verification")


def test_inspector_rejects_pathological_cheatentry_nesting(tmp_path: Path):
    depth = 130
    body = ''
    for index in reversed(range(depth)):
        body = f'<CheatEntry><ID>{index}</ID><Description>"N{index}"</Description><CheatEntries>{body}</CheatEntries></CheatEntry>'
    source = tmp_path / "deep.CT"
    source.write_text(f'<CheatTable><CheatEntries>{body}</CheatEntries></CheatTable>', encoding='utf-8')
    store = TableStore(tmp_path / "store")
    artifact = store.import_ct(str(source))
    with __import__('pytest').raises(ValueError, match='nesting depth'):
        inspect_table(store.verified_blob(artifact.sha256), artifact.sha256)


def test_inspector_bounds_oversized_display_fields_and_dropdown_without_losing_the_table(tmp_path: Path):
    # A ceiling exists so the panel has something bounded to render. Spending
    # the whole table on one over-long label or one over-long dropdown was the
    # disproportionate half: measured across 82 tables served by FearLess, four
    # were refused outright for exactly this class of thing while being
    # perfectly good tables otherwise.
    from ce_decky.ct_inspector import MAX_DESCRIPTION_BYTES, MAX_DROPDOWN_VALUES
    long_desc = "x" * (MAX_DESCRIPTION_BYTES + 1)
    path = tmp_path / "long.CT"
    path.write_text(
        f'<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>"{long_desc}"</Description></CheatEntry></CheatEntries></CheatTable>',
        encoding="utf-8",
    )
    inspection = inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(inspection.controls) == 1
    assert len(inspection.controls[0].description.encode("utf-8")) <= MAX_DESCRIPTION_BYTES

    dropdown = "\n".join(f"{i}:v" for i in range(MAX_DROPDOWN_VALUES + 1))
    path.write_text(
        f'<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>"Mode"</Description><DropDownList>{dropdown}</DropDownList></CheatEntry></CheatEntries></CheatTable>',
        encoding="utf-8",
    )
    inspection = inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(inspection.controls[0].dropdown_values) == MAX_DROPDOWN_VALUES
    # And the table says it was not taken exactly as written.
    assert inspection.dropped_values >= 1


def test_inspector_preflight_rejects_excessive_entry_depth(tmp_path: Path):
    from ce_decky.ct_inspector import MAX_INSPECTION_DEPTH
    nested = ""
    for i in range(MAX_INSPECTION_DEPTH + 1):
        nested += f'<CheatEntry><ID>{i}</ID><Description>"{i}"</Description><CheatEntries>'
    nested += "<CheatEntry><ID>999</ID><Description>\"leaf\"</Description></CheatEntry>"
    nested += "</CheatEntries></CheatEntry>" * (MAX_INSPECTION_DEPTH + 1)
    path = tmp_path / "deep.CT"
    path.write_text(f"<CheatTable><CheatEntries>{nested}</CheatEntries></CheatTable>", encoding="utf-8")
    with pytest.raises(ValueError, match="nesting depth"):
        inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())


def test_inspector_rejects_wrong_sha_and_reports_ambiguous_or_unsupported_ids(tmp_path: Path):
    path = tmp_path / "ids.CT"
    path.write_text(
        '<CheatTable><CheatEntries>'
        '<CheatEntry><ID>7</ID><Description>"A"</Description></CheatEntry>'
        '<CheatEntry><ID>7</ID><Description>"B"</Description></CheatEntry>'
        '<CheatEntry><ID>2147483648</ID><Description>"TooBig"</Description></CheatEntry>'
        '</CheatEntries></CheatTable>', encoding='utf-8'
    )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="exact SHA-256"):
        inspect_table(path, "0" * 64)
    inspection = inspect_table(path, digest)
    assert inspection.ambiguous_record_ids == (7,)
    assert inspection.unsupported_record_id_count == 1
    assert inspection.controls[-1].id is None


def test_inspector_normalizes_display_text_but_rejects_invisible_spoofing(tmp_path: Path):
    path = tmp_path / "display.CT"
    path.write_text(
        '<CheatTable CheatEngineTableVersion="45\u0301"><CheatEntries>'
        '<CheatEntry><ID>1</ID><Description>"Line\nName"</Description>'
        '<VariableType>4 Bytes</VariableType><DropDownList>0:Off\n1:On</DropDownList></CheatEntry>'
        '</CheatEntries></CheatTable>', encoding="utf-8"
    )
    inspection = inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert inspection.controls[0].description == "Line Name"
    assert "\n" not in inspection.controls[0].description

    # The spoofing character must not survive into anything rendered. Removing
    # it is what does that; refusing the table did it too, and also cost the
    # user every cheat in the table for a decoration in one label.
    path.write_text(
        '<CheatTable><CheatEntries><CheatEntry><ID>1</ID>'
        '<Description>safe\u202eexe</Description></CheatEntry></CheatEntries></CheatTable>', encoding="utf-8"
    )
    inspection = inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert inspection.controls[0].description == "safeexe"
    assert "\u202e" not in inspection.controls[0].description
    # Counted rather than swallowed: a label that carried one is worth saying.
    assert inspection.sanitized_labels >= 1


def test_a_spoofed_process_hint_is_dropped_rather_than_cleaned_up(tmp_path: Path):
    # A label is shown; a process hint is offered as the executable to attach
    # to. Removing the character from that one would present the name it was
    # made to imitate, so the candidate goes instead.
    path = tmp_path / "hint.CT"
    path.write_text(
        '<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>A</Description>'
        '<Address>ga\u200bme.exe+10</Address></CheatEntry></CheatEntries></CheatTable>', encoding="utf-8"
    )
    inspection = inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert inspection.process_candidates == ()


def test_a_dropdown_value_the_runtime_cannot_send_costs_that_value(tmp_path: Path):
    # The value is table semantics and is never trimmed, so a line this cannot
    # carry is dropped. One of the 82 sampled FearLess tables was refused
    # outright for a single such line.
    from ce_decky.ct_inspector import MAX_DROPDOWN_VALUE_BYTES
    path = tmp_path / "dropdown-value.CT"
    huge = "x" * (MAX_DROPDOWN_VALUE_BYTES + 1)
    path.write_text(
        f'<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>Mode</Description>'
        f'<DropDownList>0:Fine\n{huge}:Huge</DropDownList></CheatEntry></CheatEntries></CheatTable>', encoding="utf-8"
    )
    inspection = inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert [value for value, _ in inspection.controls[0].dropdown_values] == ["0"]
    assert inspection.dropped_values >= 1


def test_inspector_bounds_address_process_hint_scan_and_candidate_count(monkeypatch, tmp_path: Path):
    import ce_decky.ct_inspector as inspector

    path = tmp_path / "address.CT"
    huge_address = "x" * (inspector.MAX_ADDRESS_HINT_BYTES + 1)
    path.write_text(
        f'<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>A</Description><Address>{huge_address}</Address></CheatEntry></CheatEntries></CheatTable>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="process-hint scan limit"):
        inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())

    monkeypatch.setattr(inspector, "MAX_PROCESS_CANDIDATES", 2)
    path.write_text(
        '<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>A</Description>'
        '<Address>a.exe+1 b.exe+2 c.exe+3</Address></CheatEntry></CheatEntries></CheatTable>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="too many process hints"):
        inspect_table(path, hashlib.sha256(path.read_bytes()).hexdigest())


ATTACH_CT = b"""<CheatTable CheatEngineTableVersion="45">
<CheatEntries>
 <CheatEntry><ID>1</ID><Description>"game attach (f2)"</Description><VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]
{$lua}
OpenProcess("game.exe")
{$asm}
[DISABLE]
</AssemblerScript></CheatEntry>
 <CheatEntry><ID>2</ID><Description>"health"</Description><VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]
{$lua}
print("arming")
{$asm}
aobscanmodule(hook,game.exe,89 3E 5F)
alloc(newmem,1024)
hook:
  jmp newmem
[DISABLE]
hook:
  db 89 3E 5F
dealloc(newmem)
</AssemblerScript></CheatEntry>
 <CheatEntry><ID>3</ID><Description>"opener with cleanup"</Description><VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]
{$lua}
OpenProcess("game.exe")
[DISABLE]
{$lua}
closeCE()
</AssemblerScript></CheatEntry>
</CheatEntries>
</CheatTable>"""


def test_a_record_that_only_attaches_is_not_a_cheat(tmp_path: Path):
    """The table author's own attach button changes nothing in the game.

    CE Decky attaches by exact PID before any of this runs, so the record is
    machinery: it belongs with the scripts rather than among the cheats, and
    switching it on would re-open the process by name. Recognised by what the
    script does, never by what it is called: a body with no assembly in it
    cannot change the game.
    """
    source = tmp_path / "game.CT"
    source.write_bytes(ATTACH_CT)
    store = TableStore(tmp_path / "store")
    artifact = store.import_ct(str(source))
    inspection = inspect_table(store.verified_blob(artifact.sha256), artifact.sha256)

    flags = {control.description: control.attach_only for control in inspection.controls}
    assert flags["game attach (f2)"] is True
    # A cheat whose script opens with Lua and then patches the game is a cheat.
    assert flags["health"] is False
    # Something to undo means it did something; only an empty DISABLE qualifies.
    assert flags["opener with cleanup"] is False


def test_only_a_line_that_is_nothing_but_an_attach_call_counts_as_machinery(tmp_path: Path):
    """A record hidden by default has to be provably machinery, not probably.

    Asking whether a line contains an attach call anywhere read a line that
    opens the process and then does something else as if the something else
    were not there, and a `process(...)` of the author's own writing is not
    Cheat Engine's anything. Either would take a real cheat out of the default
    picker. Anything that cannot be read as the canonical body stays visible.
    """
    from ce_decky.ct_inspector import _is_attach_only

    canonical = "[ENABLE]\n{$lua}\n%s\n[DISABLE]\n"
    assert _is_attach_only(canonical % 'OpenProcess("game.exe")') is True
    assert _is_attach_only(canonical % 'openProcess(getProcessIDFromProcessName("game.exe"))') is True
    assert _is_attach_only(
        canonical % 'local pid = getProcessIDFromProcessName("game.exe")\nopenProcess(pid)'
    ) is True

    # A second statement sharing the line is a second thing the script does.
    assert _is_attach_only(canonical % 'openProcess("game.exe") startTrainer()') is False
    # A function of the table author's own, which CE Decky cannot identify.
    assert _is_attach_only(canonical % 'process("anything")') is False
    # The attach call as an argument to something else is that something else.
    assert _is_attach_only(canonical % 'luacall(openProcess("game.exe"))') is False
