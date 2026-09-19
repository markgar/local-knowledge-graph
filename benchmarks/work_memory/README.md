# Work-memory comparison

This is an actual agent-answer comparison, separate from the scripted CLI
contract checks in `benchmarks/agent/`. It asks whether the KG helps an agent
prepare from a growing set of work documents, compared with reading/searching
the same Markdown sources.

The initial fixture contains twelve synthetic Atlas documents and ten
predeclared questions. `questions.json` is the public task; `gold.json` is
withheld until both arms submit. Both files are hashed before a run. Gold checks
current facts, open work, explicit completion, two-hop evidence, chronology,
similar-name isolation, code examples, and abstention.

## Run protocol

```bash
uv run python benchmarks/work_memory/evaluate.py prepare --output /path/to/new-run
```

Preparation copies the selected corpus into the run directory, records hashes,
and builds the KG once. It refuses to overwrite an existing run.

Launch two fresh agents using the same agent configuration/runtime defaults.
Give each only its arm name, run directory, and this tool command:

```bash
uv run python benchmarks/work_memory/evaluate.py tool --run /path/to/new-run --arm kg -- info
uv run python benchmarks/work_memory/evaluate.py tool --run /path/to/new-run --arm markdown -- info
```

`info` supplies identical questions, conventions, source names, and seed-entity
configuration. The KG arm can use read-only lexical KG commands. The Markdown
arm can use regex search and batch document reads; reading the entire small
corpus is explicitly allowed. Do not give either arm access to gold, evaluator
source, the other arm, or repository/session history. These are protocol
restrictions, not a filesystem security boundary.

Use one wrapper invocation per tool call, with no shell chaining or stdout
pipes. This rule was added after the recorded pilot's delivery audit, not
retroactively imposed on that run. A successful database operation can still
fail during delivery: the wrapper now records `operation_success` separately
from `delivery_success` and captures broken-pipe errors. It cannot observe
truncation performed later by the tool host.

## Telemetry gate

Before another comparison, verify the tooling with real CLI invocations, not
only unit tests or inferred code paths. The wrapper records a run/request ID,
the exact native CLI arguments and exit code, subprocess stdout/stderr sizes,
timings, serialized-response hash and collection counts, and separate operation
and stdout-delivery outcomes. Empty successful results remain distinguishable
from query errors and broken pipes.

Collect the second layer from the actual tool host's event log:

```bash
uv run python benchmarks/work_memory/host_telemetry.py \
  --events /path/to/session/events.jsonl \
  --run /path/to/run \
  --output /path/to/run/host-telemetry.json
```

The collector exports only matching tool events, with host call IDs, recorded
model tags, process exits, returned-content sizes, and explicit spill notices.
It correlates journals using arm, exact arguments, and the observed host time
window. Missing and ambiguous matches stay explicit. Host-tool success does
not mean process success; a successful stdout write does not mean the host
delivered the whole output inline.

Live preflight covered a successful explained query, an empty result, a CLI
validation error, and a deliberately closed output pipe. A native ingestion
CLI run also emitted its actual unchanged-document and supersession outcomes.
Subsequent model-context assembly and token usage are **not observed** by this
tooling; byte counts must not be relabeled as those measurements.

Journals contain queries and returned data, including source quotes. Treat them
as sensitive as the indexed corpus. Host exports exclude unrelated events and
assistant reasoning.

Each agent writes only its own `answers-kg.json` or `answers-markdown.json` in
the run directory, then calls its wrapper with `submit`. Submission checks
structure, not correctness; no gold feedback is returned.

```bash
uv run python benchmarks/work_memory/evaluate.py score --run /path/to/new-run
```

The scorer requires both submissions, verifies frozen inputs and submitted
answer hashes, and writes `scores.json`. Review the short natural-language
answers separately: exact fact scoring does not detect arbitrary unsupported
claims outside the requested fact fields.

## Scoring and telemetry

Report these independently rather than collapsing them into an opaque score:

- Canonical facts and abstention.
- Exact citation validity and required evidence coverage.
- Full-question pass (all of the above).
- Actual data-tool calls, failures, returned UTF-8 bytes, and tool elapsed time.

All tool requests and returned payloads are saved in per-arm JSONL journals,
including failed operations. Raw agent answers, the source snapshot, ingestion
trace, and scores remain in the run directory. Shared `info`/`submit` calls and
one-time KG ingestion are excluded from data-tool totals; ingestion cost is
reported separately.

