"""AES-256-GCM for the few small secrets that must survive a database dump unreadable.

A TOTP secret is not a password: the server has to present it to `pyotp` in the clear on
every verification, so it cannot be hashed. Storing it as written would mean a dump, a
backup or one stray `SELECT` hands over a working authenticator for every enrolled account —
the second factor defeated without touching anybody's password.

GCM rather than CBC or a bare cipher because it authenticates as well as encrypts: a
ciphertext somebody edited fails to decrypt instead of decrypting to a different secret. The
nonce is random per value and stored in front of the ciphertext, which is the only way a key
used for many values stays safe — a reused nonce with GCM leaks the key stream.

The key is a deployment secret like `JWT_SECRET`, passed in by the caller rather than read
here, so the document store (CLAUDE.md) can use this module with a key of its own instead of
sharing one. Losing a key makes everything under it unreadable, so both are escrowed.
"""

import base64
import os
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 96 bits, the size GCM is specified for; anything else costs an extra hashing step.
_NONCE_BYTES = 12
_TAG_BYTES = 16
KEY_BYTES = 32


@lru_cache
def _cipher(key_hex: str) -> AESGCM:
    """Cached per key: building the cipher parses the key, and this runs per request."""
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        raise ValueError(
            "Encryption keys are hex; generate one with `openssl rand -hex 32`."
        ) from None
    if len(key) != KEY_BYTES:
        raise ValueError(f"An AES-256 key is {KEY_BYTES} bytes ({KEY_BYTES * 2} hex characters).")
    return AESGCM(key)


def encrypt(plaintext: str, key_hex: str, associated_data: bytes | None = None) -> str:
    """Base64 of `nonce || ciphertext`, in one column and one round trip.

    `associated_data` is authenticated but not stored: `decrypt` must be handed the same
    bytes, so a ciphertext bound to one row cannot be copied onto another and still open."""
    nonce = os.urandom(_NONCE_BYTES)
    sealed = _cipher(key_hex).encrypt(nonce, plaintext.encode(), associated_data)
    return base64.b64encode(nonce + sealed).decode()


def decrypt(blob: str, key_hex: str, associated_data: bytes | None = None) -> str:
    """Raises `cryptography.exceptions.InvalidTag` for a wrong key, wrong associated data or
    an edited ciphertext — never returns something else."""
    raw = base64.b64decode(blob)
    cipher = _cipher(key_hex)
    return cipher.decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], associated_data).decode()


# --- bytes, under a raw key: the document store (`core/documents.py`) -------------------------
#
# Beside the str API rather than through it: a document is megabytes of bytes, and a per-client
# DEK arrives as raw bytes — base64 and hex round trips would buy nothing. Not cached per key
# like `_cipher`: an `lru_cache` of client DEKs would keep up to 128 of them alive in memory
# for the life of the process, and building an `AESGCM` is cheap next to sealing a PDF.


def _aes256(key: bytes) -> AESGCM:
    if len(key) != KEY_BYTES:
        raise ValueError(f"An AES-256 key is {KEY_BYTES} bytes.")
    return AESGCM(key)


def seal(plaintext: bytes, key: bytes, associated_data: bytes) -> bytes:
    """`nonce || ciphertext || tag`, with a fresh random 96-bit nonce every call."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + _aes256(key).encrypt(nonce, plaintext, associated_data)


def open_sealed(blob: bytes, key: bytes, associated_data: bytes) -> bytes:
    """Raises `InvalidTag` for a wrong key, other associated data or any edited byte —
    including a blob cut too short to hold a nonce and a tag, which AESGCM would otherwise
    answer with a `ValueError` for the empty nonce."""
    if len(blob) < _NONCE_BYTES + _TAG_BYTES:
        raise InvalidTag
    return _aes256(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], associated_data)
