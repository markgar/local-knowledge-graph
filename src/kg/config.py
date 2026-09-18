from __future__ import annotations

import fcntl
import os
import stat
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from kg.models.manifest import CorpusManifest


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSelection:
    paths: list[Path]
    missing: list[str]


@dataclass(frozen=True)
class SourceContent:
    content: bytes
    observed_mtime: str


def load_manifest(path: Path) -> CorpusManifest:
    manifest_path = path.resolve()
    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"Could not read manifest {manifest_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"Invalid YAML in {manifest_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"Manifest {manifest_path} must contain a mapping")

    manifest = CorpusManifest.model_validate(raw)
    base = manifest_path.parent
    manifest.vault_root = (base / manifest.vault_root).resolve()
    manifest.database = (base / manifest.database).resolve()
    manifest.manifest_path = manifest_path
    return manifest


def select_sources(manifest: CorpusManifest) -> SourceSelection:
    root = manifest.vault_root
    if not root.is_dir():
        raise ManifestError(f"vault_root is not a directory: {root}")

    selected: set[Path] = set()
    missing: list[str] = []
    for pattern in manifest.include:
        matches = list(_safe_glob(root, pattern, manifest.allow_symlinks))
        markdown_files = [
            path
            for path in matches
            if path.is_file() and path.suffix.lower() == ".md"
        ]
        if not markdown_files:
            missing.append(pattern)
            continue
        selected.update(markdown_files)

    return SourceSelection(paths=sorted(selected), missing=missing)


def relative_source_path(manifest: CorpusManifest, path: Path) -> str:
    return path.relative_to(manifest.vault_root).as_posix()


def read_source(manifest: CorpusManifest, path: Path) -> SourceContent:
    relative = path.relative_to(manifest.vault_root)
    descriptor = _open_source(
        manifest.vault_root,
        relative,
        manifest.allow_symlinks,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ManifestError(f"source is not a regular file: {path}")
        if before.st_size > manifest.max_source_bytes:
            raise ManifestError(
                "source exceeds max_source_bytes "
                f"({before.st_size} > {manifest.max_source_bytes})"
            )
        content = bytearray()
        while len(content) <= manifest.max_source_bytes:
            chunk = os.read(
                descriptor,
                min(64 * 1024, manifest.max_source_bytes + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) > manifest.max_source_bytes:
            raise ManifestError(
                f"source exceeds max_source_bytes ({len(content)} > "
                f"{manifest.max_source_bytes})"
            )
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if identity_before != identity_after or len(content) != after.st_size:
            raise ManifestError(f"source changed while being read: {path}")
        return SourceContent(
            content=bytes(content),
            observed_mtime=datetime.fromtimestamp(after.st_mtime, UTC).isoformat(),
        )
    finally:
        os.close(descriptor)


def _safe_glob(
    root: Path,
    pattern: str,
    allow_symlinks: bool,
) -> Iterable[Path]:
    for path in root.glob(pattern):
        if not allow_symlinks and _has_symlink_component(root, path):
            raise ManifestError(f"symlink sources are disabled: {path}")
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ManifestError(f"include entry escapes vault_root: {pattern}") from exc
        yield path


def _has_symlink_component(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def _open_source(root: Path, relative: Path, allow_symlinks: bool) -> int:
    if allow_symlinks:
        descriptor = os.open(root / relative, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            resolved = _opened_path(descriptor)
            resolved.relative_to(root)
        except ManifestError:
            os.close(descriptor)
            raise
        except (OSError, ValueError) as exc:
            os.close(descriptor)
            raise ManifestError(f"source escapes vault_root: {root / relative}") from exc
        return descriptor

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | nofollow
    )
    directory = os.open(root, directory_flags)
    try:
        parts = relative.parts
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        try:
            return os.open(
                parts[-1],
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
                dir_fd=directory,
            )
        except OSError as exc:
            raise ManifestError(f"could not securely open source {root / relative}: {exc}") from exc
    finally:
        os.close(directory)


def _opened_path(descriptor: int) -> Path:
    if sys.platform == "darwin":
        try:
            raw_path = fcntl.fcntl(
                descriptor,
                getattr(fcntl, "F_GETPATH", 50),
                b"\0" * 1024,
            )
        except OSError as exc:
            raise ManifestError(
                "could not determine securely opened source path with F_GETPATH"
            ) from exc
        path_text = raw_path.split(b"\0", 1)[0].decode()
        if path_text:
            return Path(os.path.normpath(path_text))

    proc_path = Path("/proc/self/fd") / str(descriptor)
    try:
        path_text = os.readlink(proc_path)
    except OSError as exc:
        raise ManifestError(
            "could not determine securely opened source path; "
            "F_GETPATH or procfs descriptor links are required"
        ) from exc
    if not os.path.isabs(path_text) or path_text.endswith(" (deleted)"):
        raise ManifestError("securely opened source has no stable absolute path")
    return Path(os.path.normpath(path_text))
