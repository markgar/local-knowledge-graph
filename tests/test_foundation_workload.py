from __future__ import annotations

from support.modules import module

from kg.markdown import parse_markdown


def test_workload_is_reproducible_and_pins_sizes() -> None:
    workload = module("benchmarks/foundation/workload.py")
    assert workload.serialized() == workload.serialized()
    assert workload.manifest() == {
        "version": "foundation-workload/2",
        "seed": 1729,
        "documents": 1000,
        "corpora": {"work": 800, "isolated": 200},
        "namespaces": {"markdown": 500, "email": 500},
        "text_bytes": 283240,
        "workload_bytes": 544977,
        "sha256": "d627ee88e0d5d5fa7ef1a5babea74ffb611a7865a4ff1c37095b49072d022ef0",
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


def test_long_workload_documents_have_real_paragraph_boundaries() -> None:
    workload = module("benchmarks/foundation/workload.py")
    counts: dict[int, int] = {}
    for index, document in enumerate(workload.documents()):
        parsed = parse_markdown(document["text"], "synthetic")
        counts[len(parsed.anchors)] = counts.get(len(parsed.anchors), 0) + 1
        if index % 5 == 0:
            assert len(parsed.anchors) == 41
            for anchor in parsed.anchors:
                assert document["text"][anchor.start_offset:anchor.end_offset] == anchor.quote
    assert counts == {41: 200, 1: 800}
