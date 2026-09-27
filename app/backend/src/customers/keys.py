"""Each client's data-encryption key (DEK), wrapped under the deployment master key.

Phase 8's `store_document` seals a client's documents under `data_key(db, customer_id)`;
erasure (Task 7) deletes the client's `customer_document_keys` row, and every document under
it becomes unreadable at once — the crypto-shred of ADR-0001 §5. Who may delete the row is the
database's business (migration 0024), not this module's.

Crypto choices:

- The DEK is 32 bytes from `secrets.token_bytes` (the OS CSPRNG): an AES-256 key.
- It is wrapped with `core.crypto` — AES-256-GCM under `DOCUMENT_MASTER_KEY`, a fresh random
  96-bit nonce per wrap, stored in front of the ciphertext in the one `wrapped_key` column.
- The customer id is the GCM associated data. A wrapped key copied onto another client's row
  fails authentication instead of quietly opening that client's documents with this one's key.
- Unwrapping under the wrong master key, for the wrong client, or from an edited row raises
  `cryptography.exceptions.InvalidTag`. There is no fallback: a document key that cannot be
  authenticated is a hard error, never a new key and never a guess.
- `master_key_version` is 1 for every row. Rotating the master key (re-wrapping each row under
  a new one) is out of scope; the column is what a rotation would need to track its progress.

A key never leaves this module except to the caller that asked for it: never logged, never in
an API response, never in an audit row.
"""

import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core import crypto
from core.config import get_settings
from customers.models import CustomerDocumentKey

DEK_BYTES = crypto.KEY_BYTES


def wrap(dek: bytes, customer_id: uuid.UUID, master_key: str) -> str:
    # Deferred: binding `master_key_version` into the associated data too belongs to the
    # rotation ticket — adding it now would make every key already wrapped unreadable.
    return crypto.encrypt(dek.hex(), master_key, customer_id.bytes)


def unwrap(wrapped: str, customer_id: uuid.UUID, master_key: str) -> bytes:
    return bytes.fromhex(crypto.decrypt(wrapped, master_key, customer_id.bytes))


async def create_key(db: AsyncSession, customer_id: uuid.UUID) -> None:
    """Stage a fresh key in the caller's transaction. `ON CONFLICT DO NOTHING` on the primary
    key: two first uses at once make one key — the loser waits for the winner's commit and
    then reads the winner's row (`data_key`)."""
    wrapped = wrap(secrets.token_bytes(DEK_BYTES), customer_id, get_settings().document_master_key)
    await db.execute(
        insert(CustomerDocumentKey)
        .values(customer_id=customer_id, wrapped_key=wrapped)
        .on_conflict_do_nothing(index_elements=["customer_id"])
    )


async def data_key(db: AsyncSession, customer_id: uuid.UUID) -> bytes:
    """The client's DEK, creating it if the row is missing — a client from before 0024, or a
    purged client who has come back and starts over with a key their old documents were never
    sealed under. The caller commits."""
    query = select(CustomerDocumentKey.wrapped_key).where(
        CustomerDocumentKey.customer_id == customer_id
    )
    wrapped = await db.scalar(query)
    if wrapped is None:
        await create_key(db, customer_id)
        wrapped = await db.scalar(query)
    return unwrap(wrapped, customer_id, get_settings().document_master_key)


async def existing_key(db: AsyncSession, customer_id: uuid.UUID) -> bytes | None:
    """The client's DEK, or None once it has been shredded. Never creates one: code that only
    *reads* documents must not mint a key their documents were never sealed under — and a
    render for a purged client must find nothing rather than start them a fresh key."""
    wrapped = await db.scalar(
        select(CustomerDocumentKey.wrapped_key).where(
            CustomerDocumentKey.customer_id == customer_id
        )
    )
    if wrapped is None:
        return None
    return unwrap(wrapped, customer_id, get_settings().document_master_key)
