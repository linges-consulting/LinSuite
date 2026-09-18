"""What an uploaded logo or favicon is allowed to be, and what it is turned into.

**The file's own bytes decide its type, never its name or its `Content-Type`.** Both of those
are chosen by whoever is uploading. A `.png` that starts `GIF89a` is not a PNG, and a `.png`
that starts `<svg` is a script the browser would run from this origin the moment the image is
served — which is why SVG is refused outright rather than sanitised. Sanitising SVG is a
losing arms race for a format nobody needs for a 512-pixel logo.

**Everything is re-encoded.** The stored bytes are what Pillow wrote from the decoded pixels,
so whatever metadata, trailing payload or exotic chunk arrived with the upload does not
survive. The size cap is the other half: a decoded-image bomb is refused by the cap before
Pillow is asked to allocate anything.
"""

import io

from PIL import Image

LOGO_MAX_BYTES = 1024 * 1024
FAVICON_MAX_BYTES = 256 * 1024
LOGO_MAX_PX = 512
FAVICON_MAX_PX = 64

PNG = b"\x89PNG\r\n\x1a\n"
JPEG = b"\xff\xd8\xff"
ICO = b"\x00\x00\x01\x00"


class Rejected(Exception):
    """A file this will not store. `status` is what the route answers with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def sniff(data: bytes) -> str | None:
    """The format these bytes actually are, or None for anything not permitted."""
    if data.startswith(PNG):
        return "png"
    if data.startswith(JPEG):
        return "jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(ICO):
        return "ico"
    return None


def normalise(data: bytes, *, allowed: tuple[str, ...], max_px: int) -> tuple[bytes, int, int]:
    """Re-encode to a PNG no larger than `max_px` on its longest side."""
    if sniff(data) not in allowed:
        raise Rejected(
            415,
            "Use a "
            + " or ".join(fmt.upper() for fmt in allowed)
            + " image. SVG is not accepted, because it can carry script.",
        )
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:
        raise Rejected(415, "That file could not be read as an image.") from None

    # `thumbnail` is a no-op when the image already fits, so a small logo is re-encoded but
    # never upscaled — scaling a 64px mark up to 512 would only make it blurry.
    image = image.convert("RGBA")
    image.thumbnail((max_px, max_px))
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue(), image.width, image.height
