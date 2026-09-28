"""The business-owned document key tier (#55, ADR-0003): one wrapped key for every
financial document, independent of any customer's own crypto-shred.

`billing/keys.py`'s `business_key` wraps and unwraps it exactly as `customers/keys.py` does
for a client's DEK, except there is exactly one row — `business_id` is always 1, the same
value `businesses.id`'s own CHECK pins it to — and it never shreds: a business's financial
documents are retained for the CRA's six-year rule regardless of what happens to any one
customer's clinical key. Migration 0044's `business_document_keys_guard` refuses UPDATE and
DELETE to every runtime role, unconditionally — there is no purge-eligible branch like
`customer_document_keys`'s, because nothing in v1 ever destroys this key.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, SmallInteger, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class BusinessDocumentKey(Base):
    __tablename__ = "business_document_keys"

    business_id: Mapped[int] = mapped_column(Integer, ForeignKey("businesses.id"), primary_key=True)
    # `core.crypto` format: base64 of nonce || ciphertext+tag. The nonce lives inside it.
    wrapped_key: Mapped[str] = mapped_column(Text)
    # Same purpose as `CustomerDocumentKey.master_key_version`: always 1 until master-key
    # rotation exists (out of scope).
    master_key_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
