# Author cited knowledge

Inspect `kg schema show --json` and use its exact active revision and registered
names. No entity type or predicate is universal. Run
`kg record --from-evidence evidence:... --json` to prefill exact current support;
it leaves `entities` and `assertions` empty and makes no write.

The record file contains only the strict `record-authoring/1` document:

```json
{
  "interface_version": "record-authoring/1",
  "expected_schema_revision": {
    "revision_id": "COPY-EXACT-REVISION-ID",
    "definition_hash": "COPY-EXACT-DEFINITION-HASH"
  },
  "support": {
    "source": {
      "kind": "source",
      "reference": {
        "corpus_id": "COPY-CORPUS-ID",
        "source_namespace": "COPY-SOURCE-NAMESPACE",
        "document_id": "COPY-DOCUMENT-ID",
        "revision_id": "COPY-DOCUMENT-REVISION-ID",
        "anchor_id": "COPY-ANCHOR-ID",
        "passage_id": null
      },
      "state_version": "COPY-STATE-VERSION"
    }
  },
  "entities": [
    {
      "local_id": "subject",
      "identity": {
        "kind": "new",
        "name": "COPY-SUPPORTED-NAME",
        "support": ["source"]
      },
      "classifications": [
        {
          "local_id": "subject-type",
          "entity_type": "REGISTERED_ENTITY_TYPE",
          "interpretation": "explicit",
          "support": ["source"],
          "select": {
            "rationale": "The source explicitly identifies this type."
          }
        }
      ],
      "aliases": [],
      "identifiers": [],
      "mentions": []
    }
  ],
  "assertions": [
    {
      "local_id": "supported-fact",
      "subject": "subject",
      "predicate": "REGISTERED_PREDICATE",
      "object": {
        "kind": "string",
        "value": "COPY-SUPPORTED-VALUE"
      },
      "interpretation": "explicit",
      "support": ["source"]
    }
  ]
}
```

An identifiable thing may remain unclassified: omit `classifications` and
`assertions`, but keep its exact identity support. Do not infer a type, resolve an
entity by name, or merge same-named mentions.

To reuse a stored entity, declare it in `entities` with
`{"kind":"existing","entity_id":"COPY-EXACT-ID"}`. Typed assertions require the
copied `selection_witness` returned by `kg read entity:ID`; classification changes
require the copied `review_witness` returned by `kg classifications entity:ID`.

Submit only after review:

```text
kg record record.json --retry-key SAVED-KEY --json
```

Keep the exact input, key, and canonical authored receipt. If the outcome is
unknown, retry only that exact file and key. Use `kg skill` for source/seed support,
aliases, identifiers, mentions, classification review, retry rules, and limits.
