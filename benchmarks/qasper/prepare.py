from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

ARCHIVE_URL = (
    "https://qasper-dataset.s3.us-west-2.amazonaws.com/"
    "qasper-train-dev-v0.3.tgz"
)
ARCHIVE_SHA256 = "a28fdf966db827bcee3d873107d6b6669864fb7ca8fbf73a192f5e39191bdb5a"
DEV_MEMBER = "qasper-dev-v0.3.json"
MARKER_NAME = ".qasper-fixture.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_archive(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and _sha256(destination) == ARCHIVE_SHA256:
        return destination
    temporary = destination.with_suffix(".tmp")
    urllib.request.urlretrieve(ARCHIVE_URL, temporary)
    actual_hash = _sha256(temporary)
    if actual_hash != ARCHIVE_SHA256:
        temporary.unlink(missing_ok=True)
        raise ValueError(
            f"QASPER archive checksum mismatch: expected {ARCHIVE_SHA256}, "
            f"received {actual_hash}"
        )
    temporary.replace(destination)
    return destination


def read_development_split(archive: Path) -> dict[str, dict[str, Any]]:
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.getmember(DEV_MEMBER)
        stream = bundle.extractfile(member)
        if stream is None:
            raise ValueError(f"{DEV_MEMBER} is missing from {archive}")
        return json.load(stream)


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

    vault = output_directory / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    expected_files = {f"{paper_id.replace('.', '-')}.md" for paper_id in paper_ids}
    marker_path = output_directory / MARKER_NAME
    if marker_path.exists():
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("fixture") != "qasper-v0.3":
            raise ValueError(f"{output_directory} belongs to another fixture")
        previous_files = set(marker.get("generated_markdown", []))
    else:
        existing_markdown = list(vault.glob("*.md"))
        if existing_markdown:
            raise ValueError(
                f"Refusing to modify unowned Markdown in {vault}; "
                f"{MARKER_NAME} is missing"
            )
        previous_files = set()
    for filename in previous_files - expected_files:
        if Path(filename).name != filename:
            raise ValueError(f"Invalid generated filename in {marker_path}: {filename}")
        (vault / filename).unlink(missing_ok=True)

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
    for paper_id in paper_ids:
        paper = dataset[paper_id]
        paper_slug = paper_id.replace(".", "-")
        filename = f"{paper_slug}.md"
        (vault / filename).write_text(
            render_paper(paper_id, paper),
            encoding="utf-8",
        )
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

    (output_directory / "corpus.yml").write_text(
        "\n".join(manifest_lines) + "\n",
        encoding="utf-8",
    )
    (output_directory / "gold.json").write_text(
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
        encoding="utf-8",
    )
    marker_path.write_text(
        json.dumps(
            {
                "fixture": "qasper-v0.3",
                "generated_markdown": sorted(expected_files),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
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
