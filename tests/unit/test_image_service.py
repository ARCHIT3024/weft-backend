"""Behavioural tests for `app/services/image_service.py`.

Every fixture image is generated in-memory by Pillow, so the suite carries no
binary assets and the tests state exactly what is in each input. Storage goes
to `tmp_path` through a real `LocalDiskStorage`, not a mock: the round-trip —
bytes in, file on disk, URL out — is most of what this module promises.

The EXIF tests are the ones to keep. A civic report carries a location the
citizen chose; the photo must not carry a second, more precise one they did
not.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.config import settings
from app.core.exceptions import BadRequestError
from app.core.storage import LocalDiskStorage
from app.services.image_service import (
    OUTPUT_CONTENT_TYPE,
    StoredImage,
    moderate_image,
    store_issue_image,
)

ISSUE_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")

# The coordinates a phone would embed. Chosen to be unmistakable if they leak.
GPS_LATITUDE = (28.0, 36.0, 47.0)
GPS_LONGITUDE = (77.0, 12.0, 30.0)


# ── Image builders ──────────────────────────────────────────────────────


def make_image(
    fmt: str = "JPEG",
    size: tuple[int, int] = (800, 600),
    mode: str = "RGB",
    color: str | tuple[int, ...] = "red",
    exif: Image.Exif | None = None,
) -> bytes:
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    if exif is not None:
        image.save(buffer, format=fmt, exif=exif)
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def make_gps_exif(orientation: int | None = None) -> Image.Exif:
    """EXIF as a phone writes it: camera model plus a GPS fix."""
    exif = Image.Exif()
    exif[0x010F] = "WeftTestPhone"  # Make
    exif[0x0110] = "WeftTestModel"  # Model
    if orientation is not None:
        exif[0x0112] = orientation  # Orientation
    gps = exif.get_ifd(0x8825)
    gps[0] = b"\x02\x03\x00\x00"  # GPSVersionID
    gps[1] = "N"
    gps[2] = GPS_LATITUDE
    gps[3] = "E"
    gps[4] = GPS_LONGITUDE
    return exif


def make_zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("payload.txt", "this is not an image")
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> LocalDiskStorage:
    return LocalDiskStorage(root=tmp_path / "media", base_url="/media")


def stored_bytes(store: LocalDiskStorage, result: StoredImage) -> bytes:
    return (store.root / result.file_path).read_bytes()


# ── Happy path ──────────────────────────────────────────────────────────


async def test_round_trip_stores_a_readable_jpeg(store: LocalDiskStorage) -> None:
    result = await store_issue_image(
        issue_id=ISSUE_ID,
        filename="pothole.jpg",
        raw=make_image(),
        storage=store,
    )

    assert result.content_type == OUTPUT_CONTENT_TYPE
    assert result.url == f"/media/{result.file_path}"

    on_disk = stored_bytes(store, result)
    assert result.size_bytes == len(on_disk)

    reopened = Image.open(io.BytesIO(on_disk))
    assert reopened.format == "JPEG"
    assert reopened.size == (800, 600)


async def test_file_path_is_derived_from_the_issue_id_and_a_fresh_uuid(store: LocalDiskStorage) -> None:
    result = await store_issue_image(issue_id=ISSUE_ID, filename="a.jpg", raw=make_image(), storage=store)

    prefix, issue_part, name = result.file_path.split("/")
    assert prefix == "issues"
    assert issue_part == str(ISSUE_ID)
    stem, extension = name.split(".")
    assert extension == "jpg"
    uuid.UUID(stem)  # raises if it is not a UUID


async def test_two_uploads_of_identical_bytes_get_distinct_paths(store: LocalDiskStorage) -> None:
    raw = make_image()
    first = await store_issue_image(issue_id=ISSUE_ID, filename="a.jpg", raw=raw, storage=store)
    second = await store_issue_image(issue_id=ISSUE_ID, filename="a.jpg", raw=raw, storage=store)

    assert first.file_path != second.file_path
    assert (store.root / first.file_path).exists()
    assert (store.root / second.file_path).exists()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
async def test_accepted_input_formats_all_normalise_to_jpeg(store: LocalDiskStorage, fmt: str) -> None:
    result = await store_issue_image(
        issue_id=ISSUE_ID, filename=f"photo.{fmt.lower()}", raw=make_image(fmt=fmt), storage=store
    )

    assert Image.open(io.BytesIO(stored_bytes(store, result))).format == "JPEG"


async def test_defaults_to_the_process_wide_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting `storage` must fall through to `get_storage()`."""
    captured: dict[str, object] = {}

    class RecordingStorage:
        async def put(self, key: str, data: bytes, content_type: str) -> str:
            captured["key"] = key
            captured["content_type"] = content_type
            return f"https://cdn.example.com/{key}"

        async def delete(self, key: str) -> None:  # pragma: no cover - unused
            raise AssertionError("delete should not be called")

    monkeypatch.setattr("app.services.image_service.get_storage", lambda: RecordingStorage())

    result = await store_issue_image(issue_id=ISSUE_ID, filename="a.jpg", raw=make_image())

    assert captured["key"] == result.file_path
    assert captured["content_type"] == OUTPUT_CONTENT_TYPE
    assert result.url == f"https://cdn.example.com/{result.file_path}"


