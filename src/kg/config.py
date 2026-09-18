from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from kg.models.manifest import CorpusManifest


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class SourceSelection:
    paths: list[Path]
    missing: list[str]


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
        matches = list(_safe_glob(root, pattern))
        markdown_files = [path for path in matches if path.is_file() and path.suffix.lower() == ".md"]
        if not markdown_files:
            missing.append(pattern)
            continue
        selected.update(markdown_files)

    return SourceSelection(paths=sorted(selected), missing=missing)


def relative_source_path(manifest: CorpusManifest, path: Path) -> str:
    return path.resolve().relative_to(manifest.vault_root).as_posix()


def _safe_glob(root: Path, pattern: str) -> Iterable[Path]:
    for path in root.glob(pattern):
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ManifestError(f"include entry escapes vault_root: {pattern}") from exc
        yield resolved
