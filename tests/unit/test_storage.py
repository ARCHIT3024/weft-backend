"""Behavioural tests for `app/core/storage.py` — the S3 stand-in.

Everything here writes to pytest's `tmp_path`, never to `settings.MEDIA_ROOT`,
so a test run leaves no files in the developer's `var/media`.

The traversal tests are the point of this module. `LocalDiskStorage` is the one
place in the image pipeline that turns a string into a filesystem path, so it
is where a path-traversal bug would live if there were one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.core import storage as storage_module
from app.core.storage import LocalDiskStorage, ObjectStorage, get_storage


@pytest.fixture
def store(tmp_path: Path) -> LocalDiskStorage:
    return LocalDiskStorage(root=tmp_path / "media", base_url="/media")


# ── put ─────────────────────────────────────────────────────────────────


async def test_put_writes_the_bytes_and_returns_the_url(store: LocalDiskStorage) -> None:
    url = await store.put("issues/abc/one.jpg", b"\xff\xd8binary", "image/jpeg")

    assert url == "/media/issues/abc/one.jpg"
    assert (store.root / "issues" / "abc" / "one.jpg").read_bytes() == b"\xff\xd8binary"


async def test_put_creates_missing_parent_directories(store: LocalDiskStorage) -> None:
    assert not store.root.exists()

    await store.put("a/b/c/d.jpg", b"x", "image/jpeg")

    assert (store.root / "a" / "b" / "c" / "d.jpg").is_file()


async def test_put_overwrites_an_existing_key(store: LocalDiskStorage) -> None:
    await store.put("k.jpg", b"first", "image/jpeg")
    await store.put("k.jpg", b"second", "image/jpeg")

    assert (store.root / "k.jpg").read_bytes() == b"second"


async def test_put_leaves_no_temporary_files_behind(store: LocalDiskStorage) -> None:
    """The write-then-rename must not litter the media root."""
    await store.put("issues/abc/one.jpg", b"data", "image/jpeg")

    names = [p.name for p in (store.root / "issues" / "abc").iterdir()]
    assert names == ["one.jpg"]


async def test_base_url_trailing_slash_does_not_double_up(tmp_path: Path) -> None:
    store = LocalDiskStorage(root=tmp_path, base_url="https://cdn.example.com/")

    url = await store.put("x.jpg", b"x", "image/jpeg")

    assert url == "https://cdn.example.com/x.jpg"


# ── delete ──────────────────────────────────────────────────────────────


async def test_delete_removes_the_file(store: LocalDiskStorage) -> None:
    await store.put("gone.jpg", b"x", "image/jpeg")
    assert (store.root / "gone.jpg").exists()

    await store.delete("gone.jpg")

    assert not (store.root / "gone.jpg").exists()


async def test_delete_of_a_missing_key_does_not_raise(store: LocalDiskStorage) -> None:
    """Deleting an absent key is a no-op, not an error.

    Cleanup paths (a rolled-back issue creation, a retried delete) call this
    without knowing whether the object made it to disk; making absence an error
    would mean every caller wrapping it in a try/except.
    """
    await store.delete("never/existed.jpg")
    await store.delete("also-never-existed.jpg")


async def test_delete_after_delete_is_still_a_no_op(store: LocalDiskStorage) -> None:
    await store.put("twice.jpg", b"x", "image/jpeg")

    await store.delete("twice.jpg")
    await store.delete("twice.jpg")


# ── Path traversal ──────────────────────────────────────────────────────


HOSTILE_KEYS = [
    "../escaped.jpg",
    "../../../../etc/passwd",
    "issues/../../escaped.jpg",
    "/absolute.jpg",
    "/etc/passwd",
    "C:\\Windows\\System32\\x.jpg",
    "..\\..\\escaped.jpg",
    "issues/..\\escaped.jpg",
    "",
    "  ",
    "a/../../b.jpg",
    "./../../b.jpg",
    "with\x00null.jpg",
]


@pytest.mark.parametrize("key", HOSTILE_KEYS)
async def test_put_rejects_keys_that_could_escape_the_root(store: LocalDiskStorage, key: str) -> None:
    with pytest.raises(ValueError):
        await store.put(key, b"pwned", "image/jpeg")


@pytest.mark.parametrize("key", HOSTILE_KEYS)
async def test_delete_rejects_keys_that_could_escape_the_root(store: LocalDiskStorage, key: str) -> None:
    with pytest.raises(ValueError):
        await store.delete(key)


async def test_no_hostile_key_writes_anything_outside_the_root(tmp_path: Path) -> None:
    """The decisive assertion: after every hostile key, the sandbox is clean.

    `tmp_path` contains the media root and nothing else, so any file appearing
    beside it is an escape.
    """
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LocalDiskStorage(root=root, base_url="/media")

    for key in HOSTILE_KEYS:
        with pytest.raises(ValueError):
            await store.put(key, b"pwned", "image/jpeg")

    assert list(outside.iterdir()) == []
    assert not root.exists() or list(root.rglob("*")) == []


async def test_a_key_that_merely_contains_dots_is_fine(store: LocalDiskStorage) -> None:
    """Rejecting `..` must not reject legitimate dotted names."""
    url = await store.put("issues/a..b/my.photo.v2.jpg", b"x", "image/jpeg")

    assert url == "/media/issues/a..b/my.photo.v2.jpg"


# ── get_storage ─────────────────────────────────────────────────────────


def test_get_storage_returns_one_shared_instance() -> None:
    storage_module._build_storage.cache_clear()
    try:
        assert get_storage() is get_storage()
    finally:
        storage_module._build_storage.cache_clear()


def test_get_storage_is_built_from_settings() -> None:
    storage_module._build_storage.cache_clear()
    try:
        built = get_storage()
        assert isinstance(built, LocalDiskStorage)
        assert built.root == Path(settings.MEDIA_ROOT).resolve()
        assert built.base_url == settings.MEDIA_BASE_URL.rstrip("/")
    finally:
        storage_module._build_storage.cache_clear()


def test_local_disk_storage_satisfies_the_protocol(store: LocalDiskStorage) -> None:
    """The seam an eventual `S3Storage` has to fit through."""
    assert isinstance(store, ObjectStorage)