# ── Size limit ──────────────────────────────────────────────────────────


async def test_oversize_upload_is_rejected(store: LocalDiskStorage) -> None:
    oversize = b"\xff" * (settings.MAX_IMAGE_BYTES + 1)

    with pytest.raises(BadRequestError) as excinfo:
        await store_issue_image(issue_id=ISSUE_ID, filename="huge.jpg", raw=oversize, storage=store)

    assert excinfo.value.code == "IMAGE_TOO_LARGE"
    assert excinfo.value.status_code == 400


async def test_the_size_check_runs_before_any_decoding(store: LocalDiskStorage) -> None:
    """Oversize garbage must fail on length, not on being undecodable.

    The distinction matters: if decoding came first, an attacker would choose
    how much work each rejected request costs. The assertion is that the *code*
    is IMAGE_TOO_LARGE — bytes that are not an image at all still fail the
    cheap check first.
    """
    with pytest.raises(BadRequestError) as excinfo:
        await store_issue_image(
            issue_id=ISSUE_ID,
            filename="huge.jpg",
            raw=b"not an image at all" * settings.MAX_IMAGE_BYTES,
            storage=store,
        )

    assert excinfo.value.code == "IMAGE_TOO_LARGE"


async def test_an_image_exactly_at_the_limit_is_accepted(store: LocalDiskStorage) -> None:
    raw = make_image()
    padded = raw + b"\x00" * (settings.MAX_IMAGE_BYTES - len(raw))
    assert len(padded) == settings.MAX_IMAGE_BYTES

    result = await store_issue_image(issue_id=ISSUE_ID, filename="edge.jpg", raw=padded, storage=store)

    assert result.size_bytes > 0


async def test_a_rejected_upload_writes_nothing(store: LocalDiskStorage) -> None:
    with pytest.raises(BadRequestError):
        await store_issue_image(issue_id=ISSUE_ID, filename="bad.jpg", raw=b"definitely not an image", storage=store)

    assert not store.root.exists() or list(store.root.rglob("*.jpg")) == []


# ── Content sniffing ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"definitely not an image", id="plain-text"),
        pytest.param(b"\x00\x01\x02\x03\x04\x05\x06\x07", id="random-bytes"),
        pytest.param(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n", id="pdf-header"),
        pytest.param(b"GIF89a" + b"\x00" * 32, id="truncated-gif"),
    ],
)
async def test_non_image_bytes_are_rejected(store: LocalDiskStorage, raw: bytes) -> None:
    with pytest.raises(BadRequestError) as excinfo:
        await store_issue_image(issue_id=ISSUE_ID, filename="x.jpg", raw=raw, storage=store)

    assert excinfo.value.code == "INVALID_IMAGE"


async def test_a_zip_named_jpg_is_rejected(store: LocalDiskStorage) -> None:
    """Validation reads the bytes, not the name or the Content-Type."""
    with pytest.raises(BadRequestError) as excinfo:
        await store_issue_image(issue_id=ISSUE_ID, filename="totally_a_photo.jpg", raw=make_zip_bytes(), storage=store)

    assert excinfo.value.code == "INVALID_IMAGE"


