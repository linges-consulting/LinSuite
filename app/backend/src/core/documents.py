"""The document store: sealed bytes in PostgreSQL, verified on every read (CLAUDE.md).

`store_document` seals a document with AES-256-GCM under a key the caller hands in and stages
the row in the caller's transaction; `fetch_document` opens it and checks its digest. Nothing
else reads or writes `documents`.

**The key is a parameter, and so is the tier it belongs to.** `core/` imports no domain
module, so it does not know where a key comes from — the per-client DEK lives in
`customers.keys`, the business's own key in `billing.keys` (#55/ADR-0003) — the caller fetches
one and passes it in, along with `key_owner` naming which tier it is. `key_owner` is never
inferred from whether `customer_id` is set: an explicit, required parameter, because a caller
that forgot it would otherwise seal a financial document under the wrong assumption silently.

**What binds a blob to its row.** The id is chosen here, before sealing, and it (plus the
customer id, for a customer-keyed document) are the GCM associated data: a ciphertext copied
onto another row, onto another tier's row, or a row re-pointed at another client, fails
authentication. A business-keyed document has no customer id to bind — single-tenant means
there is never a second business to confuse it with, so the document id alone is enough. An
HMAC-SHA256 of the plaintext is stored beside it and compared (constant-time) after every
decrypt. Either failure raises `DocumentIntegrityError` — never a partial read, never a
fallback — and is logged by id only.

**Why a keyed digest, not a bare SHA-256.** A plain hash is the one value a `pg_dump` or offsite
backup holder could use *without* the master key, to confirm a guessed low-entropy document —
and backups outlive the shred. The HMAC key is derived (HKDF-SHA256, fixed `info`) from the key
the document is sealed under, so the digest is as unusable as the ciphertext once that key goes.
A separate subkey rather than the DEK itself: one key, one job.

**If storage ever moves** (tech-stack §3 keeps the door open to object storage at scale), this
file is the one to change: callers see ids and bytes, never the table.
"""

import hashlib
import hmac
import logging
import uuid
from datetime import datetime
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core import crypto
from core.errors import DocumentIntegrityError, DocumentNotFound
from core.models import Document

log = logging.getLogger(__name__)


_DIGEST_INFO = b"linsuite/document-digest"

# The two key tiers (#55/ADR-0003) — a `store_document`/`fetch_document` caller names one
# explicitly, it is never inferred from whether `customer_id` happens to be set.
DocumentOwner = Literal["customer", "business"]


def _digest(content: bytes, key: bytes) -> bytes:
    subkey = HKDF(algorithm=SHA256(), length=32, salt=None, info=_DIGEST_INFO).derive(key)
    return hmac.new(subkey, content, hashlib.sha256).digest()


def _associated_data(document_id: uuid.UUID, customer_id: uuid.UUID | None) -> bytes:
    return document_id.bytes + (customer_id.bytes if customer_id is not None else b"")


def _check_owner(
    key_owner: DocumentOwner, customer_id: uuid.UUID | None, linked_customer_id: uuid.UUID | None
) -> None:
    if key_owner == "customer":
        if customer_id is None:
            raise ValueError("a customer-keyed document requires customer_id")
        if linked_customer_id is not None:
            raise ValueError("linked_customer_id only applies to a business-keyed document")
    elif customer_id is not None:
        raise ValueError("a business-keyed document must not set customer_id")


async def store_document(
    db: AsyncSession,
    *,
    key: bytes,
    key_owner: DocumentOwner,
    customer_id: uuid.UUID | None = None,
    linked_customer_id: uuid.UUID | None = None,
    retain_until: datetime | None = None,
    kind: str,
    source_id: uuid.UUID,
    content: bytes,
    content_type: str,
) -> uuid.UUID:
    """Seal and stage one document; the caller commits (with the retention hold it creates for
    a customer-keyed one — ADR-0001 rule 7). Idempotent on `(kind, source_id)`: a second store
    of the same source keeps the first row and returns its id.

    `key_owner="customer"` is the original tier, unchanged: `customer_id` is required.
    `key_owner="business"` seals under the deployment's own key instead (#55/ADR-0003);
    `customer_id` must be None (that FK must not exist for a financial document — it has to
    outlive any one customer's crypto-shred), and `linked_customer_id`/`retain_until` are that
    tier's own, lock-free reference to a client and its CRA retention date.
    """
    _check_owner(key_owner, customer_id, linked_customer_id)
    document_id = uuid.uuid4()
    inserted = await db.scalar(
        insert(Document)
        .values(
            id=document_id,
            customer_id=customer_id,
            key_owner=key_owner,
            linked_customer_id=linked_customer_id,
            retain_until=retain_until,
            kind=kind,
            source_id=source_id,
            content_type=content_type,
            ciphertext=crypto.seal(content, key, _associated_data(document_id, customer_id)),
            digest=_digest(content, key),
            size_bytes=len(content),
        )
        .on_conflict_do_nothing(index_elements=["kind", "source_id"])
        .returning(Document.id)
    )
    if inserted is not None:
        return inserted
    # Same source under another owner is a caller bug: `scalar_one` raises rather than hand
    # back somebody else's document id.
    existing = await db.execute(
        select(Document.id).where(
            Document.kind == kind,
            Document.source_id == source_id,
            Document.key_owner == key_owner,
            Document.customer_id == customer_id,
        )
    )
    return existing.scalar_one()


async def fetch_document(
    db: AsyncSession,
    *,
    key: bytes,
    key_owner: DocumentOwner,
    customer_id: uuid.UUID | None = None,
    document_id: uuid.UUID,
) -> tuple[bytes, str]:
    """(content, content_type) of this document. Raises `DocumentNotFound` for an unknown id,
    one belonging to another client, or one sealed under the other tier, and
    `DocumentIntegrityError` if the blob does not authenticate under this key and row or its
    digest does not match."""
    _check_owner(key_owner, customer_id, None)
    conditions = [Document.id == document_id, Document.key_owner == key_owner]
    if key_owner == "customer":
        conditions.append(Document.customer_id == customer_id)
    row = (
        await db.execute(
            select(Document.ciphertext, Document.digest, Document.content_type).where(*conditions)
        )
    ).one_or_none()
    if row is None:
        raise DocumentNotFound(str(document_id))
    try:
        content = crypto.open_sealed(
            row.ciphertext, key, _associated_data(document_id, customer_id)
        )
    except InvalidTag:
        log.error("document %s failed authentication", document_id)
        raise DocumentIntegrityError(str(document_id)) from None
    if not hmac.compare_digest(_digest(content, key), row.digest):
        log.error("document %s failed its digest check", document_id)
        raise DocumentIntegrityError(str(document_id))
    return content, row.content_type
