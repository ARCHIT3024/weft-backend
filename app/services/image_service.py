"""Image intake for issue photos: validate, strip metadata, resize, store.

Everything a citizen uploads passes through `store_issue_image`.  The order of
the steps is load-bearing and is documented at each one; the two that matter
most are:

* **Size is checked before anything is decoded.** Decoding is the expensive
  step, so the cheap length check goes first — otherwise an attacker chooses
  how much CPU and memory each request costs.
* **The image is re-encoded from raw pixels, so EXIF cannot survive.** That is
  a privacy requirement, not a bandwidth optimisation. Phone photos carry GPS
  coordinates. A citizen who deliberately drops a coarse pin — a block away
  from home, say — while attaching a photo taken at home would otherwise ship
  their exact address inside the file, defeating the coarsening entirely.

The caller-supplied filename never reaches the stored path.  It is not
sanitised; it is simply not used.  The key is `issues/{issue_id}/{uuid4}.jpg`,
built from a UUID the server already trusts and one it just generated, which
makes `../../../etc/passwd` or `C:\\Windows\\x.jpg` structurally incapable of
escaping the media root rather than merely filtered out.
"""

from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings
from app.core.exceptions import BadRequestError
from app.core.storage import ObjectStorage, get_storage

logger = logging.getLogger(__name__)

# HEIC/HEIF is what modern iPhones shoot by default, so supporting it matters
# for real intake — but `pillow-heif` is not installed in every environment
# this runs in, and a hard import would take the whole app down. Register it
# when present; when absent, HEIC simply is not among the accepted formats and
# an iPhone upload is rejected with INVALID_IMAGE like any other unsupported
# file. Nothing else in this module branches on it.
try:  # pragma: no cover - exercised only where pillow-heif is installed
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # pragma: no cover - the path taken in this environment
    HEIF_SUPPORTED = False

# Pillow format names, not MIME types and not file extensions — this is what
# `Image.format` reports, and it is derived from the bytes themselves.
_ALLOWED_FORMATS: set[str] = {"JPEG", "PNG", "WEBP"}
if HEIF_SUPPORTED:
    _ALLOWED_FORMATS |= {"HEIF", "HEIC"}

# Everything is normalised to JPEG. 85 is the usual quality/size knee; above it
# file size climbs faster than anything a viewer can see.
OUTPUT_FORMAT = "JPEG"
OUTPUT_CONTENT_TYPE = "image/jpeg"
OUTPUT_EXTENSION = "jpg"
JPEG_QUALITY = 85

# A decompression bomb — a small file that expands to a huge bitmap — is a
# denial-of-service vector that the byte-length check above cannot see, because
# the danger is in the decoded pixels, not the file. Pillow warns past
# ~89 Mpx by default and raises past twice that; make it raise at the first
# threshold instead, since nothing a phone camera produces comes close.
Image.MAX_IMAGE_PIXELS = 50_000_000


@dataclass(frozen=True)
class StoredImage:
    """The result of a successful upload."""

    file_path: str  # exactly what gets persisted to issue_images.file_path
    url: str  # client-fetchable URL
    content_type: str
    size_bytes: int


async def store_issue_image(
    *,
    issue_id: uuid.UUID,
    filename: str,
    raw: bytes,
    storage: ObjectStorage | None = None,
) -> StoredImage:
    """Validate, sanitise and store one image for `issue_id`.

    `filename` is used only to make errors legible. It does not influence the
    stored path, the format, or the accept/reject decision — that is decided by
    the bytes.

    Raises:
        BadRequestError: `IMAGE_TOO_LARGE` if over `settings.MAX_IMAGE_BYTES`,
            `INVALID_IMAGE` if the bytes are not a supported image.
    """
    storage = storage if storage is not None else get_storage()

    # ── 1. Size, before any decoding ─────────────────────────────────────
    if len(raw) > settings.MAX_IMAGE_BYTES:
        raise BadRequestError(
            code="IMAGE_TOO_LARGE",
            message=f"Image exceeds the maximum size of {settings.MAX_IMAGE_BYTES} bytes.",
            details={"max_bytes": settings.MAX_IMAGE_BYTES, "actual_bytes": len(raw)},
        )
    if not raw:
        raise BadRequestError(code="INVALID_IMAGE", message="Uploaded file is empty.")

    # ── 2. Identify by content, never by name or Content-Type ────────────
    image = _decode_and_verify(raw, filename)

    # ── 2a. Moderation seam — CURRENTLY A NO-OP ──────────────────────────
    # Called here, on the original bytes, rather than left uncalled: an
    # unreferenced hook is dead code that a later reader has to notice and wire
    # correctly, and the wiring is the part that is easy to get wrong. Placed
    # after decode (so it never sees non-image bytes) and before storage (so a
    # future rejection happens before anything is written and has to be cleaned
    # up). It screens nothing today — see `moderate_image`.
    await moderate_image(raw)

    try:
        # ── 3 + 4. Strip metadata and bound the long edge ────────────────
        processed = _normalise(image)

        # ── 5. Re-encode ─────────────────────────────────────────────────
        payload = _encode_jpeg(processed)
    finally:
        image.close()

    # ── 6. Store under a server-derived key ──────────────────────────────
    key = f"issues/{issue_id}/{uuid.uuid4()}.{OUTPUT_EXTENSION}"
    url = await storage.put(key, payload, OUTPUT_CONTENT_TYPE)

    return StoredImage(
        file_path=key,
        url=url,
        content_type=OUTPUT_CONTENT_TYPE,
        size_bytes=len(payload),
    )


