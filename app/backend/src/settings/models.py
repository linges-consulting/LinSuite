"""The two branding images, in the database like every other blob this app stores.

A table of its own rather than two `bytea` columns on `businesses`: that row is read on
almost every request — the MFA policy, the password-rotation interval, the business name —
and columns are loaded whether or not they are selected. Putting up to a megabyte of PNG on
it would put that megabyte on the wire for every one of those reads.

Two rows at most, keyed by what the image *is*. An upload replaces the row rather than
appending, because there is no history to keep here: the previous logo is not a record of
anything. `sha256` is the `ETag` the public route serves, so a browser that already has this
logo is answered 304 rather than the bytes.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base

LOGO = "logo"
FAVICON = "favicon"


class BrandingAsset(Base):
    __tablename__ = "branding_assets"
    __table_args__ = (
        CheckConstraint("kind IN ('logo', 'favicon')", name="ck_branding_assets_kind"),
    )

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Always `image/png`: everything is re-encoded on the way in (`settings/images.py`). The
    # column exists because the route must not hard-code a content type it happens to know.
    content_type: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    byte_length: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
