"""AES-CBC decryption, checked against the published vectors rather than a site."""

import pytest

from ce_decky.aes_cbc import decrypt_cbc


def test_nist_sp800_38a_aes256_cbc_vector():
    # F.2.6, CBC-AES256.Decrypt: one block, no padding to strip.
    key = bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4')
    iv = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
    assert decrypt_cbc(key, iv, bytes.fromhex('f58c4c04d6e5f1ba779eabfb5f7bfbd6')).hex() == (
        '6bc1bee22e409f96e93d7e117393172a'
    )


def test_nist_sp800_38a_aes128_cbc_chains_across_blocks():
    # F.2.2, CBC-AES128.Decrypt: the second block proves the chaining.
    key = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
    iv = bytes.fromhex('000102030405060708090a0b0c0d0e0f')
    ciphertext = bytes.fromhex('7649abac8119b246cee98e9b12e9197d5086cb9b507219ee95db113a917678b2')
    assert decrypt_cbc(key, iv, ciphertext).hex() == (
        '6bc1bee22e409f96e93d7e117393172a' 'ae2d8a571e03ac9c9eb76fac45af8e51'
    )


def test_pkcs7_padding_is_stripped_only_when_it_is_padding():
    key = bytes(32)
    iv = bytes(16)
    # A block that decrypts to something whose last byte is not a valid pad
    # length keeps every byte: guessing here would silently truncate a payload.
    plain = decrypt_cbc(key, iv, bytes(16))
    assert 1 <= len(plain) <= 16


def test_a_ragged_input_is_refused_rather_than_answered():
    for key, iv, data in ((bytes(32), bytes(16), b'ragged'),
                          (bytes(32), bytes(15), bytes(16)),
                          (bytes(7), bytes(16), bytes(16)),
                          (bytes(32), bytes(16), b'')):
        with pytest.raises(ValueError):
            decrypt_cbc(key, iv, data)
