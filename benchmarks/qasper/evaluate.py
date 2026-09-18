from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from pathlib import Path
from typing import Any

from kg.config import load_manifest
from kg.db import Database
from kg.retrieval import RetrievalService


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def normalize_evidence(value: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", normalize_text(value))


def _gold_sets(question: dict[str, Any]) -> list[set[str]]:
    return [
        {normalize_evidence(item) for item in annotation["evidence"]}
        for annotation in question["annotations"]
        if annotation["evidence"]
    ]


def _best_recall(retrieved: set[str], gold_sets: list[set[str]]) -> float:
    if not gold_sets:
        return 0.0
    return max(len(retrieved & gold) / len(gold) for gold in gold_sets)


def _best_f1(retrieved: set[str], gold_sets: list[set[str]]) -> float:
    scores = []
    for gold in gold_sets:
        overlap = len(retrieved & gold)
        precision = overlap / len(retrieved) if retrieved else 0.0
        recall = overlap / len(gold)
        scores.append(
            2 * precision * recall / (precision + recall)
            if precision + recall
            else 0.0
        )
    return max(scores, default=0.0)


def evaluate(
    manifest_path: Path,
    gold_path: Path,
    limit: int = 10,
    strategy: str = "natural",
) -> dict[str, Any]:
    if strategy not in {"strict", "natural"}:
        raise ValueError("strategy must be 'strict' or 'natural'")
    if limit < 10:
        raise ValueError("limit must be at least 10 for @10 metrics")
    manifest = load_manifest(manifest_path)
    retrieval = RetrievalService(Database(manifest.database), manifest.corpus_id)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    questions = gold["questions"]
    recall_totals = {1: 0.0, 5: 0.0, 10: 0.0}
    reciprocal_rank_total = 0.0
    evidence_f1_total = 0.0
    answerable = 0
    unanswerable = 0
    false_evidence = 0
    intact_anchors = 0
    returned_records = 0
    latencies = []
    question_results = []

    for question in questions:
        started = time.perf_counter()
        results = retrieval.search(
            question["question"],
            subject=question["paper_title"],
            limit=limit,
            query_mode=strategy,
        )
        latencies.append((time.perf_counter() - started) * 1000)
        retrieved = [normalize_evidence(result.quote) for result in results]
        retrieved_at_10 = retrieved[:10]
        retrieved_set_at_10 = set(retrieved_at_10)
        gold_sets = _gold_sets(question)

        if gold_sets:
            answerable += 1
            for cutoff in recall_totals:
                recall_totals[cutoff] += _best_recall(
                    set(retrieved[:cutoff]),
                    gold_sets,
                )
            first_hit = next(
                (
                    rank
                    for rank, quote in enumerate(retrieved, start=1)
                    if any(quote in gold_set for gold_set in gold_sets)
                ),
                None,
            )
            if first_hit:
                reciprocal_rank_total += 1 / first_hit
            evidence_f1_total += _best_f1(retrieved_set_at_10, gold_sets)
        else:
            unanswerable += 1
            false_evidence += bool(results)

        for result in results:
            returned_records += 1
            evidence = retrieval.evidence(result.record_id)
            if (
                evidence.anchor_id == result.anchor_id
                and evidence.source_revision_id == result.source_revision_id
                and normalize_evidence(evidence.quote)
                == normalize_evidence(result.quote)
            ):
                intact_anchors += 1

        question_results.append(
            {
                "paper_id": question["paper_id"],
                "question_id": question["question_id"],
                "answerable": bool(gold_sets),
                "returned": len(results),
                "recall_at_10": _best_recall(retrieved_set_at_10, gold_sets),
            }
        )

    latency_sorted = sorted(latencies)
    if not latency_sorted:
        raise ValueError("gold dataset contains no questions")
    p95_index = math.ceil(0.95 * len(latency_sorted)) - 1
    return {
        "dataset": gold["dataset"],
        "dataset_version": gold["dataset_version"],
        "split": gold["split"],
        "papers": gold["papers"],
        "questions": len(questions),
        "query_strategy": strategy,
        "answerable_questions": answerable,
        "unanswerable_questions": unanswerable,
        "metrics": {
            "evidence_recall_at_1": recall_totals[1] / answerable,
            "evidence_recall_at_5": recall_totals[5] / answerable,
            "evidence_recall_at_10": recall_totals[10] / answerable,
            "mean_reciprocal_rank": reciprocal_rank_total / answerable,
            "evidence_set_f1_at_10": evidence_f1_total / answerable,
            "unanswerable_false_evidence_rate": (
                false_evidence / unanswerable if unanswerable else 0.0
            ),
            "anchor_integrity": (
                intact_anchors / returned_records if returned_records else 1.0
            ),
            "median_query_latency_ms": statistics.median(latencies),
            "p95_query_latency_ms": latency_sorted[p95_index],
        },
        "question_results": question_results,
    }


def main() -> None:
    benchmark_directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Evaluate local knowledge graph retrieval on QASPER."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=benchmark_directory / "data" / "corpus.yml",
    )
    parser.add_argument(
        "--gold",
        type=Path,
        default=benchmark_directory / "data" / "gold.json",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--strategy", choices=("strict", "natural"), default="natural")
    parser.add_argument(
        "--output",
        type=Path,
        default=benchmark_directory / "data" / "results.json",
    )
    args = parser.parse_args()

    result = evaluate(args.manifest, args.gold, args.limit, args.strategy)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
