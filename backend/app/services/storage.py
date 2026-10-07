"""File storage.

Uploaded files are written under a server-generated UUID name inside a fixed
storage directory that is deliberately *outside* any static/web root. The
original filename is kept only as sanitized database metadata and is never used
to build a path. Uploaded files are never executed or imported.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

CHUNK_SIZE = 64 * 1024
PROBE_SIZE = 4096


class StorageError(RuntimeError):
    """Raised when a file cannot be stored safely."""


@dataclass(frozen=True)
class StoredFile:
    stored_filename: str
    path: Path
    size: int
    sha256: str
    probe: bytes


def ensure_within(directory: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and confirm it stays inside ``directory``."""
    base = directory.resolve()
    target = candidate.resolve()
    if base != target and base not in target.parents:
        raise StorageError("Refusing to write outside the storage directory.")
    return target


def build_stored_path(upload_dir: Path, extension: str) -> tuple[str, Path]:
    """Return ``(stored_filename, absolute_path)`` for a new upload."""
    if not extension.startswith(".") or not extension[1:].isalnum():
        raise StorageError("Invalid file extension.")
    stored_filename = f"{uuid.uuid4().hex}{extension.lower()}"
    path = ensure_within(upload_dir, upload_dir / stored_filename)
    return stored_filename, path


def save_stream(
    stream: BinaryIO,
    upload_dir: Path,
    extension: str,
    max_bytes: int,
) -> StoredFile:
    """Stream ``stream`` to disk, aborting as soon as the size cap is exceeded."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_filename, path = build_stored_path(upload_dir, extension)
    digest = hashlib.sha256()
    total = 0
    probe = b""
    try:
        with open(path, "wb") as handle:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):  # pragma: no cover - defensive
                    chunk = bytes(chunk)
                total += len(chunk)
                if total > max_bytes:
                    handle.close()
                    _safe_unlink(path)
                    raise StorageError("upload exceeds the maximum allowed size")
                if len(probe) < PROBE_SIZE:
                    probe += chunk[: PROBE_SIZE - len(probe)]
                digest.update(chunk)
                handle.write(chunk)
    except StorageError:
        raise
    except OSError as exc:
        _safe_unlink(path)
        raise StorageError(f"could not write uploaded file: {type(exc).__name__}") from exc

    if total == 0:
        _safe_unlink(path)
        raise StorageError("empty upload")

    return StoredFile(
        stored_filename=stored_filename,
        path=path,
        size=total,
        sha256=digest.hexdigest(),
        probe=probe,
    )


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            os.remove(path)
    except OSError:  # pragma: no cover - best effort cleanup
        pass


def delete_stored_file(upload_dir: Path, stored_filename: str) -> None:
    """Remove a stored file, refusing to escape the storage directory."""
    if not stored_filename or "/" in stored_filename or "\\" in stored_filename:
        return
    try:
        path = ensure_within(upload_dir, upload_dir / stored_filename)
    except StorageError:  # pragma: no cover - defensive
        return
    _safe_unlink(path)


def read_stored_file(upload_dir: Path, stored_filename: str) -> Path:
    """Return the path of a stored file, verifying containment."""
    if not stored_filename or "/" in stored_filename or "\\" in stored_filename:
        raise StorageError("Invalid stored file name.")
    path = ensure_within(upload_dir, upload_dir / stored_filename)
    if not path.is_file():
        raise StorageError("Stored file not found.")
    return path
