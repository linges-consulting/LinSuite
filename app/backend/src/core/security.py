"""Password hashing (tech-stack §14).

Argon2id with argon2-cffi's defaults, which track the RFC 9106 recommendations. The
parameters live in the hash string, so raising them later only affects new hashes and
`PasswordHasher.check_needs_rehash` handles the rest — no migration.

Policy: 12 characters minimum, no composition rules (NIST SP 800-63B). Breach screening
and verification belong to the login flow and are not implemented here yet.
"""

from argon2 import PasswordHasher

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)
