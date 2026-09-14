import struct
import pytest

from ce_decky.shortcuts_vdf import (
    Node, OBJECT, STRING, INT32, UINT64, BinaryVdfError,
    parse_binary, serialize_binary,
)


def fixture(appid: int = 0xF1234567, launch='OLD=1 %command%') -> bytes:
    return serialize_binary([
        Node(OBJECT, 'shortcuts', [
            Node(OBJECT, '0', [
                Node(INT32, 'appid', appid if appid < 2**31 else appid - 2**32),
                Node(STRING, 'AppName', 'Example Game'),
                Node(STRING, 'Exe', '"/games/Game.exe"'),
                Node(STRING, 'LaunchOptions', launch),
                Node(UINT64, 'LastPlayTime', 123456789),
            ]),
            Node(OBJECT, '1', [
                Node(INT32, 'appid', 42),
                Node(STRING, 'AppName', 'Other'),
                Node(STRING, 'LaunchOptions', ''),
            ]),
        ])
    ])


def test_roundtrip_is_byte_exact():
    raw = fixture()
    assert serialize_binary(parse_binary(raw)) == raw


@pytest.mark.parametrize('corrupt', [
    b'',
    b'\x00broken\x00',
    b'\x09x\x00\x08',
    b'\x02appid\x00\x01\x02',
    b'\x01name\x00unterminated',
])
def test_corrupt_or_unknown_vdf_fails_closed(corrupt):
    with pytest.raises(BinaryVdfError):
        parse_binary(corrupt)


def test_trailing_bytes_fail_closed():
    with pytest.raises(BinaryVdfError):
        parse_binary(fixture() + b'garbage')


def test_vdf_serializer_rejects_coerced_or_out_of_range_integer_nodes():
    for node in (
        Node(INT32, 'x', True),
        Node(INT32, 'x', '1'),
        Node(INT32, 'x', 0x80000000),
        Node(UINT64, 'x', -1),
        Node(UINT64, 'x', 0x1_0000_0000_0000_0000),
    ):
        with pytest.raises(BinaryVdfError, match='invalid value'):
            serialize_binary([node])
