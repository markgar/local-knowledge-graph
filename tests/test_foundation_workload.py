from __future__ import annotations

from support.modules import module


def test_workload_is_reproducible_and_pins_sizes() -> None:
    workload = module("benchmarks/foundation/workload.py")
    assert workload.serialized() == workload.serialized()
    assert workload.manifest() == {
        "version": "foundation-workload/1",
        "seed": 1729,
        "documents": 1000,
        "corpora": {"work": 800, "isolated": 200},
        "namespaces": {"markdown": 500, "email": 500},
        "text_bytes": 266840,
        "workload_bytes": 512100,
        "sha256": "ae51bd8b5eb6c2ce3c8a1318c029d4eeed0bd1fc021b0d16f72b51094fcb20f8",
        "matching_tasks": 240,
        "visible_matching_tasks": 216,
    }
    docs = workload.documents()
    assert len({doc["text"] for doc in docs}) < len(docs)
    assert docs[0]["external_id"] == docs[1]["external_id"]
    assert docs[0]["source_namespace"] != docs[1]["source_namespace"]
    assert all("\r\n" in doc["text"] and "\u0301" in doc["text"] and "🚀" in doc["text"]
               for doc in docs)
    assert len(workload.workload()["operations"]) == 7
    assert len(workload.workload()["graph"]["entities"]) == 243
    assert len(workload.workload()["graph"]["edges"]) == 480