Response bytes are not model token usage. Tool latency excludes agent reasoning.
Bytes count complete serialized JSON payloads, not necessarily successfully
delivered stdout. They do not measure model-visible context.
Runtime defaults do not independently prove the model ID used for each agent.
A single run on a small, structured corpus cannot establish broad superiority,
scalability, or real-world semantic retrieval quality. If gold, corpus, prompts,
or tools change, start a new run rather than changing the oracle mid-comparison.

## Recorded Atlas pilot

The [archived pilot](results/atlas-pilot/scores.json) includes frozen inputs,
both submitted answers, and complete tool journals. Both agents reported
following the access protocol; neither could independently identify its model.
The later [host-event audit](results/atlas-pilot/host-telemetry.json) records
`gpt-6-astra` on both arms' tool calls.
The parent reviewed the natural-language answers and found no unsupported
extra claims or stale-current-fact assertions.

| Metric | KG tools | Markdown tools |
| --- | ---: | ---: |
| Full-question passes | 10/10 | 10/10 |
| Data-tool calls | 12 | 1 |
| Serialized response bytes | 83,036 | 4,931 |
| Failed data-tool calls | 0 | 0 |

The complete corpus is only 3,931 source bytes. The Markdown agent read all
twelve documents in one batch. The KG journal contains three identical
`status Atlas` requests and two `record-state` requests, among its twelve
operations. These are observed tool actions, not hypothetical code-path costs.
Response bytes are not the amount of text actually seen by the model.

The actual host events confirm that the KG agent chained three commands
whose combined 23.6 KB output spilled, leaving only a preview. A subsequent
pretty-printing attempt used an unavailable `python` executable and broke the
pipe; the host recorded process exit 127 even though its own tool-success flag
was true. The final standalone status call returned inline. The Markdown batch
read also returned inline. All 17 wrapper invocations (including info and
submission) correlate with the 11 actual host calls; no host completion is
missing. These observations no longer rely on agent recollection.

Consequently, the 12-versus-1 call difference includes avoidable orchestration
and delivery problems. It must not be attributed wholly to KG retrieval.
The original journals' zero operation failures do not mean that all responses
were delivered successfully; the new delivery fields did not exist in that run.

No answer-correctness defect was exposed, so this run does not justify a
corpus-specific fix or a claim that KG reasoning is superior. The KG was more
expensive at the tool boundary on this tiny dataset. Follow-up work should
first rerun the same comparison with the corrected transport protocol, then
use a larger corpus where reading every document is no longer cheap. Do not
claim an efficiency improvement until that new run is measured.

## Clean Atlas rerun

The [clean rerun](results/atlas-clean-rerun/scores.json) uses the same frozen
sources, manifest, questions, and gold as the pilot, with two fresh agents.
Each tool call contains one wrapper invocation, without chaining or pipes.

| Metric | KG tools | Markdown tools |
| --- | ---: | ---: |
| Full-question passes | 10/10 | 10/10 |
| Data-tool calls | 6 | 1 |
| Serialized response bytes | 29,060 | 4,931 |
| Failed data-tool calls | 0 | 0 |
| Observed stdout-delivery failures | 0 | 0 |

The KG agent used one status request, one source-context request, and four
searches; it did not repeat the status request. Markdown again read the entire
small corpus in one batch. Both passed fact, abstention, citation validity, and
required-evidence checks. Parent review found no unsupported natural-language
claims or stale facts asserted as current; this review was not blinded.

The [actual host audit](results/atlas-clean-rerun/host-telemetry.json) correlates
all 11 wrapper invocations, including info and submission, with 11 completed
host calls. There were no recorded spills or nonzero process exits. Both arms'
host events identify `gpt-6-astra`. A separate
[verification record](results/atlas-clean-rerun/verification.json) confirms that
every complete JSON payload returned by the host matches its wrapper response
hash. Later model-context assembly and token counts remain unobserved.

The lower KG call and byte counts are observed rerun results, not an isolated
causal estimate of the transport fix: fresh agents can choose different query
strategies. KG still used more calls and serialized bytes than Markdown on this
3,931-byte corpus. This confirms a clean comparison, not an efficiency win.

The next experiment is a larger corpus with longer history, multiple projects,
and plausible unrelated material, with questions and expected answers frozen
before either agent runs. Use observed failures to choose generic interface or
retrieval improvements rather than adding graph features speculatively.

## Larger comparisons

Custom corpora, questions, and gold are frozen together:

```bash
uv run python benchmarks/work_memory/evaluate.py prepare \
  --output /path/to/new-run \
  --manifest /path/to/scenario/corpus.yml \
  --questions /path/to/scenario/questions.json \
  --gold /path/to/scenario/gold.json \
  --response-limit-bytes 12000
```

New runs keep a private, hashed copy of gold in the run directory; neither arm's
data tools expose it. Old runs without run-local gold retain the original scorer
behavior. Questions and gold must have matching unique question IDs.

