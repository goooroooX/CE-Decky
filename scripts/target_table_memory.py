#!/usr/bin/env python3
"""Read a running game's memory for an agent-led table repair, never write it.

Supply an exact AppID, selected table SHA and independently established executable
path. With no recipe, report the exact process/file identities and loaded PE
sections. A bounded JSON recipe adds typed reads, pointer walks, module-pattern
scans, pointer-chain candidate discovery and the FName layout explicitly named
by the recipe. Results describe observations, never gameplay success or consent.
See docs/TABLE_REPAIR.md for the recipe format and evidence limits.
"""
from __future__ import annotations

import argparse
import bisect
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time

if __package__:
    from . import host_platform, target_plugin_rpc
else:
    import host_platform
    import target_plugin_rpc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "py_modules"))
from ce_decky.ce_launch import confirm_target_identity, resolve_target_identity  # noqa: E402
from ce_decky.ct_scans import compile_pattern  # noqa: E402

MAX_ADDRESS = (1 << 47) - 1
MAX_READ_BYTES = 512 * 1024 * 1024
MAX_SCAN_BYTES = 256 * 1024 * 1024
MAX_SECONDS = 30
MAX_OPERATIONS = 128
MAX_MAP_BYTES = 16 * 1024 * 1024
FORMATS = {"u8": "<B", "u16": "<H", "u32": "<I", "i32": "<i", "u64": "<Q", "f32": "<f", "f64": "<d"}
OP_FIELDS = {
    "address": {"base", "offset"},
    "read": {"base", "offset", "type", "size"},
    "walk": {"base", "offsets"},
    "scan": {"pattern", "sections", "section_rvas", "limit"},
    "find_chain": {"offsets", "checks", "section_rvas", "limit"},
    "ue_fname_16_16": {"object", "pool"},
    "rip_relative": {"base", "displacement_offset", "instruction_size"},
    "xor": {"left", "right"},
    "array": {"base", "count", "start", "limit", "stride", "type"},
    "ue_objects_16_16": {"array", "pool", "depth", "limit"},
}


class Refused(ValueError):
    """Identity, recipe or evidence does not justify this operation."""


class Unavailable(Refused):
    """An observation could not be completed; never a negative compatibility result."""


class Unresolved(Unavailable):
    """A speculative chain points outside readable memory; an ordinary non-match."""


def number(value, maximum=MAX_ADDRESS):
    try:
        n = int(value, 0) if isinstance(value, str) else value
    except ValueError as exc:
        raise Refused("expected an integer or a hexadecimal integer string") from exc
    if type(n) is not int or not 0 <= n <= maximum:
        raise Refused("integer outside the permitted range")
    return n


def digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise Refused("an exact lowercase SHA-256 is required")
    return value


