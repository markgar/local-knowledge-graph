"""Generate deterministic foundation inputs; does not execute future services."""

from __future__ import annotations

import hashlib
import json
from collections import Counter

VERSION = "foundation-workload/1"
SEED = 1729


def documents() -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for index in range(1000):
        corpus = "work" if index < 800 else "isolated"
        namespace = "markdown" if index % 2 == 0 else "email"
        # Formula-based generation is independent of random-module/runtime versions.
        task = index < 240
        text = (
            "- [ ] Review Atlas. [owner:: Sam]\r\n" if task
            else "Repeated source evidence.\r\n"
        )
        text += "Cafe\u0301 \U0001f680\r\n"
        if index % 5 == 0:
            text += "\r\n".join(f"Paragraph {part}: reference {(index + SEED) % 17}."
                                for part in range(40))
        result.append({
            "corpus_id": corpus,
            "source_namespace": namespace,
            "external_id": f"item-{index // 2}",
            "text": text,
            "matching_task": task,
            "access_group": "denied" if index % 10 == 1 else "allowed",
            "entity_name": "Sam" if task else "Sam Example",
        })
    return result


def workload() -> dict[str, object]:
    docs = documents()
    return {
        "version": VERSION,
        "seed": SEED,
        "documents": docs,
        "operations": [
            {"operation": "create", "indices": list(range(1000))},
            {"operation": "identical_retry", "indices": list(range(100))},
            {"operation": "update", "indices": list(range(100)), "append": "\nUpdated."},
            {"operation": "remove_restore", "indices": list(range(100, 150))},
            {"operation": "metadata_only", "indices": list(range(150, 175))},
            {"operation": "stale_write", "indices": list(range(25))},
            {"operation": "failed_snapshot", "indices": list(range(175, 200))},
        ],
        "query_mix": {"structured": 100, "graph": 100, "search": 100, "count": 100},
        "search_limit": 20,
        "ambiguity": ["Sam", "Sam Example"],
        "graph": {
            "entities": ["sam-primary", "sam-alternative", "atlas"]
            + [f"task-{index}" for index in range(240)],
            "edges": [
                {"subject": subject, "predicate": predicate, "object": target,
                 "support_document_index": index, "interpretation": "inferred"}
                for index in range(240)
                for subject, predicate, target in (
                    ("sam-primary", "work:owns", f"task-{index}"),
                    (f"task-{index}", "work:part_of", "atlas"),
                )
            ],
        },
    }


def serialized() -> bytes:
    return (json.dumps(workload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n").encode("utf-8")


def manifest() -> dict[str, object]:
    docs = documents()
    return {
        "version": VERSION,
        "seed": SEED,
        "documents": len(docs),
        "corpora": dict(Counter(str(doc["corpus_id"]) for doc in docs)),
        "namespaces": dict(Counter(str(doc["source_namespace"]) for doc in docs)),
        "text_bytes": sum(len(str(doc["text"]).encode("utf-8")) for doc in docs),
        "workload_bytes": len(serialized()),
        "sha256": hashlib.sha256(serialized()).hexdigest(),
        "matching_tasks": sum(bool(doc["matching_task"]) for doc in docs),
        "visible_matching_tasks": sum(
            bool(doc["matching_task"]) and doc["access_group"] == "allowed" for doc in docs
        ),
    }


if __name__ == "__main__":
    print(json.dumps(manifest(), indent=2, sort_keys=True))
