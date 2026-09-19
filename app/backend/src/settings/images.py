"""What an uploaded logo or favicon is allowed to be, and what it is turned into.

**The file's own bytes decide its type, never its name or its `Content-Type`.** Both of those
are chosen by whoever is uploading. A `.png` that starts `GIF89a` is not a PNG, and a `.png`
that starts `<svg` is a script the browser would run from this origin the moment the image is
served — which is why SVG is refused outright rather than sanitised. Sanitising SVG is a
losing arms race for a format nobody needs for a 512-pixel logo.

**Everything is re-encoded.** The stored bytes are what Pillow wrote from the decoded pixels,
so whatever metadata, trailing payload or exotic chunk arrived with the upload does not
survive. Two pieces of that metadata have to be handled rather than merely not copied:

- **The ICC profile is carried in `im.info` and Pillow re-attaches it on save.** After a
  `convert("RGBA")` it describes a colour space the pixels are no longer in — a CMYK JPEG
  converted to RGB and then shipped with its CMYK profile renders wrong, which on a brand
  colour is the one thing this feature exists to get right. It is dropped explicitly.
- **EXIF orientation is applied, not discarded.** A photo from a phone is stored rotated and
  says so in a tag; drop the tag without rotating the pixels and the logo is sideways.

The size cap is the other half: a decoded-image bomb is refused before Pillow allocates.
"""

import io

from PIL import Image, ImageOps

LOGO_MAX_BYTES = 1024 * 1024
FAVICON_MAX_BYTES = 256 * 1024
LOGO_MAX_PX = 512
FAVICON_MAX_PX = 64
# The byte cap does not bound the *decoded* image: PNG compresses flat colour so well that a
# megabyte of it is a 20000x20000 bitmap, which is 1.6 GB of RAM the moment Pillow decodes it.
# So the header's dimensions are checked before anything is decoded. Fifty megapixels is a
# generous ceiling for a mark that ends up 512 pixels wide.
MAX_PIXELS = 50_000_000

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
    # Pillow's own bomb guard raises here for anything past twice *its* ceiling, which is a
    # size question and so a 413 like the others. Everything below that ceiling it merely
    # warns about, which is what the explicit check is for.
    try:
        # `open` reads the header only, so the dimensions are known before any pixels are.
        image = Image.open(io.BytesIO(data))
    except Image.DecompressionBombError:
        raise Rejected(413, "That image is too large to decode. Resize it first.") from None
    except Exception:
        raise Rejected(415, "That file could not be read as an image.") from None
    if image.width * image.height > MAX_PIXELS:
        raise Rejected(413, f"That image is {image.width}x{image.height}. Resize it first.")
    try:
        image.load()
    except Exception:
        raise Rejected(415, "That file could not be read as an image.") from None

    # Rotate before converting, while the tag is still attached to the pixels it describes.
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGBA")
    # Now in sRGB whatever it arrived as, so the old profile would be a lie on the way out.
    image.info.pop("icc_profile", None)
    # `thumbnail` is a no-op when the image already fits, so a small logo is re-encoded but
    # never upscaled — scaling a 64px mark up to 512 would only make it blurry.
    image.thumbnail((max_px, max_px))
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue(), image.width, image.height
