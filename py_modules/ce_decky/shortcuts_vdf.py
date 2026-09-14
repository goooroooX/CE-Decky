"""Strict read-only parser for Steam's binary `shortcuts.vdf`.

CE Decky never writes Steam state, so this reads and never mutates: the
production plugin enumerates non-Steam shortcuts through SteamClient, and this
parser exists for `scripts/provider_search_survey.py`, which measures table
search against the shortcuts installed on a development machine.
`serialize_binary` is kept because `parse_binary` proves a byte-exact round
trip before trusting what it read.
"""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import TypeAlias

from .text import utf8_bytes


class BinaryVdfError(ValueError):
    pass


@dataclass(frozen=True)
class Node:
    tag: int
    key: str
    value: object


Nodes: TypeAlias = list[Node]
END = 0x08
OBJECT = 0x00
STRING = 0x01
INT32 = 0x02
UINT64 = 0x07
SUPPORTED = {OBJECT, STRING, INT32, UINT64}
MAX_VDF_BYTES = 64 * 1024 * 1024


def _read_cstring(data: bytes, pos: int) -> tuple[str, int]:
    try:
        end = data.index(b'\x00', pos)
    except ValueError as exc:
        raise BinaryVdfError('unterminated VDF string') from exc
    try:
        text = data[pos:end].decode('utf-8', errors='strict')
    except UnicodeDecodeError as exc:
        raise BinaryVdfError('non-UTF-8 VDF string') from exc
    return text, end + 1


def _parse_nodes(data: bytes, pos: int, *, depth: int, max_depth: int, counter: list[int], max_nodes: int) -> tuple[Nodes, int]:
    if depth > max_depth:
        raise BinaryVdfError('VDF nesting too deep')
    out: Nodes = []
    while True:
        if pos >= len(data):
            raise BinaryVdfError('truncated VDF: missing end marker')
        tag = data[pos]
        pos += 1
        if tag == END:
            return out, pos
        if tag not in SUPPORTED:
            raise BinaryVdfError(f'unsupported binary VDF tag 0x{tag:02x}')
        counter[0] += 1
        if counter[0] > max_nodes:
            raise BinaryVdfError('too many VDF nodes')
        key, pos = _read_cstring(data, pos)
        if tag == OBJECT:
            value, pos = _parse_nodes(data, pos, depth=depth + 1, max_depth=max_depth, counter=counter, max_nodes=max_nodes)
        elif tag == STRING:
            value, pos = _read_cstring(data, pos)
        elif tag == INT32:
            if pos + 4 > len(data):
                raise BinaryVdfError('truncated int32')
            value = struct.unpack_from('<i', data, pos)[0]
            pos += 4
        else:  # UINT64
            if pos + 8 > len(data):
                raise BinaryVdfError('truncated uint64')
            value = struct.unpack_from('<Q', data, pos)[0]
            pos += 8
        out.append(Node(tag, key, value))


def parse_binary(data: bytes, *, max_depth: int = 32, max_nodes: int = 100_000) -> Nodes:
    if not isinstance(data, bytes):
        raise BinaryVdfError('binary VDF input must be bytes')
    if len(data) <= 0 or len(data) > MAX_VDF_BYTES:
        raise BinaryVdfError('binary VDF size is invalid')
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not (1 <= max_depth <= 256):
        raise BinaryVdfError('max_depth is invalid')
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not (1 <= max_nodes <= 1_000_000):
        raise BinaryVdfError('max_nodes is invalid')
    nodes, pos = _parse_nodes(data, 0, depth=0, max_depth=max_depth, counter=[0], max_nodes=max_nodes)
    if pos != len(data):
        raise BinaryVdfError(f'trailing bytes after top-level VDF end marker: {len(data) - pos}')
    # Important safety invariant: if we cannot losslessly understand the source,
    # we do not mutate Steam's file.
    if serialize_binary(nodes) != data:
        raise BinaryVdfError('binary VDF did not round-trip byte-for-byte')
    return nodes


def _cstring(value: str) -> bytes:
    if not isinstance(value, str):
        raise BinaryVdfError('VDF string must be text')
    if '\x00' in value:
        raise BinaryVdfError('NUL in VDF string')
    try:
        return utf8_bytes(value, 'VDF string') + b'\x00'
    except ValueError as exc:
        raise BinaryVdfError(str(exc)) from exc


def serialize_binary(nodes: Nodes) -> bytes:
    out = bytearray()
    for node in nodes:
        if node.tag not in SUPPORTED:
            raise BinaryVdfError(f'cannot serialize VDF tag 0x{node.tag:02x}')
        out.append(node.tag)
        out.extend(_cstring(node.key))
        if node.tag == OBJECT:
            if not isinstance(node.value, list):
                raise BinaryVdfError('object node does not contain a node list')
            out.extend(serialize_binary(node.value))
        elif node.tag == STRING:
            if not isinstance(node.value, str):
                raise BinaryVdfError('string node has non-string value')
            out.extend(_cstring(node.value))
        elif node.tag == INT32:
            if isinstance(node.value, bool) or not isinstance(node.value, int) or not (-0x80000000 <= node.value <= 0x7FFFFFFF):
                raise BinaryVdfError('int32 node has invalid value')
            out.extend(struct.pack('<i', node.value))
        elif node.tag == UINT64:
            if isinstance(node.value, bool) or not isinstance(node.value, int) or not (0 <= node.value <= 0xFFFFFFFFFFFFFFFF):
                raise BinaryVdfError('uint64 node has invalid value')
            out.extend(struct.pack('<Q', node.value))
    out.append(END)
    return bytes(out)