async def test_a_truncated_jpeg_is_rejected(store: LocalDiskStorage) -> None:
    """`Image.open` reads only the header; `verify()` is what catches this."""
    raw = make_image()

    with pytest.raises(BadRequestError) as excinfo:
        await store_issue_image(issue_id=ISSUE_ID, filename="cut.jpg", raw=raw[: len(raw) // 2], storage=store)

    assert excinfo.value.code == "INVALID_IMAGE"


async def test_an_unsupported_but_valid_image_format_is_rejected(store: LocalDiskStorage) -> None:
    """A real GIF is a real image, and still not on the accept list."""
    with pytest.raises(BadRequestError) as excinfo:
        await store_issue_image(
            issue_id=ISSUE_ID, filename="animation.gif", raw=make_image(fmt="GIF", mode="P"), storage=store
        )

    assert excinfo.value.code == "INVALID_IMAGE"


async def test_heic_is_unsupported_when_pillow_heif_is_absent() -> None:
    """HEIC support is conditional and must degrade, never crash on import.

    `pillow-heif` is not installed in this environment, so the module has to
    import anyway and simply not list HEIF among the accepted formats.
    """
    from app.services import image_service

    if image_service.HEIF_SUPPORTED:
        pytest.skip("pillow-heif is installed here; the absent-dependency path cannot be exercised")

    assert "HEIF" not in image_service._ALLOWED_FORMATS
    assert {"JPEG", "PNG", "WEBP"} == image_service._ALLOWED_FORMATS


# ── EXIF stripping (privacy) ────────────────────────────────────────────


async def test_gps_exif_on_input_is_absent_from_the_output(store: LocalDiskStorage) -> None:
    """The privacy guarantee: a home address must not ride along in the file.

    A citizen who pins an issue a street away from where they live, but attaches
    a photo taken at home, would otherwise publish their exact front door.
    """
    raw = make_image(exif=make_gps_exif())

    # Sanity: the input really does carry the coordinates we are checking for.
    source_gps = Image.open(io.BytesIO(raw)).getexif().get_ifd(0x8825)
    assert source_gps[2] == GPS_LATITUDE
    assert source_gps[4] == GPS_LONGITUDE

    result = await store_issue_image(issue_id=ISSUE_ID, filename="home.jpg", raw=raw, storage=store)

    output = stored_bytes(store, result)
    exif = Image.open(io.BytesIO(output)).getexif()
    assert dict(exif) == {}
    assert dict(exif.get_ifd(0x8825)) == {}


async def test_no_exif_survives_at_the_byte_level(store: LocalDiskStorage) -> None:
    """Not just "Pillow cannot see it" — the bytes are not in the file."""
    raw = make_image(exif=make_gps_exif())

    result = await store_issue_image(issue_id=ISSUE_ID, filename="home.jpg", raw=raw, storage=store)

    output = stored_bytes(store, result)
    assert b"Exif" not in output
    assert b"WeftTestPhone" not in output
    assert b"WeftTestModel" not in output


async def test_exif_orientation_is_baked_into_the_pixels_before_stripping(store: LocalDiskStorage) -> None:
    """Dropping EXIF must not leave every portrait phone photo sideways.

    Orientation 6 means "rotate 90° CW to display". The tag is discarded, so
    the rotation has to be applied to the pixels: a 800x600 input with that tag
    must come back 600x800.
    """
    raw = make_image(size=(800, 600), exif=make_gps_exif(orientation=6))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="portrait.jpg", raw=raw, storage=store)

    assert Image.open(io.BytesIO(stored_bytes(store, result))).size == (600, 800)


# ── Resizing ────────────────────────────────────────────────────────────


async def test_a_large_image_is_scaled_down_to_the_long_edge_limit(store: LocalDiskStorage) -> None:
    limit = settings.IMAGE_MAX_DIMENSION
    raw = make_image(size=(limit * 2, limit))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="big.jpg", raw=raw, storage=store)

    width, height = Image.open(io.BytesIO(stored_bytes(store, result))).size
    assert max(width, height) == limit
    assert (width, height) == (limit, limit // 2)  # 2:1 aspect ratio preserved


async def test_a_tall_image_is_scaled_by_its_long_edge_too(store: LocalDiskStorage) -> None:
    limit = settings.IMAGE_MAX_DIMENSION
    raw = make_image(size=(limit // 2, limit * 2))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="tall.jpg", raw=raw, storage=store)

    width, height = Image.open(io.BytesIO(stored_bytes(store, result))).size
    assert height == limit
    assert width == limit // 4


async def test_a_small_image_is_never_upscaled(store: LocalDiskStorage) -> None:
    raw = make_image(size=(320, 240))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="small.jpg", raw=raw, storage=store)

    assert Image.open(io.BytesIO(stored_bytes(store, result))).size == (320, 240)


async def test_an_image_exactly_at_the_limit_is_left_alone(store: LocalDiskStorage) -> None:
    limit = settings.IMAGE_MAX_DIMENSION
    raw = make_image(size=(limit, 400))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="exact.jpg", raw=raw, storage=store)

    assert Image.open(io.BytesIO(stored_bytes(store, result))).size == (limit, 400)


# ── Transparency ────────────────────────────────────────────────────────


async def test_a_transparent_png_is_flattened_onto_white(store: LocalDiskStorage) -> None:
    """The documented tradeoff: JPEG has no alpha, so transparency is lost.

    Pinned as a test so the behaviour is a decision, not a surprise.
    """
    raw = make_image(fmt="PNG", mode="RGBA", color=(0, 0, 0, 0), size=(64, 64))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="logo.png", raw=raw, storage=store)

    output = Image.open(io.BytesIO(stored_bytes(store, result)))
    assert output.mode == "RGB"
    assert output.getpixel((32, 32)) == (255, 255, 255)