def _decode_and_verify(raw: bytes, filename: str) -> Image.Image:
    """Return an open `Image` only if `raw` really is a supported image.

    `verify()` is what catches truncated and structurally corrupt files that
    `Image.open` — which reads only the header — happily accepts. It also
    leaves the image object unusable, hence the reopen: this is Pillow's
    documented contract, not a workaround.
    """
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = probe.format
            probe.verify()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        # A ZIP named `photo.jpg` lands here: the bytes are inspected, the name
        # is not. The name goes in the message only so the citizen knows which
        # of their five attachments was refused.
        raise _invalid_image(filename) from exc

    if fmt not in _ALLOWED_FORMATS:
        raise _invalid_image(filename, fmt)

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise _invalid_image(filename) from exc
    return image


def _invalid_image(filename: str, detected_format: str | None = None) -> BadRequestError:
    supported = ", ".join(sorted(_ALLOWED_FORMATS))
    return BadRequestError(
        code="INVALID_IMAGE",
        message=f"'{filename}' is not a supported image. Supported formats: {supported}.",
        details={"detected_format": detected_format} if detected_format else {},
    )


def _normalise(image: Image.Image) -> Image.Image:
    """Drop metadata, apply the EXIF orientation, and bound the long edge.

    Order matters: orientation is read from EXIF, so it has to be honoured
    *before* the metadata is discarded, or every portrait phone photo displays
    sideways. `exif_transpose` rotates the pixels so the correct orientation
    survives into a file that carries no EXIF at all.
    """
    oriented = _apply_exif_orientation(image)

    # Convert before resizing: resampling in RGB avoids palette artefacts, and
    # it is the mode JPEG needs anyway.
    #
    # TRANSPARENCY TRADEOFF — PNG and WEBP inputs may have an alpha channel;
    # JPEG has none. Transparent pixels are composited onto white rather than
    # preserved. That is acceptable here because these are civic-issue photos
    # (a pothole, a broken streetlight), never logos or UI assets, and the
    # benefit of a single output format — one code path for stripping, sizing
    # and serving — outweighs alpha we have no use for. If Weft ever accepts
    # images where transparency is meaningful, this is the line to revisit.
    rgb = _to_rgb(oriented)
    if rgb is not oriented:
        oriented.close()

    limit = settings.IMAGE_MAX_DIMENSION
    width, height = rgb.size
    if max(width, height) > limit:
        # `thumbnail` preserves the aspect ratio and, by its own contract,
        # never enlarges — so a 640x480 upload comes back 640x480. It mutates
        # in place, which is fine: `rgb` is ours.
        rgb.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    return rgb


def _apply_exif_orientation(image: Image.Image) -> Image.Image:
    try:
        transposed = ImageOps.exif_transpose(image)
    except Exception:  # pragma: no cover - defensive; malformed EXIF only
        logger.warning("Could not apply the EXIF orientation; using the image as-is.", exc_info=True)
        return image
    # `exif_transpose` returns a copy when it rotates and (on some versions) a
    # copy even when it does not; either way the caller owns exactly one object
    # to close, so normalise to "always a distinct object we may mutate".
    return transposed if transposed is not None else image


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        rgba.close()
        return canvas
    return image.convert("RGB")


def _encode_jpeg(image: Image.Image) -> bytes:
    """Serialise to JPEG from pixel data alone.

    Nothing is passed for `exif`, `icc_profile` or `comment`, and Pillow writes
    none of them unless asked, so the output carries no GPS, no camera serial,
    no capture timestamp — see the module docstring for why that is the point.
    """
    buffer = io.BytesIO()
    image.save(buffer, format=OUTPUT_FORMAT, quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


async def moderate_image(raw: bytes) -> None:
    """DOES NOTHING. Image moderation is NOT implemented.

    Uploaded images are **not** screened — not for nudity, not for violence,
    not for anything. Nothing in Weft inspects the content of a submitted photo
    beyond checking that it decodes as an image.

    This function exists only to mark where AWS Rekognition
    (`DetectModerationLabels`) would be called once it is built, so that the
    call site does not have to be invented later. It is a placeholder, and it
    must not be read as evidence that moderation happens. If you are deciding
    whether Weft screens user-submitted imagery, the answer today is no.
    """
    # Intentionally empty. Do not add "temporary" logic here that makes this
    # look like a working moderation check.
    return None
