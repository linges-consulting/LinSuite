"""Direct field encryption for the Resend/SMTP credentials on `businesses` (Task 2, #11).

Same shape as `auth/mfa.py`'s TOTP-secret encryption: one AES-256-GCM key
(`NOTIFICATION_CREDENTIAL_KEY`), no associated data, no per-record wrapped key. Contrast
`customers/keys.py`, which wraps a key per customer because there are many of them and each
needs independent crypto-shredding — there is one business row here and no shredding
requirement, so direct field encryption under a dedicated key is the right amount of
machinery (owner decision, m3.md).

`encrypt_credential`/`decrypt_credential` are the only place `core.crypto` is called for
these columns (`resend_api_key_encrypted`, `smtp_password_encrypted`).
"""

from core import crypto
from core.config import get_settings


def encrypt_credential(value: str) -> str:
    return crypto.encrypt(value, get_settings().notification_credential_key)


def decrypt_credential(sealed: str) -> str:
    return crypto.decrypt(sealed, get_settings().notification_credential_key)