The optional response limit is a **shared benchmark transport adapter**, not a
native KG optimization. Large responses are saved intact under `responses/ARM/`
using their content hash. The returned envelope identifies the complete result,
its size, and its fields. Both arms can retrieve selected JSON-pointer fields
and list/string pages with `response ID --field /FIELD --offset N --limit N`.
`response-fields ID` lists object field names. Pages report total length and the
next offset; strings use character offsets, preserving original source text.
Nothing is silently dropped, and all response-fetch calls count as data-tool
calls. Archive cached responses with the journals to preserve the full evidence.

In bounded mode, `info` previews at most 20 source paths and seed entities and
reports their full counts. Both arms have `catalog sources|entities [REGEX]
--offset N --limit N` to discover more. This avoids making a large inventory
itself an oversized prerequisite. Existing unbounded runs remain supported.

Do not compare these call counts directly with an unpaged run as if only corpus
size changed. Record the transport setting, native execution sizes, observed
delivery, and agent-selected paging costs separately.

Generate the expanded synthetic scenario with:

```bash
uv run python benchmarks/work_memory/expanded.py --output /path/to/scenario
```

The generator refuses to overwrite an existing directory. It preserves the
original twelve documents and adds dated work history, explicit task/decision
replacement chains, ownership changes, numerical comparisons, and other projects.
The generated manifest, questions, and gold can be passed to `prepare` above.
Fixture-specific names remain benchmark data, not KG engine logic.

## Recorded expanded comparison

The [expanded run](results/atlas-expanded-v1/scores.json) contains 483 documents,
475,964 source bytes, 13 projects, and 276 days of history. It includes 39 added
focal-history documents and 14 predeclared questions. Both fresh agents used
the same 12,000-byte, lossless paging adapter.

| Metric | KG tools | Markdown tools |
| --- | ---: | ---: |
| Canonical factual answers correct | 14/14 | 14/14 |
| Abstention correct | 14/14 | 14/14 |
| Questions with valid exact citations | 14/14 | 14/14 |
| Original frozen full-question passes | 12/14 | 13/14 |
| Data-tool calls, including page fetches | 23 | 8 |
| Serialized response bytes | 84,381 | 52,460 |
| Failed data-tool calls | 0 | 0 |

The full-question scores expose two **gold-rule defects**, not factual errors:

- `ledger-lifecycle` required five literal YAML date excerpts. Those dates are
  available to KG as source event metadata, but frontmatter is not a source
  anchor and cannot be quoted by its source-range tools. This defect was
  [recorded before reviewing answers or scoring](results/atlas-expanded-v1/pre-score-audit.json).
  KG correctly reported every date, owner, state, and due date and cited each
  task version, but failed these five extra literal-quote checks.
- `unknown-approval` only accepted the original pending-approval note. Both
  agents instead cited the newer, explicit statement that final production
  approval is not recorded. The answers are supported; the source whitelist was
  stale after extending the scenario. This defect was identified after scoring.

Original gold and scores are preserved unchanged. Future fixture generation
corrects those expectations; no corrected score is substituted into this run.
The [parent review](results/atlas-expanded-v1/review.json) found no unsupported
extra claims or stale facts asserted as current. Every submitted quote was
present in data actually returned to that agent, not merely in the corpus.

The [host audit](results/atlas-expanded-v1/host-telemetry.json) correlates all 35
wrapper calls, including common info/submission calls, with completed host
events. Both arms were tagged `gpt-6-astra`; no agent output spilled and no
process or stdout-delivery failure was observed. Every returned JSON payload
matches its journal hash. The [access audit](results/atlas-expanded-v1/verification.json)
also confirms that the only additional agent tools wrote their answer files.

KG used one status call, eight page fetches, three source-context calls, and
eleven searches. Five page fetches retrieved separate sections of that status
response. Markdown used a search, a path catalog, two batch reads, and four page
fetches; project-named paths let it request 51 documents in full rather than all
483. Its initial regex search still scanned all 483 files internally.

**Interpretation:** current-state ingestion and retrieval held up, but this run
does not show an agent-efficiency advantage. KG still required more calls and
serialized bytes. The concrete interface problem is fragmented, verbose evidence
delivery, not an observed need for additional graph features. A compact,
batchable evidence interface is the next candidate to measure, not a proven fix.

This remains one synthetic, structured experiment with easily filterable paths.
It does not establish performance on messy personal work, and a KG-only versus
Markdown-only comparison does not measure the benefit of adding KG tools to an
agent that retains ordinary Markdown access. Neither byte counts nor tool
elapsed times measure model tokens or end-to-end reasoning cost.
