# Author a grounded record

Inspect `kg schema show --json` and use its exact active revision and registered
names. No entity type or predicate is universal. Run `kg record
--from-evidence evidence:... --json` to prefill exact current support; it leaves
`changes` empty and makes no write.

## Create an unclassified entity

An identifiable thing may remain unclassified. Add only exact existence support:

```json
{
  "expected_schema_revision": "COPY-EXACT-REVISION-OBJECT",
  "support": {"source": "COPY-SCAFFOLD-SUPPORT-OBJECT"},
  "changes": [{
    "kind": "entity",
    "local_id": "sample-item",
    "name": "COPY-SUPPORTED-NAME",
    "support": {"kind": "source", "evidence": ["source"]}
  }]
}
```

Do not infer a type or merge same-named mentions.

## Reuse an entity in a scalar assertion

Find or read the intended entity and copy its exact `reference` object. Replace
the placeholders with names from the active schema:

```json
{
  "expected_schema_revision": "COPY-EXACT-REVISION-OBJECT",
  "support": {"source": "COPY-SCAFFOLD-SUPPORT-OBJECT"},
  "changes": [{
    "kind": "assertion",
    "local_id": "sample-property",
    "subject": "COPY-STORED-ENTITY-REFERENCE-OBJECT",
    "predicate": "REGISTERED_PREDICATE",
    "object": {"kind": "string", "value": "COPY-SUPPORTED-VALUE"},
    "subject_classification": "COPY-REQUIRED-REGISTERED_TYPE-SELECTION",
    "interpretation": "explicit",
    "support": {"kind": "source", "evidence": ["source"]}
  }]
}
```

Do not omit a required classification selection or replace it with a type name.
Submit only after review:

```text
kg record record.json --retry-key SAVED-KEY --json
```

Keep the exact input, key, canonical receipt, and returned copy-ready mappings.
Use each mapping's `inspection_command` where present. Selection-event mappings
intentionally have no fact inspection target.

Classification claims and selections require deliberate typed-endpoint review;
use `kg classifications --help` and `kg record --schema`. Use `kg skill` for
aliases, identifiers, mentions, selection history, native support arrays, retry
rules, and uncommon limits.
