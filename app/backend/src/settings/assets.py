"""The logo and the favicon: storing them, and serving them to anybody.

Split out of `settings/routes.py` because the two halves have nothing in common but a
prefix. Everything else in that module is a small JSON document read and written by one
screen; this is bytes, content types, caching and conditional requests — and it is the only
part of the settings domain an anonymous browser talks to.

**Writing and reading are guarded differently on purpose.** The two `POST`s are the only
paths exempt from the JSON-only CSRF rule, so `main.py` allowlists them by name and checks
the `Origin` there, before a body is read. The `GET`s have no guard at all: a logo is on the
login screen, and a favicon is in the tab before anybody signs in.
"""

import hashlib
from datetime import UTC, datetime

from fastapi import HTTPException, Request, Response, UploadFile
from pydantic import BaseModel

from auth.models import User
from core.audit import record_event
from core.db import SessionDep
from settings import images
from settings.models import BrandingAsset


class AssetOut(BaseModel):
    url: str
    etag: str
    byte_length: int
    width: int
    height: int


def etag_of(digest: str) -> str:
    return f'"{digest}"'


def url_of(kind: str, digest: str) -> str:
    # The digest in the query string is what makes a replaced logo a different URL, so a
    # browser holding the old one never has to be told to forget it.
    return f"/api/branding/{kind}?v={digest[:12]}"


async def store(
    db: SessionDep, admin: User, kind: str, upload: UploadFile, *, allowed, max_px, cap
) -> AssetOut:
    data = await upload.read(cap + 1)
    if len(data) > cap:
        # The middleware refuses an honest `Content-Length` before the body is read; this is
        # the chunked-upload case, where the size is not known until it has arrived.
        raise HTTPException(status_code=413, detail=f"Keep it under {cap // 1024} KB.")
    try:
        encoded, width, height = images.normalise(data, allowed=allowed, max_px=max_px)
    except images.Rejected as rejected:
        raise HTTPException(status_code=rejected.status, detail=rejected.detail) from None

    digest = hashlib.sha256(encoded).hexdigest()
    asset = await db.get(BrandingAsset, kind)
    if asset is None:
        asset = BrandingAsset(kind=kind)
        db.add(asset)
    asset.content_type = "image/png"
    asset.data = encoded
    asset.byte_length = len(encoded)
    asset.sha256 = digest
    asset.updated_at = datetime.now(UTC)
    record_event(
        db,
        "business.branding_updated",
        target_type="business",
        target_id="1",
        actor_user_id=admin.id,
        metadata={kind: "uploaded", "sha256": digest},
    )
    await db.commit()
    return AssetOut(
        url=url_of(kind, digest),
        etag=etag_of(digest),
        byte_length=len(encoded),
        width=width,
        height=height,
    )


async def remove(db: SessionDep, admin: User, kind: str) -> Response:
    asset = await db.get(BrandingAsset, kind)
    if asset is not None:
        await db.delete(asset)
        record_event(
            db,
            "business.branding_updated",
            target_type="business",
            target_id="1",
            actor_user_id=admin.id,
            metadata={kind: "removed"},
        )
        await db.commit()
    return Response(status_code=204)


def _matches(header: str, etag: str) -> bool:
    """RFC 9110 §13.1.2: `*` matches anything present, and `W/"x"` is weak-equal to `"x"`.

    Weak comparison is the right one here whatever the client sends: these bytes are
    byte-for-byte identical whenever the digest is, so there is no distinction to preserve.
    Exact string equality would answer 200 to a browser that revalidated with `W/`, which is
    a megabyte down the wire for nothing.
    """
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.removeprefix("W/") == etag:
            return True
    return False


async def serve(db: SessionDep, request: Request, kind: str) -> Response:
    asset = await db.get(BrandingAsset, kind)
    if asset is None:
        raise HTTPException(status_code=404, detail="Not Found")
    etag = etag_of(asset.sha256)
    # `max-age` is short and the ETag does the real work: the URL carries the digest, so a
    # changed logo is a changed URL and the stale copy is never asked for again anyway.
    headers = {"ETag": etag, "Cache-Control": "public, max-age=300"}
    if _matches(request.headers.get("if-none-match", ""), etag):
        return Response(status_code=304, headers=headers)
    return Response(asset.data, media_type=asset.content_type, headers=headers)
