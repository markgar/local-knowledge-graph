from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DEFAULT_FOLDS = 5
DEFAULT_SEED = "local-knowledge-graph-qasper-e4-v1"


def _fold_for_paper(paper_id: str, folds: int, seed: str) -> int:
    digest = hashlib.sha256(f"{seed}:{paper_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % folds


def _is_answered(question: dict[str, Any], threshold: float) -> bool:
    score = question.get("top_score")
    return (
        isinstance(score, (int, float))
        and not isinstance(score, bool)
        and score >= threshold
    )


def _question_key(question: dict[str, Any]) -> tuple[str, str]:
    return question["paper_id"], question["question_id"]


def _confusion(
    questions: Iterable[dict[str, Any]],
    threshold: float,
) -> dict[str, int]:
    counts = {
        "true_positive": 0,
        "false_positive": 0,
        "true_negative": 0,
        "false_negative": 0,
    }
    for question in questions:
        answerable = question["answerable"]
        answered = _is_answered(question, threshold)
        if answerable and answered:
            counts["true_positive"] += 1
        elif answerable:
            counts["false_negative"] += 1
        elif answered:
            counts["false_positive"] += 1
        else:
            counts["true_negative"] += 1
    return counts


def _rates(counts: dict[str, int]) -> tuple[float, float]:
    positive = counts["true_positive"] + counts["false_negative"]
    negative = counts["true_negative"] + counts["false_positive"]
    if positive == 0 or negative == 0:
        raise ValueError("calibration data must contain answerable and unanswerable questions")
    true_positive_rate = counts["true_positive"] / positive
    false_positive_rate = counts["false_positive"] / negative
    return true_positive_rate, false_positive_rate


def calibrate_threshold(questions: list[dict[str, Any]]) -> float:
    scores = sorted(
        {
            float(question["top_score"])
            for question in questions
            if isinstance(question.get("top_score"), (int, float))
            and not isinstance(question["top_score"], bool)
            and math.isfinite(question["top_score"])
        }
    )
    if not scores:
        raise ValueError("calibration data contains no finite reranker scores")
    candidates = [math.nextafter(scores[0], -math.inf)]
    candidates.extend(
        (left + right) / 2
        for left, right in zip(scores, scores[1:], strict=False)
    )
    candidates.append(math.nextafter(scores[-1], math.inf))

    def objective(threshold: float) -> tuple[float, float, float, float]:
        true_positive_rate, false_positive_rate = _rates(
            _confusion(questions, threshold)
        )
        balanced_accuracy = (
            true_positive_rate + (1.0 - false_positive_rate)
        ) / 2
        return (
            balanced_accuracy,
            -false_positive_rate,
            true_positive_rate,
            threshold,
        )

    return max(candidates, key=objective)


def _aggregate(
    questions: list[dict[str, Any]],
    decisions: dict[tuple[str, str], tuple[bool, float, int]],
) -> dict[str, Any]:
    answered_questions = [
        question
        for question in questions
        if decisions[_question_key(question)][0]
    ]
    counts = {
        "true_positive": 0,
        "false_positive": 0,
        "true_negative": 0,
        "false_negative": 0,
    }
    for question in questions:
        answered = decisions[_question_key(question)][0]
        answerable = question["answerable"]
        if answerable and answered:
            counts["true_positive"] += 1
        elif answerable:
            counts["false_negative"] += 1
        elif answered:
            counts["false_positive"] += 1
        else:
            counts["true_negative"] += 1

    true_positive_rate, false_positive_rate = _rates(counts)
    total = len(questions)
    answered = len(answered_questions)
    answerable = counts["true_positive"] + counts["false_negative"]
    metrics = {
        "answerability_accuracy": (
            counts["true_positive"] + counts["true_negative"]
        )
        / total,
        "answerability_balanced_accuracy": (
            true_positive_rate + (1.0 - false_positive_rate)
        )
        / 2,
        "answerable_coverage": true_positive_rate,
        "answered_precision": counts["true_positive"] / answered if answered else 1.0,
        "unanswerable_false_evidence_rate": false_positive_rate,
        "evidence_recall_at_1": sum(
            question["recall_at_1"]
            for question in answered_questions
            if question["answerable"]
        )
        / answerable,
        "evidence_recall_at_5": sum(
            question["recall_at_5"]
            for question in answered_questions
            if question["answerable"]
        )
        / answerable,
        "evidence_recall_at_10": sum(
            question["recall_at_10"]
            for question in answered_questions
            if question["answerable"]
        )
        / answerable,
        "mean_reciprocal_rank": sum(
            question["reciprocal_rank"]
            for question in answered_questions
            if question["answerable"]
        )
        / answerable,
        "evidence_set_f1_at_10": sum(
            question["evidence_f1_at_10"]
            for question in answered_questions
            if question["answerable"]
        )
        / answerable,
    }
    return {
        "metrics": metrics,
        "confusion": counts,
        "question_results": [
            {
                **question,
                "answered": decisions[_question_key(question)][0],
                "answerability_threshold": decisions[_question_key(question)][1],
                "calibration_fold": decisions[_question_key(question)][2],
            }
            for question in questions
        ],
    }


def calibrate(
    reranked_result: dict[str, Any],
    *,
    folds: int = DEFAULT_FOLDS,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    if folds < 2:
        raise ValueError("folds must be at least 2")
    if reranked_result.get("query_strategy") != "reranked":
        raise ValueError("E4 calibration requires E3 reranked results")
    questions = reranked_result.get("question_results")
    if not isinstance(questions, list) or not questions:
        raise ValueError("reranked results contain no question results")
    required_fields = {
        "paper_id",
        "question_id",
        "answerable",
        "top_score",
        "recall_at_1",
        "recall_at_5",
        "recall_at_10",
        "reciprocal_rank",
        "evidence_f1_at_10",
    }
    for question in questions:
        missing = required_fields - question.keys()
        if missing:
            raise ValueError(
                "reranked question result is missing fields: "
                + ", ".join(sorted(missing))
            )
    question_keys = [_question_key(question) for question in questions]
    if len(question_keys) != len(set(question_keys)):
        raise ValueError("reranked results contain duplicate paper and question IDs")

    fold_by_paper = {
        question["paper_id"]: _fold_for_paper(question["paper_id"], folds, seed)
        for question in questions
    }
    decisions: dict[tuple[str, str], tuple[bool, float, int]] = {}
    fold_thresholds = []
    for fold in range(folds):
        training = [
            question
            for question in questions
            if fold_by_paper[question["paper_id"]] != fold
        ]
        evaluation = [
            question
            for question in questions
            if fold_by_paper[question["paper_id"]] == fold
        ]
        if not evaluation:
            raise ValueError(f"calibration fold {fold} contains no questions")
        threshold = calibrate_threshold(training)
        fold_thresholds.append(
            {
                "fold": fold,
                "threshold": threshold,
                "training_questions": len(training),
                "evaluation_questions": len(evaluation),
            }
        )
        for question in evaluation:
            decisions[_question_key(question)] = (
                _is_answered(question, threshold),
                threshold,
                fold,
            )

    aggregate = _aggregate(questions, decisions)
    return {
        "experiment": "E4",
        "method": "paper-grouped out-of-fold top-reranker-score threshold",
        "objective": "maximum balanced answerability accuracy",
        "folds": folds,
        "seed": seed,
        "dataset": reranked_result.get("dataset"),
        "dataset_version": reranked_result.get("dataset_version"),
        "embedding_profile": reranked_result.get("embedding_profile"),
        "questions": len(questions),
        "deployment_threshold": calibrate_threshold(questions),
        "fold_thresholds": fold_thresholds,
        **aggregate,
    }


def main() -> None:
    benchmark_directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Calibrate E4 answerability from unchanged E3 reranked results."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=benchmark_directory / "data" / "results-reranked-gte.json",
    )
    parser.add_argument("--folds", type=int, default=DEFAULT_FOLDS)
    parser.add_argument("--seed", default=DEFAULT_SEED)
    parser.add_argument(
        "--output",
        type=Path,
        default=benchmark_directory / "data" / "results-selective-gte.json",
    )
    args = parser.parse_args()
    reranked_result = json.loads(args.input.read_text(encoding="utf-8"))
    result = calibrate(reranked_result, folds=args.folds, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["metrics"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
