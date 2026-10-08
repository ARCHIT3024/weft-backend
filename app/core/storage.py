"""Object storage abstraction — a local-disk stand-in for S3.

Weft has no AWS credentials yet, so uploaded images live on the API host's
filesystem under `settings.MEDIA_ROOT` and are served by the static mount that
`app/main.py` attaches at `settings.MEDIA_BASE_URL`.  That is a development /
single-host arrangement, not the production plan.

The seam that makes the eventual move cheap is `ObjectStorage`: two async
methods, keyed by an opaque string, returning a fetchable URL.  An `S3Storage`
implementing the same Protocol (`put_object` + `delete_object`, URL built from
`settings.CLOUDFRONT_DOMAIN`) drops in at `get_storage()` and nothing above it
changes — `app/services/image_service.py` never touches a `Path`.

Both methods are `async` for that reason alone.  Local disk I/O is blocking, so
it is pushed to a worker thread rather than run inline; that keeps the event
loop free *and* keeps the signature honest for the network-backed
implementation that replaces it.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol, runtime_checkable

from app.config import settings


@runtime_checkable
class ObjectStorage(Protocol):
    """A minimal blob store: put bytes at a key, delete a key."""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store `data` at `key`. Returns the publicly fetchable URL."""
        ...

    async def delete(self, key: str) -> None:
        """Remove `key`. A missing key is NOT an error."""
        ...


def _validate_key(key: str) -> PurePosixPath:
    """Reject any key that could address something outside the storage root.

    Callers are expected to build keys from server-controlled values (see
    `image_service.store_issue_image`, which derives them from an issue id and
    a fresh UUID), so this should never fire.  It exists because "no user input
    reaches here" is an invariant of *today's* callers, and a traversal bug is
    not the failure mode you want to discover from the outside.

    A `ValueError` rather than a `BadRequestError`: a bad key is a programming
    error in this codebase, not something a client did.
    """
    if not key or key.strip() != key:
        raise ValueError(f"Invalid storage key: {key!r}")

    # Backslashes are path separators on Windows but ordinary characters to
    # PurePosixPath, so `..\\..\\etc` would sail through the parts check below
    # and then traverse once handed to the OS. Refuse them outright.
    if "\\" in key or "\x00" in key:
        raise ValueError(f"Invalid storage key: {key!r}")

    pure = PurePosixPath(key)
    if pure.is_absolute() or pure.drive or any(part in ("..", "") for part in pure.parts):
        raise ValueError(f"Invalid storage key: {key!r}")
    return pure


class LocalDiskStorage:
    """`ObjectStorage` backed by a directory tree on the local filesystem.

    `root` is created lazily on first write, so constructing one in a test with
    a `tmp_path` that does not exist yet is fine.
    """

    def __init__(self, root: str | Path, base_url: str) -> None:
        self._root = Path(root).resolve()
        # Trailing slashes would double up when joined with a key.
        self._base_url = base_url.rstrip("/")

    @property
    def root(self) -> Path:
        return self._root

    @property
    def base_url(self) -> str:
        return self._base_url

    def _path_for(self, key: str) -> Path:
        """Resolve `key` to an absolute path, proving it stays under the root.

        Belt and braces over `_validate_key`: that check is syntactic, this one
        is the real containment test after symlinks and `..` are resolved away.
        """
        relative = _validate_key(key)
        candidate = (self._root / relative).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ValueError(f"Storage key escapes the media root: {key!r}")
        return candidate

    def url_for(self, key: str) -> str:
        """The client-fetchable URL a stored `key` is served at."""
        return f"{self._base_url}/{_validate_key(key).as_posix()}"

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store `data` at `key`. Returns the publicly fetchable URL.

        `content_type` is unused here — a filesystem has nowhere to record it,
        and the static mount infers it from the extension.  It stays in the
        signature because S3 needs it at write time, and adding a parameter
        later would mean touching every caller.
        """
        path = self._path_for(key)
        await asyncio.to_thread(self._write_sync, path, data)
        return self.url_for(key)

    @staticmethod
    def _write_sync(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a concurrent reader (the static mount) never
        # sees a half-written image. The temp file is a sibling, so the rename
        # stays on one filesystem and is atomic.
        temp = path.with_name(f".{path.name}.tmp")
        try:
            temp.write_bytes(data)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)

    async def delete(self, key: str) -> None:
        """Remove `key`. A missing key is NOT an error."""
        path = self._path_for(key)
        await asyncio.to_thread(path.unlink, missing_ok=True)


@lru_cache(maxsize=1)
def _build_storage() -> LocalDiskStorage:
    return LocalDiskStorage(root=settings.MEDIA_ROOT, base_url=settings.MEDIA_BASE_URL)


def get_storage() -> ObjectStorage:
    """Process-wide storage instance built from settings.

    Cached, so every caller shares one object; tests that need a different root
    either pass a `LocalDiskStorage` explicitly (every function that uses
    storage takes it as an argument) or call `_build_storage.cache_clear()`.
    """
    return _build_storage()
