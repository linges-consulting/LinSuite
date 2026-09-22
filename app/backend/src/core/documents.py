"""The document store: sealed bytes in PostgreSQL, verified on every read (CLAUDE.md).

`store_document` seals a document with AES-256-GCM under a key the caller hands in and stages
the row in the caller's transaction; `fetch_document` opens it and checks its SHA-256. Nothing
else reads or writes `documents`.

**The key is a parameter.** `core/` imports no domain module, and the per-client DEK lives in
`customers.keys` — so the caller fetches it (`data_key` to write, `existing_key` to read) and
passes it in. That also keeps the store honest about what it does not know: not every future
document need be sealed under a client's DEK (invoices are kept six years for the CRA, and an
erasure must not destroy them — pre-flight C10).

**What binds a blob to its row.** The id is chosen here, before sealing, and it and the
customer id are the GCM associated data: a ciphertext copied onto another row, or a row
re-pointed at another client, fails authentication. The plaintext's SHA-256 is stored beside
it and compared after every decrypt. Either failure raises `DocumentIntegrityError` — never a
partial read, never a fallback — and is logged by id only.

**If storage ever moves** (tech-stack §3 keeps the door open to object storage at scale), this
file is the one to change: callers see ids and bytes, never the table.
"""

import hashlib
import hmac
import logging
import uuid

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core import crypto
from core.errors import DocumentIntegrityError
from core.models import Document

log = logging.getLogger(__name__)


def _associated_data(document_id: uuid.UUID, customer_id: uuid.UUID) -> bytes:
    return document_id.bytes + customer_id.bytes


async def store_document(
    db: AsyncSession,
    *,
    key: bytes,
    customer_id: uuid.UUID,
    kind: str,
    source_id: uuid.UUID,
    content: bytes,
    content_type: str,
) -> uuid.UUID:
    """Seal and stage one document; the caller commits (with the retention hold it creates —
    ADR-0001 rule 7). Idempotent on `(kind, source_id)`: a second store of the same source
    keeps the first row and returns its id."""
    document_id = uuid.uuid4()
    inserted = await db.scalar(
        insert(Document)
        .values(
            id=document_id,
            customer_id=customer_id,
            kind=kind,
            source_id=source_id,
            content_type=content_type,
            ciphertext=crypto.seal(content, key, _associated_data(document_id, customer_id)),
            sha256=hashlib.sha256(content).digest(),
            size_bytes=len(content),
        )
        .on_conflict_do_nothing(index_elements=["kind", "source_id"])
        .returning(Document.id)
    )
    if inserted is not None:
        return inserted
    # Same source under another client is a caller bug: `scalar_one` raises rather than
    # hand back somebody else's document id.
    existing = await db.execute(
        select(Document.id).where(
            Document.kind == kind,
            Document.source_id == source_id,
            Document.customer_id == customer_id,
        )
    )
    return existing.scalar_one()


async def fetch_document(
    db: AsyncSession, *, key: bytes, document_id: uuid.UUID
) -> tuple[bytes, str]:
    """(content, content_type). Raises `DocumentIntegrityError` if the blob does not
    authenticate under this key and row, or its hash does not match; `NoResultFound` if there
    is no such document."""
    row = (
        await db.execute(
            select(
                Document.customer_id, Document.ciphertext, Document.sha256, Document.content_type
            ).where(Document.id == document_id)
        )
    ).one()
    try:
        content = crypto.open_sealed(
            row.ciphertext, key, _associated_data(document_id, row.customer_id)
        )
    except InvalidTag:
        log.error("document %s failed authentication", document_id)
        raise DocumentIntegrityError(str(document_id)) from None
    if not hmac.compare_digest(hashlib.sha256(content).digest(), row.sha256):
        log.error("document %s failed its SHA-256 check", document_id)
        raise DocumentIntegrityError(str(document_id))
    return content, row.content_type