async def test_a_greyscale_image_is_converted_to_rgb(store: LocalDiskStorage) -> None:
    raw = make_image(fmt="PNG", mode="L", color=128, size=(64, 64))

    result = await store_issue_image(issue_id=ISSUE_ID, filename="grey.png", raw=raw, storage=store)

    assert Image.open(io.BytesIO(stored_bytes(store, result))).mode == "RGB"


# ── Hostile filenames ───────────────────────────────────────────────────


HOSTILE_FILENAMES = [
    "../../../../etc/passwd",
    "..\\..\\..\\Windows\\System32\\evil.jpg",
    "C:\\Windows\\x.jpg",
    "/etc/cron.d/evil.jpg",
    "....//....//escape.jpg",
    "a\x00.jpg",
    "issues/../../../escape.jpg",
    "%2e%2e%2f%2e%2e%2fescape.jpg",
    "",
    "." * 300 + ".jpg",
]


@pytest.mark.parametrize("filename", HOSTILE_FILENAMES)
async def test_a_hostile_filename_cannot_escape_the_media_root(
    tmp_path: Path, store: LocalDiskStorage, filename: str
) -> None:
    """The filename is not sanitised — it is not used at all.

    The stored key is built from the issue id and a fresh UUID, so there is no
    string from the client anywhere in the path. This asserts the consequence:
    whatever the caller sends, the file lands under `issues/{issue_id}/` and
    nothing appears beside the media root.
    """
    sentinel = tmp_path / "outside"
    sentinel.mkdir()

    result = await store_issue_image(issue_id=ISSUE_ID, filename=filename, raw=make_image(size=(32, 32)), storage=store)

    assert result.file_path.startswith(f"issues/{ISSUE_ID}/")
    written = (store.root / result.file_path).resolve()
    assert written.is_file()
    assert store.root in written.parents
    assert list(sentinel.iterdir()) == []


@pytest.mark.parametrize("filename", HOSTILE_FILENAMES)
async def test_no_part_of_the_filename_reaches_the_stored_path(store: LocalDiskStorage, filename: str) -> None:
    result = await store_issue_image(issue_id=ISSUE_ID, filename=filename, raw=make_image(size=(32, 32)), storage=store)

    stem = result.file_path.rsplit("/", 1)[-1].removesuffix(".jpg")
    uuid.UUID(stem)
    assert "passwd" not in result.file_path
    assert ".." not in result.file_path


# ── Moderation stub ─────────────────────────────────────────────────────


async def test_moderate_image_is_a_no_op_and_screens_nothing() -> None:
    """Guards the seam, and guards against it quietly becoming a real check.

    If someone implements moderation, this test should be replaced, not
    deleted — the thing that must never happen is code that *looks* like
    moderation while approving everything.
    """
    assert await moderate_image(b"anything at all") is None
    assert await moderate_image(b"") is None
    assert "NOT implemented" in (moderate_image.__doc__ or "")
