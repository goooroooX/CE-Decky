#!/usr/bin/env python3
"""Inspect a table's literal address dependencies or derive a reviewed copy.

Offline only. No Cheat Engine, provider, process memory, import, consent or profile
mutation. Derivation requires the exact source SHA and expected substitution
counts, preserves everything outside Address/AssemblerScript text, refuses signed
tables, and publishes a new file exclusively after production static inspection.
The diff proves the edit's extent, never that the new addresses are correct.
See docs/TABLE_REPAIR.md.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import difflib
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from xml.parsers import expat

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
from ce_decky.atomic import read_regular_bytes  # noqa: E402
from ce_decky.ct_inspector import inspect_table, read_table_root  # noqa: E402
from ce_decky.table_store import MAX_CT_BYTES  # noqa: E402
from ce_decky.table_markers import local_tag  # noqa: E402

MAX_RECIPE_BYTES = 256 * 1024


def source_table(path, expected):
    root, blob = read_table_root(path, expected)
    inspection = inspect_table(path, expected)
    return root, blob, inspection


def inventory(path, expected):
    root, blob, inspection = source_table(path, expected)
    records = []
    for control in inspection.controls:
        records.append({"id": control.id, "path": list(control.path), "kind": control.kind,
                        "owner_script_id": control.owner_script_id, "script_owned": control.script_owned})
    literals = []
    for entry in (node for node in root.iter() if local_tag(node.tag) == "CheatEntry"):
        for field in ("AssemblerScript", "Address"):
            value = next((n.text for n in entry if local_tag(n.tag) == field), None)
            entry_id = next((n.text for n in entry if local_tag(n.tag) == "ID"), None)
            for line, text in enumerate((value or "").splitlines(), 1):
                for match in re.finditer(r"(?i)\b0x[0-9a-f]{6,16}\b", text):
                    literals.append({"record_id": entry_id, "field": field,
                                     "line": line, "literal": match.group()})
                    if len(literals) >= 4096:
                        break
                if len(literals) >= 4096:
                    break
            if len(literals) >= 4096:
                break
        if len(literals) >= 4096:
            break
    return {"schema": 1, "table_sha256": expected, "records": records,
            "scans": [scan.as_dict() for scan in inspection.scans], "literal_candidates": literals,
            "literal_list_complete": len(literals) < 4096,
            "limits": "Literals include constants and comments; dynamic Lua addresses and non-0x expressions need code review. No literal is declared to be an address.",
            "ambiguous_record_ids": list(inspection.ambiguous_record_ids),
            "unsupported_record_id_count": inspection.unsupported_record_id_count}


def extract_records(path, expected, record_ids, output):
    """Export decoded scripts for bounded review, without loading any code."""
    root, _, inspection = source_table(path, expected)
    if not record_ids or len(record_ids) > 64 or len(set(record_ids)) != len(record_ids):
        raise ValueError("extract needs 1 to 64 distinct record IDs")
    if inspection.ambiguous_record_ids or inspection.unsupported_record_id_count:
        raise ValueError("extract requires unique supported record IDs")
    wanted, scripts = set(record_ids), []
    for entry in (node for node in root.iter() if local_tag(node.tag) == "CheatEntry"):
        fields = {local_tag(node.tag): node.text for node in entry}
        try:
            record_id = int(fields.get("ID") or "")
        except ValueError:
            continue
        if record_id in wanted:
            code = fields.get("AssemblerScript")
            if not code:
                raise ValueError(f"record {record_id} has no assembler script")
            scripts.append((record_id, code))
    if {record_id for record_id, _ in scripts} != wanted:
        raise ValueError("a requested script record is missing")
    output = Path(output)
    output.mkdir(mode=0o700)
    exported = []
    for record_id, code in scripts:
        target = output / f"record-{record_id}.txt"
        data = code.encode("utf-8")
        with target.open("xb") as stream:
            stream.write(data)
        exported.append({"record_id": record_id, "path": str(target), "sha256": sha256(data).hexdigest()})
    return {"schema": 1, "source_sha256": expected, "scripts": exported,
            "limits": "Decoded script text only; nothing was executed or repaired."}


def protected_tree(root):
    clone = deepcopy(root)
    for entry in (node for node in clone.iter() if local_tag(node.tag) == "CheatEntry"):
        for field in entry:
            if local_tag(field.tag) in ("Address", "AssemblerScript"):
                field.text = None
    return ET.tostring(clone, encoding="utf-8")


def editable_spans(blob):
    """Exact byte intervals inside direct record script/address fields.

    Expat supplies source offsets, so a tag-looking string in a comment or Lua
    CDATA cannot create a fake editable field. Start-tag scanning respects
    quoted attributes; empty fields grant no editable bytes.
    """
    parser = expat.ParserCreate(namespace_separator="}")
    stack, spans = [], []
    def start(name, attributes):
        begin = None
        if stack and local_tag(stack[-1][0]) == "CheatEntry" and local_tag(name) in ("Address", "AssemblerScript"):
            pos, quote = parser.CurrentByteIndex, None
            while pos < len(blob):
                char = blob[pos]
                if quote:
                    if char == quote:
                        quote = None
                elif char in (34, 39):
                    quote = char
                elif char == 62:
                    if blob[pos - 1] != 47:
                        begin = pos + 1
                    break
                pos += 1
        stack.append((name, begin))
    def end(name):
        _, begin = stack.pop()
        if begin is not None and parser.CurrentByteIndex > begin:
            spans.append((begin, parser.CurrentByteIndex))
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(blob, True)
    return sorted(spans)


def derive(path, recipe, output):
    if not isinstance(recipe, dict) or type(recipe.get("schema")) is not int or recipe["schema"] != 1:
        raise ValueError("derivation recipe schema must be 1")
    expected = recipe.get("source_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("derivation needs an exact source SHA-256")
    root, blob, inspection = source_table(path, expected)
    if inspection.has_signature or inspection.ambiguous_record_ids or inspection.unsupported_record_id_count:
        raise ValueError("derive only an unsigned table with unique supported record IDs")
    substitutions = recipe.get("substitutions")
    if not isinstance(substitutions, list) or not 1 <= len(substitutions) <= 64:
        raise ValueError("derivation needs 1 to 64 reviewed substitutions")
    original = blob.decode("utf-8")
    changed, edits = original, []
    for item in substitutions:
        if not isinstance(item, dict) or set(item) != {"old", "new", "count"}:
            raise ValueError("each substitution needs old, new and count")
        old, new, count = item["old"], item["new"], item["count"]
        if (not isinstance(old, str) or not isinstance(new, str) or not old or old == new
                or max(len(old), len(new)) > 4096 or type(count) is not int or not 1 <= count <= 4096):
            raise ValueError("invalid or unbounded substitution")
        if changed.count(old) != count:
            raise ValueError("substitution occurrence count differs from the reviewed recipe")
        encoded, token = changed.encode("utf-8"), old.encode("utf-8")
        permitted = sum(encoded[a:b].count(token) for a, b in editable_spans(encoded))
        if permitted != count:
            raise ValueError("substitution touches bytes outside script/address content")
        changed = changed.replace(old, new)
        if len(changed.encode("utf-8")) > MAX_CT_BYTES:
            raise ValueError("derived table exceeds the production size limit")
        edits.append(item)
    data = changed.encode("utf-8")
    new_sha = sha256(data).hexdigest()
    output = Path(output)
    if path.resolve() == output.resolve() or output.exists():
        raise ValueError("output must be a new path, separate from the source")
    # A same-directory temporary file is inspected before an exclusive hard-link
    # publication. A race cannot replace an existing user's file.
    fd, temporary = tempfile.mkstemp(prefix=".table-repair-", suffix=".CT", dir=output.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        derived_root, verified, _ = source_table(Path(temporary), new_sha)
        if protected_tree(root) != protected_tree(derived_root):
            raise ValueError("substitutions changed content outside script/address text")
        if verified != data or data == blob:
            raise ValueError("derived bytes were changed or no edit was made")
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    diff = "".join(difflib.unified_diff(original.splitlines(True), changed.splitlines(True),
                                      fromfile="source.CT", tofile="derived.CT"))
    return {"schema": 1, "source_sha256": expected, "derived_sha256": new_sha,
            "output": str(output), "substitutions": edits, "diff": diff[:65536],
            "diff_complete": len(diff) <= 65536,
            "limits": "Reviewed textual derivation only. Address correctness, execution consent and gameplay effect are not established."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("inspect")
    read.add_argument("table", type=Path)
    read.add_argument("--sha256", required=True)
    export = commands.add_parser("extract")
    export.add_argument("table", type=Path)
    export.add_argument("--sha256", required=True)
    export.add_argument("--record", type=int, nargs="+", required=True)
    export.add_argument("--output-dir", type=Path, required=True)
    edit = commands.add_parser("derive")
    edit.add_argument("table", type=Path)
    edit.add_argument("--recipe", type=Path, required=True)
    edit.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inventory(args.table, args.sha256)
        elif args.command == "extract":
            result = extract_records(args.table, args.sha256, args.record, args.output_dir)
        else:
            raw = read_regular_bytes(args.recipe, max_bytes=MAX_RECIPE_BYTES)
            result = derive(args.table, json.loads(raw), args.output)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError) as exc:
        print(f"table repair: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
