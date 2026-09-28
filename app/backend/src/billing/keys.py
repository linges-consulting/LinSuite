"""The business's single document-encryption key, wrapped under `DOCUMENT_MASTER_KEY`.

Mirrors `customers/keys.py`'s per-client DEK, except there is exactly one row instead of one
per client: `business_document_keys.business_id` is always 1, `businesses.id`'s own value.
Every financial document (#55/ADR-0003) is sealed under it, via `core.documents.store_document`
/`fetch_document` with `key_owner="business"`.

There is no `existing_key`/shred counterpart the way `customers.keys` has one: destroying this
row is not a supported operation in v1 (see `billing/models.py`'s docstring) — every business
document depends on this key existing for as long as the deployment does.
"""

import secrets

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from billing.models import BusinessDocumentKey
from core import crypto
from core.config import get_settings

DEK_BYTES = crypto.KEY_BYTES
BUSINESS_ID = 1
# Single-tenant means there is never a second business row to bind against — unlike a
# customer's DEK, whose wrap is bound to that customer's id (`customers/keys.py`).
_WRAP_AAD = b"linsuite/business-document-key"


def wrap(dek: bytes, master_key: str) -> str:
    return crypto.encrypt(dek.hex(), master_key, _WRAP_AAD)


def unwrap(wrapped: str, master_key: str) -> bytes:
    return bytes.fromhex(crypto.decrypt(wrapped, master_key, _WRAP_AAD))


async def create_key(db: AsyncSession) -> None:
    """Stage a fresh key in the caller's transaction. `ON CONFLICT DO NOTHING` on the primary
    key: two first uses at once make one key — the loser waits for the winner's commit and
    then reads the winner's row (`business_key`)."""
    wrapped = wrap(secrets.token_bytes(DEK_BYTES), get_settings().document_master_key)
    await db.execute(
        insert(BusinessDocumentKey)
        .values(business_id=BUSINESS_ID, wrapped_key=wrapped)
        .on_conflict_do_nothing(index_elements=["business_id"])
    )


async def business_key(db: AsyncSession) -> bytes:
    """The deployment's document key for business-owned documents, creating it if missing.
    The caller commits."""
    query = select(BusinessDocumentKey.wrapped_key).where(
        BusinessDocumentKey.business_id == BUSINESS_ID
    )
    wrapped = await db.scalar(query)
    if wrapped is None:
        await create_key(db)
        wrapped = await db.scalar(query)
    return unwrap(wrapped, get_settings().document_master_key)
