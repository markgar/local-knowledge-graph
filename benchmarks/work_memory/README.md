# Work-memory comparison

This harness compares answers from fresh agents using the same frozen Markdown
sources and questions, with different tools. Unlike the
[scripted agent CLI checks](../agent/README.md), agents choose operations and
submit answers. **The harness does not launch agents.** A coordinator enforces
fresh contexts, identical runtime/model settings, and access restrictions.

## Retrieval and tool boundaries

The indexed arms use the [Markdown demonstration](../../README.md#markdown-demonstration)
and its SQLite database. Here, "graph" means explicit demonstration relationships
and subject expansion, not Ladybug or the canonical evidence/query services.

New runs use protocol version 3 and three arms by default:

| Arm | Shared tools | Additional tools |
| --- | --- | --- |
| `markdown` | Batch `read`, case-insensitive regex `grep`, catalogs, response paging | `search` aliases content grep |
| `index` | Same | Corpus-wide lexical passage search, source ranges/context |
| `kg` | Same | Indexed search with graph-expanded subject scope, explicit status/actions/state/history and evidence reads |

Indexed `search` uses the private
[`_lexical_search.py`](../_lexical_search.py) worker, with **strict** matching when
`--query-mode` is omitted. `--query-mode natural` selects OR-term BM25.
Both are benchmark-only options, not public `kg search` modes. Semantic modes
and contextual search are rejected; no semantic models or vector index are
required. Normal product search uses the full pipeline and has separate
[parity validation](../productization/README.md).

The `index` arm cannot use `--subject`, `--explain`, `--include-quotes`, or
structured-state commands. The `kg` arm can request version-1 lexical
explanations; `--include-quotes` requires `--explain`. Non-search indexed
operations use the `kg` CLI. All arms are restricted to the run's manifest and
JSON transport. Version-3 runs reject live-clock `--since`: interpret relative
time using the question pack's reference date and dated evidence.

## Prepare a run

Run commands from the repository root with the
[development environment](../../CONTRIBUTING.md) available:

```bash
uv run python benchmarks/work_memory/evaluate.py prepare \
  --output .kg/work-memory-r1 \
  --arms markdown index kg \
  --replicate 1 \
  --response-limit-bytes 12000
```

Preparation requires a new output directory. Defaults are
[`corpora/atlas-state.yml`](../../corpora/atlas-state.yml),
[`questions.json`](questions.json), and [`gold.json`](gold.json): twelve
synthetic Atlas documents and ten questions. For another scenario, pass
`--manifest`, `--questions`, and `--gold`. Questions and gold must have identical,
unique question IDs. `--arms` selects a nonempty, nonrepeating subset of the
three arms; `--replicate` is a positive integer (default 1).

Preparation copies selected sources into `notes/`, freezes questions, gold,
manifest, source hashes, arm list, replicate, and access/transport policy in
`snapshot.json`, and builds one shared SQLite index. `ingestion.json` and
`setup.json` record ingestion outcomes and cost. Gold is private by protocol:
the wrapper does not expose it, but the run directory is **not a filesystem
security boundary**.

Do not give agents access to gold, evaluator/generator source, private scenario
metadata, other agents' answers, prior runs, or repository/session history.
Give each agent only its arm, run directory, and wrapper command:

```bash
uv run python benchmarks/work_memory/evaluate.py tool --run .kg/work-memory-r1 --arm markdown -- info
uv run python benchmarks/work_memory/evaluate.py tool --run .kg/work-memory-r1 --arm index -- info
uv run python benchmarks/work_memory/evaluate.py tool --run .kg/work-memory-r1 --arm kg -- info
```

`info` supplies the same questions, conventions, source names, and seed entities,
plus arm-specific tools and submission instructions. Every arm may read the full
corpus. Use **one wrapper invocation per tool call**, without shell chaining or
stdout pipes. Do not repeat a successful query merely to reformat output.

## Submit, score, and repeat

Each agent writes only its own `answers-ARM.json` in the run directory. The
top-level `answers` array contains one object per question with `id`, a nonempty
`answer` string, `facts` object, `abstains` boolean, and `citations` array; follow
the question pack's fact and citation conventions. Then invoke, for that arm:

```bash
uv run python benchmarks/work_memory/evaluate.py tool --run .kg/work-memory-r1 --arm kg -- submit
```

Submission checks structure and records the answer hash, not correctness. No gold
feedback is returned. After **all configured arms** submit, the coordinator runs:

```bash
uv run python benchmarks/work_memory/evaluate.py score --run .kg/work-memory-r1
```

The scorer verifies frozen inputs, gold, and submitted-answer hashes, and writes
`scores.json`. Report facts, abstention, exact citation validity, required evidence
coverage, and full-question passes separately. Review natural-language answers
for unsupported extra claims: exact fact scoring does not check every assertion.

For repetitions, prepare new directories with distinct `--replicate` values and
identical inputs, arm configuration, and response budget. Use fresh agents with
identical model settings for every arm/replicate, then summarize at least two runs:

```bash
uv run python benchmarks/work_memory/evaluate.py summarize \
  --runs .kg/work-memory-r1 .kg/work-memory-r2 \
  --output .kg/work-memory-summary.json
```

The summary rechecks submissions and requires distinct run/replicate IDs and
identical frozen inputs/tool policies. It reports observations, means, and medians
without changing per-run scores, certifying host delivery, or claiming statistical
significance. New runs with `--arms markdown kg` still give KG document access;
older snapshots without an arm list retain the original two-arm, KG-only policy.

## Bounded responses

`--response-limit-bytes` is optional (unbounded by default) and must be at least
4000. It is a **shared benchmark transport adapter**, not a native KG optimization.
Oversized responses are stored intact by content hash under `responses/ARM/`.
The returned envelope identifies their ID, size, and fields.

Within the wrapper, use:

```text
response ID --field /JSON/POINTER --offset N --limit N
response-fields ID --field /JSON/POINTER --offset N --limit N
catalog sources|entities [REGEX] --offset N --limit N
```

Offsets default to 0 and must be nonnegative; limits default to 20 and must be
between 1 and 2000. `response-fields` lists an object's keys;
`response` selects JSON-pointer fields and pages lists/strings. String offsets
are characters, preserving source text. Pages report total length and next
offset. In bounded mode, `info` previews at most 20 sources/entities; use catalogs
for the rest. `info` itself can return an envelope, including for the interleaved
question pack. Archive cached responses alongside journals so evidence remains
recoverable. Do not compare paged and unpaged call counts as if only corpus size
changed.

## Telemetry and delivery

Per-arm `tools-ARM.jsonl` journals preserve requests and returned payloads,
including failures, request/run IDs, subprocess argv/exit status, sizes, timing,
response hash/counts, and separate `operation_success` and `delivery_success`.
Indexed search records backend `legacy_lexical`; other CLI operations use
`kg_cli`. A successful operation can still fail stdout delivery, and a successful
stdout write does not prove the tool host delivered the complete response.

Validate transport using actual wrapper/CLI calls, including successful, empty,
invalid, and failed-delivery cases. Collect the independent host layer from the
actual session event log:

```bash
uv run python benchmarks/work_memory/host_telemetry.py \
  --events /path/to/session/events.jsonl \
  --run .kg/work-memory-r1 \
  --output .kg/work-memory-r1/host-telemetry.json
```

The collector exports matching tool events, observed model tags, host call IDs,
process exits, returned-content sizes, and explicit spills. It correlates arm,
arguments, and time windows; missing/ambiguous matches remain explicit.
Host-tool success is not process success. Preserve audits with each run.
Journals and exports contain queries/source quotes and must be protected like the
corpus; host exports exclude unrelated events and assistant reasoning.

Data-tool totals exclude `info`, `submit`, and shared ingestion, but **include**
all `response` fetches, even pages of `info`. Report that common setup cost
separately. Bytes describe serialized payloads, not model-visible context or
tokens. Wrapper elapsed time includes frozen-source integrity hashing before and
after calls; it is not database performance or end-to-end agent reasoning cost.
Later model-context assembly is unobserved, and runtime defaults alone do not
verify actual model identity.

## Larger and discovery scenarios

All generators require new output directories. Keep names and expectations in
benchmark data rather than introducing corpus-specific engine rules.

- `expanded.py --output DIRECTORY` preserves the twelve Atlas sources and adds
  dated work history, explicit replacement chains, ownership/numerical changes,
  and other projects. Pass its `corpus.yml`, `questions.json`, and `gold.json`
  to `prepare`.
- `interleaved.py` generates prose mail, meetings, and notes with opaque paths
  and mixed projects. Defaults: `--background-documents 2000 --seed 17`.
  These produce 2,078 documents and fourteen direct questions across 17
  workstreams. Source facts, not KG output, define gold.
- `discovery.py` creates ten partial-memory questions over the unchanged
  interleaved sources. It freezes a reference date and includes imperfect date
  recall and ambiguity. Do not show discovery agents the direct question pack,
  private mappings/rationales, or prior answers.

Example discovery setup:

```bash
uv run python benchmarks/work_memory/interleaved.py --output .kg/interleaved-input
uv run python benchmarks/work_memory/discovery.py \
  --inputs .kg/interleaved-input --output .kg/discovery-questions
uv run python benchmarks/work_memory/evaluate.py prepare \
  --output .kg/discovery-r1 \
  --manifest .kg/interleaved-input/corpus.yml \
  --questions .kg/discovery-questions/questions.json \
  --gold .kg/discovery-questions/gold.json \
  --arms markdown index kg --replicate 1 --response-limit-bytes 12000
```

Interleaved sources do not supply perfect wikilinks or explicit owner/due/
supersession annotations. Successful ingestion does not establish understanding:
the engine does not generally infer prose commitments, email-thread context, or
implicit supersession. An empty task list is not evidence that prose has no
commitments. Source data is synthetic, background text is templated, and every
arm receives approved entity names/aliases. Private metadata's
`not_in_required_evidence_fraction` measures evidence sparsity, not semantic
irrelevance of all other documents.

Interpret `index` versus `markdown` as adding indexed retrieval, and `kg` versus
`index` as adding graph/state tools. Correct project classification alone does
not establish discovery success: required evidence must identify the episode
and explanations still need review. No embedding-based retrieval is measured.

## Retained evidence and limitations

Frozen [Atlas pilot](results/atlas-pilot/scores.json),
[clean rerun](results/atlas-clean-rerun/scores.json),
[expanded comparison](results/atlas-expanded-v1/scores.json), and
[discovery replicates](results/discovery-v1/summary.json) preserve measured
outcomes, journals, and reviews. They do not demonstrate a consistent index/KG
efficiency advantage or establish broad superiority on real personal work.
The expanded run contains documented gold-rule defects
([review](results/atlas-expanded-v1/review.json)); preserve its original gold and
scores rather than substituting post-hoc regrading. Pilot delivery problems and
non-blinded reviews also limit interpretation. Small, correlated question sets
and two discovery repetitions do not establish statistical significance.

Change sources, gold, tools, prompts, seed, or transport settings only in a new
run, never mid-comparison. Preserve labels, frozen dates, hashes, exact evidence,
and negative results. These lexical benchmarks need no model downloads; any
dependency/network blockers must use approved resources, not policy bypasses.

Discovery archives store sources, cached responses, and ingestion reports in
`evidence.tar.gz`; the rebuildable SQLite index is omitted. To restore the
original layout for inspection/scoring, extract a bundle into its replicate
directory:

```bash
tar -xzf benchmarks/work_memory/results/discovery-v1/r1/evidence.tar.gz \
  -C benchmarks/work_memory/results/discovery-v1/r1
```

Keep answers, gold, scores, journals, audits, and review records unchanged.