def bounded_json(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise Refused("recipe exceeds 256 KiB")
    return json.loads(raw)


@dataclass(frozen=True)
class Mapping:
    start: int
    end: int
    flags: str
    offset: int
    device: str
    inode: int
    path: str


def parse_maps(raw):
    if len(raw) > MAX_MAP_BYTES:
        raise Refused("process maps exceed the bound")
    rows = []
    for line in raw.splitlines():
        parts = line.split(maxsplit=5)
        if len(parts) < 5:
            raise Refused("malformed process map")
        a, b = (int(n, 16) for n in parts[0].split("-"))
        if a >= b or (rows and a < rows[-1].end):
            raise Refused("unordered or overlapping process mappings")
        rows.append(Mapping(a, b, parts[1], int(parts[2], 16), parts[3], int(parts[4]), parts[5] if len(parts) == 6 else ""))
    return rows


class Memory:
    """Read-only process descriptor, mapped-range checks and one whole-run budget."""
    def __init__(self, fd, maps, *, pread=None, clock=time.monotonic):
        # POSIX calls are resolved after the host guard, so --help can run on
        # a host without pread and the diagnostic remains the platform refusal.
        self.fd, self.maps, self.pread, self.clock = fd, maps, pread or os.pread, clock
        self.starts = [m.start for m in maps]
        self.deadline = clock() + MAX_SECONDS
        self.bytes_read = 0
        self.cache = OrderedDict()

    def mapped(self, address, size=1):
        if type(address) is not int or not 0 < address <= MAX_ADDRESS or size < 1:
            return False
        index = bisect.bisect_right(self.starts, address) - 1
        return index >= 0 and "r" in self.maps[index].flags and address + size <= self.maps[index].end

    def read(self, address, size, *, cached=False):
        if not 1 <= size <= 2 * 1024 * 1024 or not self.mapped(address, size):
            raise Unresolved("address is not inside one readable mapping")
        if self.clock() > self.deadline or self.bytes_read + size > MAX_READ_BYTES:
            raise Unavailable("whole-probe time or byte budget exhausted")
        key = (address, size)
        if cached and key in self.cache:
            return self.cache[key]
        try:
            data = self.pread(self.fd, size, address)
        except OSError as exc:
            raise Unavailable("process memory is unreadable") from exc
        self.bytes_read += size
        if len(data) != size:
            raise Unavailable("short process-memory read")
        if cached:
            if len(self.cache) >= 65536:
                self.cache.popitem(last=False)
            self.cache[key] = data
        return data

    def scalar(self, address, kind="u64", *, cached=False):
        if kind not in FORMATS:
            raise Refused("unsupported scalar type")
        fmt = FORMATS[kind]
        value = struct.unpack(fmt, self.read(address, struct.calcsize(fmt), cached=cached))[0]
        if isinstance(value, float) and not math.isfinite(value):
            raise Unavailable("non-finite floating-point value")
        return value

    def walk(self, start, offsets, *, cached=False):
        if not isinstance(offsets, list) or not 1 <= len(offsets) <= 24:
            raise Refused("a walk needs 1 to 24 offsets")
        value, hops = start, []
        for offset in offsets:
            value = self.scalar(value + number(offset), cached=cached)
            hops.append(value)
            if not self.mapped(value):
                raise Unresolved(f"pointer walk unresolved at hop {len(hops) - 1}")
        return hops


def pe_sections(memory, base):
    if memory.read(base, 2) != b"MZ":
        raise Refused("module does not have an MZ header")
    pe = base + memory.scalar(base + 0x3C, "u32")
    if pe - base > 1024 * 1024 or memory.read(pe, 4) != b"PE\0\0":
        raise Refused("module does not have bounded PE headers")
    if memory.scalar(pe + 4, "u16") != 0x8664 or memory.scalar(pe + 24, "u16") != 0x20B:
        raise Refused("only PE32+ x86-64 modules are supported")
    count, optional = memory.scalar(pe + 6, "u16"), memory.scalar(pe + 20, "u16")
    image_size = memory.scalar(pe + 24 + 56, "u32")
    if not 1 <= count <= 96 or not 112 <= optional <= 4096 or not 0 < image_size <= 2 * 1024**3:
        raise Refused("invalid PE section bounds")
    result = []
    for i in range(count):
        raw = memory.read(pe + 24 + optional + i * 40, 40)
        name, size, rva = struct.unpack_from("<8sII", raw)
        flags = struct.unpack_from("<I", raw, 36)[0]
        if rva + size > image_size:
            raise Refused("section exceeds module image")
        result.append({"name": name.rstrip(b"\0").decode("ascii", "replace"), "rva": rva, "size": size,
                       "executable": bool(flags & 0x20000000), "writable": bool(flags & 0x80000000)})
    return result


def regions(sections, selector):
    if selector not in ("executable", "writable", "all"):
        raise Refused("section selector must be executable, writable or all")
    return [s for s in sections if s["size"] and (selector == "all" or s[selector])]


def scan(memory, base, sections, pattern, selector, limit):
    if not isinstance(pattern, str) or len(pattern) > 4096:
        raise Refused("invalid or oversized scan pattern")
    if not pattern.split() or any(not re.fullmatch(r"[0-9a-fA-F?]{2}", token) for token in pattern.split()):
        raise Refused("pattern needs space-separated hex bytes or nibble wildcards")
    if not 1 <= limit <= 64:
        raise Refused("match limit must be 1 to 64")
    compiled = compile_pattern(pattern)
    if compiled is None:
        raise Refused("scan needs a supported pattern with a literal first byte")
    selected = regions(sections, selector)
    if sum(s["size"] for s in selected) > MAX_SCAN_BYTES:
        raise Unavailable("selected module sections exceed the scan budget")
    hits = set()
    for section in selected:
        tail = b""
        for off in range(0, section["size"], 1024 * 1024):
            address = base + section["rva"] + off
            raw = memory.read(address, min(1024 * 1024, section["size"] - off))
            joined = tail + raw
            position = 0
            while (match := compiled.search(joined, position)) is not None:
                hits.add(address - len(tail) + match.start())
                if len(hits) >= limit:
                    return {"matches": sorted(hits), "complete": False, "reason": "match limit reached"}
                position = match.start() + 1
            tail = raw[-4096:]
    return {"matches": sorted(hits), "complete": True}


def find_chain(memory, base, sections, offsets, checks, limit):
    """Search module pointer slots with explicit endpoint/hop invariants."""
    if not isinstance(offsets, list) or not 1 <= len(offsets) <= 24:
        raise Refused("candidate walk needs 1 to 24 offsets")
    for offset in offsets:
        number(offset)
    if not 1 <= limit <= 64:
        raise Refused("candidate limit must be 1 to 64")
    selected = regions(sections, "writable")
    if sum(s["size"] for s in selected) > 32 * 1024 * 1024:
        raise Unavailable("writable sections exceed the chain-discovery budget")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 16:
        raise Refused("candidate discovery requires 1 to 16 independent checks")
    # Validate the recipe before the loop; a bad check is not a rejected candidate.
    for check in checks:
        if not isinstance(check, dict) or set(check) not in (
                {"hop", "offset", "equals_hop"}, {"hop", "offset", "bytes"}, {"hop", "offset", "type", "expect"}):
            raise Refused("discovery check needs a hop/offset and an explicit equality or byte assertion")
        number(check["hop"], len(offsets) - 1)
        if "equals_hop" in check:
            number(check["equals_hop"], len(offsets) - 1)
        elif "bytes" in check:
            if not isinstance(check["bytes"], str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,256}", check["bytes"]):
                raise Refused("byte assertion needs 1 to 256 literal bytes in hex")
        elif check["type"] not in FORMATS or type(check["expect"]) not in (int, float):
            raise Refused("scalar assertion needs a supported type and numeric expectation")
        number(check["offset"])
    def check_matches(hops, check, cached=True):
        a = hops[number(check["hop"])] + number(check["offset"])
        if "equals_hop" in check:
            return memory.scalar(a, cached=cached) == hops[number(check["equals_hop"])]
        if "bytes" in check:
            wanted = bytes.fromhex(check["bytes"])
            return memory.read(a, len(wanted), cached=cached) == wanted
        return memory.scalar(a, check["type"], cached=cached) == check["expect"]
    matches = []
    for section in selected:
        for off in range(0, section["size"], 65536):
            address = base + section["rva"] + off
            raw = memory.read(address, min(65536, section["size"] - off))
            for index in range(0, len(raw) - 7, 8):
                if not memory.mapped(struct.unpack_from("<Q", raw, index)[0]):
                    continue
                slot = address + index
                try:
                    hops = memory.walk(slot, offsets, cached=True)
                    if not all(check_matches(hops, c) for c in checks):
                        continue
                    # Reject a moving chain; cached values never prove stability.
                    if memory.walk(slot, offsets) != hops or not all(check_matches(hops, c, False) for c in checks):
                        continue
                except Unresolved:
                    continue
                matches.append({"slot": slot, "rva": slot - base, "hops": hops})
                if len(matches) >= limit:
                    return {"candidates": matches, "complete": False, "reason": "candidate limit reached"}
    return {"candidates": matches, "complete": True, "candidate_only": True}


def ue_name(memory, obj, pool):
    """Only the explicit 16/16-bit, two-byte-entry FName layout used by a recipe."""
    index = memory.scalar(obj + 0x18, "u32")
    block = memory.scalar(pool + 0x10 + (index >> 16) * 8)
    entry = block + (index & 0xFFFF) * 2
    header = memory.scalar(entry, "u16")
    count = header >> 6
    if header & 1 or not 1 <= count <= 255:
        raise Unavailable("FName entry is not a bounded narrow name in this layout")
    try:
        result = memory.read(entry + 2, count).decode("ascii")
    except UnicodeDecodeError as exc:
        raise Unavailable("FName entry is not ASCII") from exc
    if any(ord(c) < 32 or ord(c) == 127 for c in result):
        raise Unavailable("FName entry contains control characters")
    return result


def run_recipe(memory, base, sections, recipe):
    if not isinstance(recipe, dict) or type(recipe.get("schema")) is not int or recipe["schema"] != 1:
        raise Refused("recipe schema must be 1")
    operations = recipe.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise Refused("recipe needs 1 to 128 operations")
    values, results = {"module": base}, []
    def reference(value):
        if isinstance(value, str) and value in values:
            return values[value]
        if isinstance(value, str) and any(r["name"] == value for r in results):
            raise Unavailable("dependency has no unambiguous scalar result")
        return value
    def address(value):
        if isinstance(value, dict):
            if set(value) != {"array", "index"}:
                raise Refused("array address needs array and index")
            items = reference(value["array"])
            if not isinstance(items, list):
                raise Refused("array reference is not an array result")
            source = next((r for r in results if r["name"] == value["array"]), {})
            if source.get("kind") != "array":
                raise Refused("array address requires an explicit array operation")
            index = number(value["index"]) - source["start_index"]
            if index < 0 or index >= len(items):
                raise Unavailable("requested array element is not in this bounded reading")
            value = items[index]
        return number(reference(value))
    for op in operations:
        if not isinstance(op, dict) or not isinstance(op.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", op["name"]):
            raise Refused("operation needs a bounded unique name")
        name, kind = op["name"], op.get("kind")
        if not isinstance(kind, str) or kind not in OP_FIELDS or set(op) - (OP_FIELDS[kind] | {"name", "kind", "expect", "equals"}):
            raise Refused("unsupported operation or unknown recipe fields")
        if "expect" in op and "equals" in op:
            raise Refused("choose one expectation form")
        if name in values or any(r["name"] == name for r in results):
            raise Refused("duplicate operation name")
        answer = {"name": name, "kind": kind}
        try:
            selected = sections
            if "section_rvas" in op:
                wanted = op["section_rvas"]
                if not isinstance(wanted, list) or not 1 <= len(wanted) <= 96:
                    raise Refused("section_rvas needs 1 to 96 exact section RVAs")
                wanted = {number(rva) for rva in wanted}
                selected = [s for s in sections if s["rva"] in wanted]
                if {s["rva"] for s in selected} != wanted:
                    raise Refused("requested section RVA is absent")
            if kind == "address":
                value = number(address(op["base"]) + number(op.get("offset", 0)))
                answer["value"] = value
            elif kind == "read":
                if "size" in op and op.get("type") != "bytes":
                    raise Refused("size applies only to an explicit bytes read")
                a = address(op["base"]) + number(op.get("offset", 0))
                if op.get("type") == "bytes":
                    value = memory.read(a, number(op["size"], 256)).hex()
                else:
                    value = memory.scalar(a, op.get("type", "u64"))
                answer.update(address=a, value=value)
            elif kind == "walk":
                hops = memory.walk(address(op["base"]), op["offsets"])
                if memory.walk(address(op["base"]), op["offsets"]) != hops:
                    raise Unavailable("pointer chain changed during validation")
                value = hops[-1]
                answer.update(hops=hops, value=value)
            elif kind == "scan":
                answer.update(scan(memory, base, selected, op["pattern"], op.get("sections", "executable"), number(op.get("limit", 16), 64)))
                value = answer["matches"][0] if answer["complete"] and len(answer["matches"]) == 1 else None
            elif kind == "find_chain":
                answer.update(find_chain(memory, base, selected, op["offsets"], op["checks"], number(op.get("limit", 16), 64)))
                value = None  # A discovered pointer is never promoted to write authority.
            elif kind == "ue_fname_16_16":
                value = ue_name(memory, address(op["object"]), address(op["pool"]))
                answer["value"] = value
            elif kind == "rip_relative":
                a = address(op["base"])
                offset = number(op["displacement_offset"], 15)
                size = number(op["instruction_size"], 15)
                if offset + 4 > size:
                    raise Refused("displacement must fit inside the stated instruction")
                value = a + size + memory.scalar(a + offset, "i32")
                if not memory.mapped(value):
                    raise Unavailable("RIP-relative target is not readable")
                answer["value"] = value
            elif kind == "xor":
                def operand(item):
                    if isinstance(item, str) and item not in values and any(r["name"] == item for r in results):
                        raise Unavailable("dependency has no unambiguous scalar result")
                    return number(values[item] if isinstance(item, str) and item in values else item, (1 << 64) - 1)
                value = operand(op["left"]) ^ operand(op["right"])
                answer["value"] = value
            elif kind == "array":
                a, count = address(op["base"]), number(reference(op["count"]), 1_000_000)
                start, limit = number(op.get("start", 0), count), number(op.get("limit", 128), 512)
                element_type = op.get("type", "u64")
                if element_type not in FORMATS or limit < 1:
                    raise Refused("array needs a scalar element type and positive limit")
                size = struct.calcsize(FORMATS[element_type])
                stride = number(op.get("stride", size), 4096)
                if stride < size:
                    raise Refused("array stride is smaller than its element")
                value = [memory.scalar(a + index * stride, element_type) for index in range(start, min(count, start + limit))]
                answer.update(value=value, total_count=count, start_index=start, complete=start == 0 and len(value) == count)
            elif kind == "ue_objects_16_16":
                items, pool = reference(op["array"]), address(op["pool"])
                depth, limit = number(op.get("depth", 16), 40), number(op.get("limit", 128), 512)
                if not isinstance(items, list) or depth < 1 or limit < 1:
                    raise Refused("object survey needs an array and positive depth/limit")
                rows = []
                for item in items[:limit]:
                    row = {"address": item}
                    try:
                        if type(item) is not int or not 0x10000 < item <= MAX_ADDRESS:
                            raise Unavailable("array item is not a supported object pointer")
                        row["name"] = ue_name(memory, item, pool)
                        cls, lineage, seen = memory.scalar(item + 0x10), [], set()
                        if cls <= 0x10000 or not memory.mapped(cls, 0x48):
                            raise Unavailable("candidate has no readable object class")
                        row["class_address"] = cls
                        while cls and len(lineage) < depth and cls not in seen:
                            seen.add(cls)
                            lineage.append(ue_name(memory, cls, pool))
                            cls = memory.scalar(cls + 0x40)
                        row.update(classes=lineage, lineage_complete=cls == 0 and lineage[-1] == "Object", state="observed")
                    except Unavailable as exc:
                        row.update(state="inconclusive", reason=str(exc))
                    rows.append(row)
                source = next((r for r in results if r["name"] == op["array"]), {})
                value = rows
                answer.update(value=value, complete=source.get("complete", False) and len(items) <= limit
                              and all(row["state"] == "observed" and row.get("lineage_complete") for row in rows))
            else:
                raise Refused("unsupported recipe operation")
            expected = op.get("expect")
            if "equals" in op:
                expected_reference = op["equals"]
                if isinstance(expected_reference, str) and expected_reference not in values and any(r["name"] == expected_reference for r in results):
                    raise Unavailable("expectation dependency has no scalar result")
                if not isinstance(expected_reference, str) or expected_reference not in values or type(values[expected_reference]) not in (int, float):
                    raise Refused("equals needs a prior numeric result")
                expected = values[expected_reference]
            if ("expect" in op or "equals" in op) and (type(expected) is bool or value != expected):
                answer.update(state="mismatch", expected=expected)
            else:
                answer["state"] = "computed" if kind in ("address", "xor") else "observed"
            if value is not None:
                values[name] = value
        except Unavailable as exc:
            answer.update(state="inconclusive", reason=str(exc))
        except (KeyError, TypeError) as exc:
            raise Refused("malformed recipe operation") from exc
        results.append(answer)
    return results


def file_fingerprint(path):
    h = sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 2 * 1024**3:
            raise Refused("executable must be a bounded regular file")
        left = before.st_size
        while left:
            chunk = stream.read(min(left, 1024 * 1024))
            if not chunk:
                raise Refused("executable shortened during fingerprinting")
            h.update(chunk)
            left -= len(chunk)
        if stream.read(1):
            raise Refused("executable grew during fingerprinting")
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise Refused("executable changed during fingerprinting")
    return h.hexdigest(), after


def probe(app_id, table_sha, executable, recipe=None, *, rpc, proc_root=Path("/proc")):
    started = datetime.now(timezone.utc).isoformat()
    table_sha = digest(table_sha)
    profiles = rpc("list_profiles", [])
    profile = next((p for p in profiles if p.get("app_id") == app_id), None)
    if not profile or profile.get("table_sha256") != table_sha or not profile.get("target_process"):
        raise Refused("AppID, selected table SHA and explicit target must match the live profile")
    inspected = rpc("inspect_table_sha", [table_sha, app_id])  # Production verified_blob boundary.
    if inspected.get("sha256") != table_sha:
        raise Refused("inspection returned another table identity")
    capability = rpc("get_ce_launch_capability", [app_id])
    game = capability.get("game") or {}
    if game.get("app_id") != app_id or not game.get("running") or game.get("reason"):
        raise Refused("the backend cannot observe this exact running game")
    identities = [identity for pid in game.get("pids", [])
                  if (identity := resolve_target_identity(app_id, profile["target_process"], [pid], proc_root=proc_root))]
    if len(identities) != 1:
        raise Refused("the target process is missing or ambiguous")
    identity = identities[0]
    path = Path(executable).resolve(strict=True)
    if not Path(executable).is_absolute() or path.name.casefold() != profile["target_process"].casefold():
        raise Refused("supply the independently established exact target executable path")
    fingerprint, info = file_fingerprint(path)
    if recipe is not None and (not isinstance(recipe, dict) or digest(recipe.get("table_sha256")) != table_sha
                               or digest(recipe.get("executable_sha256")) != fingerprint):
        raise Refused("recipe does not match the exact table and executable bytes")
    recipe_sha = None if recipe is None else sha256(
        json.dumps(recipe, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    process = proc_root / str(identity.pid)
    with (process / "maps").open("r") as stream:
        raw = stream.read(MAX_MAP_BYTES + 1)
    if len(raw) > MAX_MAP_BYTES:
        raise Refused("process maps exceed the bound")
    maps = parse_maps(raw)
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
    modules = [m for m in maps if m.path == str(path) and m.offset == 0
               and m.inode == info.st_ino and m.device == device]
    if len(modules) != 1 or not confirm_target_identity(identity, proc_root=proc_root):
        raise Refused("exact file mapping or process start identity changed")
    fd = os.open(process / "mem", os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        memory = Memory(fd, maps)
        base = modules[0].start
        sections = pe_sections(memory, base)
        results = run_recipe(memory, base, sections, recipe) if recipe is not None else []
        if not confirm_target_identity(identity, proc_root=proc_root):
            raise Refused("process identity changed while reading memory")
    finally:
        os.close(fd)
    after = path.stat()
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
        raise Refused("executable identity changed during the probe")
    return {"schema": 1, "mode": "read_only_table_repair", "app_id": app_id,
            "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
            "recipe_sha256": recipe_sha,
            "table_sha256": table_sha, "executable_sha256": fingerprint, "executable": str(path),
            "process": asdict(identity), "module_base": base, "sections": sections,
            "results": results, "evidence_limit": "observations only; no table execution, writes, effect proof or compatibility verdict"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument("--table-sha", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        host_platform.require("The table-repair memory probe")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    try:
        recipe = bounded_json(args.recipe) if args.recipe else None
        answer = probe(args.appid, args.table_sha, args.executable, recipe,
                       rpc=lambda method, arguments: target_plugin_rpc.call(method, arguments, target_plugin_rpc.DEFAULT_DECKY_URL, 15))
        result = json.dumps(answer, indent=2, allow_nan=False) + "\n"
        if args.out:
            with args.out.open("x", encoding="utf-8") as stream:
                stream.write(result)
        print(result, end="")
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f"table memory probe: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
