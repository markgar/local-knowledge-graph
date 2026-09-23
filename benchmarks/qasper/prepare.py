from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, cast

ARCHIVE_URL = (
    "https://qasper-dataset.s3.us-west-2.amazonaws.com/"
    "qasper-train-dev-v0.3.tgz"
)
ARCHIVE_SHA256 = "a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a"
DEV_MEMBER = "qasper-dev-v0.3.json"
MARKER_NAME = ".qasper-fixture.json"


def _ensure_directory(path: Path) -> None:
    missing = []
    candidate = path
    while True:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            missing.append(candidate)
            parent = candidate.parent
            if parent == candidate:
                raise ValueError(f"Could not find an existing parent for {path}") from None
            candidate = parent
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"Refusing to use symlinked directory: {candidate}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Expected a directory: {candidate}")
        break

    for candidate in reversed(missing):
        candidate.mkdir()
        metadata = candidate.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"Refusing to use unsafe directory: {candidate}")


def _validate_existing_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Refusing to overwrite non-regular file: {path}")


def _atomic_write(path: Path, content: str | bytes) -> None:
    _ensure_directory(path.parent)
    _validate_existing_regular_file(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        if isinstance(content, str):
            text_stream = os.fdopen(descriptor, "w", encoding="utf-8")
            descriptor = -1
            with text_stream:
                text_stream.write(content)
                text_stream.flush()
                os.fsync(text_stream.fileno())
        else:
            binary_stream = os.fdopen(descriptor, "wb")
            descriptor = -1
            with binary_stream:
                binary_stream.write(content)
                binary_stream.flush()
                os.fsync(binary_stream.fileno())
        _validate_existing_regular_file(path)
        os.replace(temporary, path)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    _validate_existing_regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_archive(destination: Path) -> Path:
    _ensure_directory(destination.parent)
    _validate_existing_regular_file(destination)
    if destination.exists() and _sha256(destination) == ARCHIVE_SHA256:
        return destination

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        digest = hashlib.sha256()
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            with urllib.request.urlopen(ARCHIVE_URL) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        actual_hash = digest.hexdigest()
        if actual_hash != ARCHIVE_SHA256:
            raise ValueError(
                f"QASPER archive checksum mismatch: expected {ARCHIVE_SHA256}, "
                f"received {actual_hash}"
            )
        _validate_existing_regular_file(destination)
        os.replace(temporary, destination)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return destination


def read_development_split(archive: Path) -> dict[str, dict[str, Any]]:
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember(DEV_MEMBER)
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError(f"{DEV_MEMBER} is missing from {archive}")
        payload: object = json.load(stream)
        if not isinstance(payload, dict) or not all(
            isinstance(paper_id, str) and isinstance(paper, dict)
            for paper_id, paper in payload.items()
        ):
            raise ValueError(f"{DEV_MEMBER} has an invalid top-level structure")
        return cast(dict[str, dict[str, Any]], payload)


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def render_paper(paper_id: str, paper: dict[str, Any]) -> str:
    title = normalize_text(paper["title"])
    lines = [
        "---",
        f"qasper_paper_id: {json.dumps(paper_id)}",
        f"title: {json.dumps(title)}",
        'dataset: "QASPER 0.3 development split"',
        f"source_url: {json.dumps(f'https://arxiv.org/abs/{paper_id}')}",
        "---",
        "",
        f"# {title}",
        "",
        "## Abstract",
        "",
        normalize_text(paper["abstract"]),
        "",
    ]
    for section in paper["full_text"]:
        section_name = normalize_text(section["section_name"]) or "Body"
        lines.extend((f"## {section_name}", ""))
        for paragraph in section["paragraphs"]:
            text = normalize_text(paragraph)
            if text:
                lines.extend((text, ""))
    if paper["figures_and_tables"]:
        lines.extend(("## Figures and Tables", ""))
        for item in paper["figures_and_tables"]:
            caption = normalize_text(item["caption"])
            if caption:
                lines.extend((f"FLOAT SELECTED: {caption}", ""))
    return "\n".join(lines).rstrip() + "\n"


def _gold_questions(paper_id: str, paper: dict[str, Any]) -> list[dict[str, Any]]:
    questions = []
    for question in paper["qas"]:
        annotations = []
        for annotation in question["answers"]:
            answer = annotation["answer"]
            annotations.append(
                {
                    "unanswerable": answer["unanswerable"],
                    "evidence": [
                        normalize_text(item)
                        for item in answer["evidence"]
                        if normalize_text(item)
                    ],
                }
            )
        questions.append(
            {
                "paper_id": paper_id,
                "paper_title": normalize_text(paper["title"]),
                "question_id": question["question_id"],
                "question": normalize_text(question["question"]),
                "annotations": annotations,
            }
        )
    return questions


def _retrievable_evidence(paper: dict[str, Any]) -> set[str]:
    evidence = {
        normalize_text(paper["title"]),
        normalize_text(paper["abstract"]),
        "Abstract",
        "Figures and Tables",
    }
    for section in paper["full_text"]:
        evidence.add(normalize_text(section["section_name"]) or "Body")
        evidence.update(
            normalize_text(paragraph)
            for paragraph in section["paragraphs"]
            if normalize_text(paragraph)
        )
    evidence.update(
        f"FLOAT SELECTED: {normalize_text(item['caption'])}"
        for item in paper["figures_and_tables"]
        if normalize_text(item["caption"])
    )
    return evidence


def prepare_corpus(
    dataset: dict[str, dict[str, Any]],
    paper_ids: list[str],
    output_directory: Path,
) -> dict[str, int]:
    missing_ids = sorted(set(paper_ids) - dataset.keys())
    if missing_ids:
        raise ValueError(f"Selected paper IDs are missing from QASPER: {missing_ids}")

    _ensure_directory(output_directory)
    vault = output_directory / "vault"
    _ensure_directory(vault)
    expected_files = {f"{paper_id.replace('.', '-')}.md" for paper_id in paper_ids}
    marker_path = output_directory / MARKER_NAME
    if marker_path.exists() or marker_path.is_symlink():
        _validate_existing_regular_file(marker_path)
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("fixture") != "qasper-v0.3":
            raise ValueError(f"{output_directory} belongs to another fixture")
        generated_markdown = marker.get("generated_markdown")
        if not isinstance(generated_markdown, list) or not all(
            isinstance(filename, str)
            and Path(filename).name == filename
            and filename.endswith(".md")
            for filename in generated_markdown
        ):
            raise ValueError(f"Invalid generated Markdown list in {marker_path}")
        previous_files = set(cast(list[str], generated_markdown))
        existing_markdown = {
            str(path.relative_to(vault))
            for path in vault.rglob("*.md")
        }
        unexpected_markdown = sorted(existing_markdown - previous_files)
        if unexpected_markdown:
            raise ValueError(
                f"Refusing to modify unexpected Markdown in {vault}: "
                f"{unexpected_markdown}"
            )
        for filename in existing_markdown:
            _validate_existing_regular_file(vault / filename)
    else:
        unowned_markdown = list(vault.rglob("*.md"))
        if unowned_markdown:
            raise ValueError(
                f"Refusing to modify unowned Markdown in {vault}; "
                f"{MARKER_NAME} is missing"
            )
        existing_managed_files = [
            path
            for path in (output_directory / "corpus.yml", output_directory / "gold.json")
            if path.exists() or path.is_symlink()
        ]
        if existing_managed_files:
            raise ValueError(
                f"Refusing to overwrite unowned benchmark files; "
                f"{MARKER_NAME} is missing: {existing_managed_files}"
            )
        previous_files = set()

    questions = []
    manifest_lines = [
        "corpus_id: qasper-dev-v0-3",
        "display_name: QASPER development benchmark",
        "vault_root: vault",
        "database: qasper.sqlite3",
        "include:",
        '  - "*.md"',
        "allow_symlinks: false",
        "max_source_bytes: 5000000",
        "seed_entities:",
    ]
    rendered_papers: dict[str, str] = {}
    for paper_id in paper_ids:
        paper = dataset[paper_id]
        paper_slug = paper_id.replace(".", "-")
        filename = f"{paper_slug}.md"
        rendered_papers[filename] = render_paper(paper_id, paper)
        available_evidence = _retrievable_evidence(paper)
        unavailable_evidence = sorted(
            {
                normalize_text(item)
                for question in paper["qas"]
                for annotation in question["answers"]
                for item in annotation["answer"]["evidence"]
                if normalize_text(item) not in available_evidence
            }
        )
        if unavailable_evidence:
            raise ValueError(
                f"Generated Markdown cannot represent QASPER evidence for "
                f"{paper_id}: {unavailable_evidence}"
            )
        title = normalize_text(paper["title"])
        manifest_lines.extend(
            (
                f"  - entity_id: {json.dumps(f'paper-{paper_slug}')}",
                f"    name: {json.dumps(title)}",
                "    entity_type: publication",
                "    aliases: []",
            )
        )
        questions.extend(_gold_questions(paper_id, paper))

    for filename, content in rendered_papers.items():
        _atomic_write(vault / filename, content)
    for filename in previous_files - expected_files:
        stale_path = vault / filename
        _validate_existing_regular_file(stale_path)
        stale_path.unlink(missing_ok=True)

    _atomic_write(output_directory / "corpus.yml", "\n".join(manifest_lines) + "\n")
    _atomic_write(
        output_directory / "gold.json",
        json.dumps(
            {
                "dataset": "QASPER",
                "dataset_version": "0.3",
                "split": "development",
                "papers": len(paper_ids),
                "questions": questions,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    _atomic_write(
        marker_path,
        json.dumps(
            {
                "fixture": "qasper-v0.3",
                "generated_markdown": sorted(expected_files),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return {"papers": len(paper_ids), "questions": len(questions)}


def main() -> None:
    benchmark_directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Download and convert the pinned QASPER benchmark sample."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=benchmark_directory / "data",
        help="Generated corpus directory, which should remain untracked.",
    )
    args = parser.parse_args()

    selection = json.loads(
        (benchmark_directory / "sample.json").read_text(encoding="utf-8")
    )
    archive = download_archive(args.output / "qasper-train-dev-v0.3.tgz")
    counts = prepare_corpus(
        read_development_split(archive),
        selection["paper_ids"],
        args.output,
    )
    print(
        f"Prepared {counts['papers']} papers and {counts['questions']} questions "
        f"in {args.output}"
    )


if __name__ == "__main__":
    main()
