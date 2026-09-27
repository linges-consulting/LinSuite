"""S2: direct field encryption for the Resend/SMTP credentials on `businesses` (Task 2, #11).

Same shape as `auth/mfa.py`'s TOTP-secret round trip. The `database` fixture is used only for
the fixed `NOTIFICATION_CREDENTIAL_KEY` env var it sets — nothing here touches a connection.
"""

from notifications.credentials import decrypt_credential, encrypt_credential


def test_a_credential_round_trips(database):
    sealed = encrypt_credential("re_live_abc123")
    assert sealed != "re_live_abc123"
    assert decrypt_credential(sealed) == "re_live_abc123"


def test_the_ciphertext_is_not_deterministic(database):
    # A fresh random nonce every call (core/crypto.py): two encryptions of the same value
    # never produce the same ciphertext.
    assert encrypt_credential("hunter2") != encrypt_credential("hunter2")
