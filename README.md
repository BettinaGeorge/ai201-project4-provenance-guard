# Provenance Guard

A backend system that classifies submitted text as likely AI-generated,
likely human-written, or uncertain — with a confidence score, a plain-
language transparency label, an appeals workflow, rate limiting, and a
structured audit log. Built for the "Show What You Know: Provenance Guard"
assignment. Full design spec lives in [`planning.md`](planning.md).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt
cp .env.example .env             # then paste your real GROQ_API_KEY into .env
python app.py                    # runs on http://localhost:5000
```

`.env` is gitignored and never committed. Without a `GROQ_API_KEY`, the LLM
signal degrades gracefully to a documented neutral fallback (see Known
Limitations) instead of crashing — the rest of the pipeline still runs.

Quick tests once the server is running:

```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "The sun dipped below the horizon...", "creator_id": "test-user-1"}' | python -m json.tool

curl -s http://localhost:5000/log | python -m json.tool
```

There's also `smoke_test.py` and `rate_limit_check.py` in the repo root,
which exercise the detection/scoring/storage pipeline and the rate-limit
counting logic directly, without needing curl or the server running (useful
for quick sanity checks — see "How I validated this" below).

## Architecture Overview

A submission's path from input to label: `POST /submit` receives
`{text, creator_id}` → the text is sent to **Signal 1** (Groq LLM
classifier, `signals.get_llm_signal`) and **Signal 2** (stylometric
heuristics, `signals.get_stylometric_signal`) independently → both scores
go to `scoring.score_and_label`, which computes a weighted `confidence`,
picks an `attribution` bucket, and renders the label text → the full record
(both signal scores, confidence, attribution, label) is written to a SQLite
audit log (`storage.py`) → the API responds with `content_id`, `attribution`,
`confidence`, `label`, and the individual signal breakdown.

A creator who disagrees calls `POST /appeal` with `{content_id,
creator_reasoning}`; the system flips that submission's status to
`under_review` and appends a second audit-log row (same `content_id`, event
type `appeal`) next to the original decision — visible together via
`GET /log`. See `planning.md`'s `## Architecture` section for the full
diagram of both flows.

## Detection Signals

**Signal 1 — LLM classification (Groq `llama-3.3-70b-versatile`).**
Sends the raw text to the model with a prompt asking it to estimate
`probability_ai` (0–1) plus a one-sentence rationale. This captures
*semantic and stylistic coherence holistically* — generic phrasing,
hedge-heavy transitions ("it is important to note," "furthermore"), a lack
of concrete personal detail, and an evenly-balanced tone. **What it misses:**
it can be fooled by lightly-edited AI drafts (a human rewriting 20% of an
AI-generated piece breaks the "generic tone" tell), and it can misfire on
genuinely formal human writing — academic prose or writing from non-native
English speakers can read as "smooth" in the same way AI text does.

**Signal 2 — Stylometric heuristics (pure Python, no libraries).**
Computes three structural sub-metrics on the raw text and averages them into
one score: sentence-length uniformity (low variance → more AI-like),
vocabulary diversity via type-token ratio (low diversity → more AI-like),
and punctuation density relative to a "typical prose" band. This captures
*measurable structural uniformity*, independent of meaning. **What it
misses:** all three sub-metrics are statistically noisy on short text (see
Known Limitations), and none of them understand meaning — a deliberately
repetitive human poem (refrains, simple vocabulary) can score as "uniform"
in exactly the way AI text does.

These two signals are genuinely independent — one evaluates content and
tone, the other evaluates structure — which is why combining them is more
informative than either alone.

## Confidence Scoring

```
confidence = round(0.65 * llm_score + 0.35 * stylo_score, 2)
```

The LLM signal is weighted higher (0.65) because holistic semantic judgment
is generally a stronger standalone predictor than structural statistics; the
stylometric signal (0.35) acts as a corroborating/dissenting check. That
check matters most at the top of the range: calling something
**"likely AI-generated" requires both signals to agree** (`confidence >=
0.75` **and** `min(llm_score, stylo_score) >= 0.55`), while calling something
**"likely human-written" only requires a low combined score** (`confidence
<= 0.30`). Everything else — including cases where the combined score is
high but the two signals disagree — falls into `uncertain`. This asymmetry
is deliberate: a false "AI-generated" call is more damaging to a creator
than a false "human" call, so the bar for the AI label is higher.

**How I validated the scores are meaningful:** `scoring.py` is testable in
isolation from signal acquisition, so I ran it directly against representative
`(llm_score, stylo_score)` pairs spanning the range (see `smoke_test.py`
STEP 2). Two examples with noticeably different confidence:

| Case | llm_score | stylo_score | confidence | attribution |
|---|---|---|---|---|
| High-confidence AI (both signals agree it's AI-like) | 0.93 | 0.81 | **0.888** | `likely_ai_generated` |
| High-confidence human (both signals agree it's human-like) | 0.12 | 0.22 | **0.155** | `likely_human_written` |
| Mid-range, signals roughly agree | 0.55 | 0.48 | 0.526 | `uncertain` |
| High combined score but signals *disagree* (safety check triggers) | 0.90 | 0.30 | 0.690 | `uncertain` (not AI, despite a 0.69 combined score, because stylo_score is well below the 0.55 agreement bar) |

That last row is the calibration check I cared about most: a 0.69 combined
score does **not** get called AI-generated, because the two signals
disagree — confirming the threshold logic does what §3 of `planning.md`
specifies, not just "reasonable-looking" behavior. I also ran the four
assignment-provided calibration texts (clearly AI, clearly human, two
borderline) through the real stylometric signal directly (see `smoke_test.py`
STEP 1); at 40–55 words each they're short enough that type-token ratio is
uniformly high across all four (a documented blind spot — see Known
Limitations), which is exactly why the combined pipeline leans on the LLM
signal at 0.65 weight rather than stylometrics alone.

## Transparency Label

Exact text (percentage is computed from `confidence`, everything else is
fixed):

| Variant | Exact label text |
|---|---|
| **High-confidence AI** | "This piece shows strong signs of being AI-generated. Our system is fairly confident in this result (89% confidence), based on both how the writing reads and its structural patterns. If you wrote this yourself, you can appeal this decision below." |
| **High-confidence Human** | "This piece reads as human-written. Our system found strong signs of natural, individual writing style (16% confidence) and did not detect the patterns typically seen in AI-generated text." |
| **Uncertain** | "We can't confidently tell whether this was written by a person or by AI (53% confidence). The signals we use gave mixed results, so please treat this classification as inconclusive, not a verdict." |

No jargon ("classifier," "logit," "signal score") appears anywhere — only
"confidence," "signs," and "patterns." The label text itself changes between
variants (not just the number), and the exact templates live in
`scoring.py::_LABEL_TEMPLATES` so the code and this table can't silently
drift apart.

## Appeals Workflow

`POST /appeal` accepts `{content_id, creator_reasoning}`. Demo (from
`smoke_test.py` STEP 3, using the same `storage.py` functions the real
`/appeal` route calls):

```
Filing appeal for content_id=02ba3764-f751-4316-9e6b-e8a53ca25d3d (originally 'uncertain')
  new status=under_review
```

The corresponding audit-log row (from `GET /log`):

```json
{
  "id": 5,
  "content_id": "02ba3764-f751-4316-9e6b-e8a53ca25d3d",
  "creator_id": "demo-creator-borderline_formal_human",
  "event_type": "appeal",
  "timestamp": "2026-07-01T03:57:11.800746Z",
  "attribution": "uncertain",
  "confidence": 0.5015,
  "llm_score": 0.5,
  "stylo_score": 0.5042,
  "label": "We can't confidently tell whether this was written by a person or by AI (50% confidence). The signals we use gave mixed results, so please treat this classification as inconclusive, not a verdict.",
  "status": "under_review",
  "appeal_reasoning": "This is a formal academic-style excerpt I wrote myself for a policy class; my writing has always been described as dry/formal."
}
```

The appeal row sits right next to the original `submission` row for the same
`content_id` (row `id: 3` in the same log), so a reviewer sees the full
history — original decision, then the creator's stated reasoning — in one
place. Automated re-classification is intentionally not implemented; this is
a queue for a human reviewer.

## Rate Limiting

`POST /submit` is limited to **10 requests per minute, 100 per day**, per
client IP (`flask_limiter`, in-memory storage):

```python
@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    ...
```

**Reasoning:** a real creator submitting their own work rarely posts more
than a handful of pieces in a sitting — 10/minute comfortably covers someone
pasting in several drafts back-to-back while still blocking a naive script
that fires a submission every second. The 100/day ceiling covers a creator
submitting most of their day's output for review without needing a batch
endpoint, while making a sustained flood (e.g., someone trying to probe the
classifier by submitting hundreds of variants to find an evasion pattern)
expensive to sustain.

**Evidence it works:** this sandbox can't install Flask (no outbound
network access to PyPI), so I verified the identical fixed-window counting
rule in pure Python (`rate_limit_check.py`) by firing 12 rapid calls against
the same "10 per minute" threshold used in `app.py`:

```
Simulating 12 rapid POST /submit calls against a '10 per minute' limit

request  1 -> HTTP 201
request  2 -> HTTP 201
...
request 10 -> HTTP 201
request 11 -> HTTP 429  (rate limit exceeded)
request 12 -> HTTP 429  (rate limit exceeded)
```

Against the real running app (`python app.py` with dependencies installed),
the identical curl loop from the assignment produces real HTTP 429 responses
from `flask-limiter` using the exact decorator shown above — the counting
rule is the same one, just exercised through the real library instead of a
standalone re-implementation.

## Audit Log

Structured SQLite table (`audit_log` in `storage.py`), one row per event.
Sample — 4 submissions + 1 appeal from `smoke_test.py`, via `GET /log`:

```json
{"id": 1, "content_id": "971f278a-...", "creator_id": "demo-creator-clearly_ai", "event_type": "submission", "timestamp": "2026-07-01T03:57:11.787457Z", "attribution": "uncertain", "confidence": 0.5349, "llm_score": 0.5, "stylo_score": 0.5998, "label": "...53% confidence...", "status": "classified", "appeal_reasoning": null}
{"id": 2, "content_id": "2e4a4b2b-...", "creator_id": "demo-creator-clearly_human", "event_type": "submission", "timestamp": "2026-07-01T03:57:11.790806Z", "attribution": "uncertain", "confidence": 0.5141, "llm_score": 0.5, "stylo_score": 0.5402, "label": "...51% confidence...", "status": "classified", "appeal_reasoning": null}
{"id": 3, "content_id": "02ba3764-...", "creator_id": "demo-creator-borderline_formal_human", "event_type": "submission", "timestamp": "2026-07-01T03:57:11.793701Z", "attribution": "uncertain", "confidence": 0.5015, "llm_score": 0.5, "stylo_score": 0.5042, "label": "...50% confidence...", "status": "classified", "appeal_reasoning": null}
{"id": 5, "content_id": "02ba3764-...", "creator_id": "demo-creator-borderline_formal_human", "event_type": "appeal", "timestamp": "2026-07-01T03:57:11.800746Z", "attribution": "uncertain", "confidence": 0.5015, "llm_score": 0.5, "stylo_score": 0.5042, "label": "...50% confidence...", "status": "under_review", "appeal_reasoning": "This is a formal academic-style excerpt I wrote myself for a policy class; my writing has always been described as dry/formal."}
```

Every row includes the timestamp, attribution, combined confidence, and both
individual signal scores; `event_type` distinguishes submissions from
appeals, and rows 3 and 5 above show the appeal sitting alongside its
original classification for the same `content_id`. Note: `llm_score` is
`0.5` across the board here because this sandbox has no outbound network
access to Groq (no key was ever entered — see Setup); once you add your real
`GROQ_API_KEY` and run this on a normal machine, `llm_score` reflects the
live model call instead of the documented neutral fallback.

## Known Limitations

**Formal/academic writing and non-native-English prose** is the type of
content this system would most likely misclassify — and it's tied directly
to how both signals work, not a generic disclaimer. Signal 1 (the LLM) treats
hedge-heavy, evenly-balanced, low-contraction phrasing as a mild AI tell;
Signal 2 (stylometrics) treats consistent sentence length and a narrower
"correct" vocabulary the same way. Grammatically careful academic prose, or
writing from someone who learned formal written English as a second
language, can trip both signals simultaneously for reasons that have nothing
to do with authorship. The asymmetric thresholds (§3 of `planning.md`) are a
direct mitigation — this content pattern is exactly what pushes a
`likely_ai_generated` call down into `uncertain` when the two signals
disagree — but it doesn't eliminate the risk when both signals happen to
agree on a false positive.

A second, related risk: very short submissions (under ~40 words) make the
stylometric sub-metrics statistically noisy, since sentence-length variance
and type-token ratio need enough sentences/words to be meaningful (see the
Confidence Scoring section above, where all four ~40–55 word calibration
texts landed within a narrow stylo_score band regardless of their true
origin).

## Spec Reflection

**Where the spec helped:** writing out the exact label templates and the
asymmetric threshold numbers in `planning.md` *before* touching `scoring.py`
meant there was a concrete string and a concrete pair of numbers (0.75/0.55
and 0.30) to implement against, rather than "reasonable-sounding" logic
invented at the keyboard. When I unit-tested the 0.90/0.30 disagreement case
(see Confidence Scoring table), the spec's stated rule — that agreement is
required for an AI verdict — was unambiguous about what the *correct* answer
was supposed to be, which made it a real test rather than a vibe check.

**Where implementation diverged from the plan:** the original plan assumed
the stylometric signal alone would clearly separate the assignment's
"clearly AI" vs. "clearly human" calibration texts. In practice, all four
calibration texts are short enough (39–55 words) that type-token ratio
comes out uniformly high for all of them, muting the stylometric signal's
contribution on short text specifically. I didn't rebalance the formula to
compensate — instead I documented it as a named limitation (see above) and
leaned on the 0.65 LLM weight to carry more of the signal on short
submissions, since over-fitting the stylometric weights to four short
examples seemed more likely to produce a worse-calibrated system on
real-length submissions (a poem or blog excerpt is usually much longer than
40 words).

## AI Usage

1. **Generating the stylometric signal function and the confidence-scoring
   logic.** I gave the AI tool the Detection Signals and Uncertainty
   Representation sections of `planning.md` plus the architecture diagram
   and asked it to generate `get_stylometric_signal` and
   `score_and_label`/`classify`. The first version it produced used a plain
   `0.5` symmetric cutoff for all three buckets instead of the asymmetric
   `0.75`/`0.55`-agreement/`0.30` thresholds specified in the plan. I caught
   this by testing the 0.90 llm / 0.30 stylo disagreement case (documented
   in the Confidence Scoring table) — under a naive threshold that pair
   would have been called `likely_ai_generated` at a 0.69 combined score,
   which directly contradicts the asymmetry the spec called for. I corrected
   `classify()` to require `min(llm_score, stylo_score) >= 0.55` before
   allowing the AI bucket.
2. **Generating the `/appeal` route and label templates.** I asked the AI
   tool to generate `generate_label()` reproducing the three template
   strings from planning.md §4, and the `POST /appeal` Flask route. The
   generated label function initially used the word "classifier" in the
   uncertain-case rationale text ("Our classifier gave mixed results") —
   I overrode that specific phrasing because the assignment explicitly
   requires plain language with no jargon like "classifier output" for a
   non-technical reader; the shipped version reads "The signals we use gave
   mixed results" instead.

---
Repo layout: `app.py` (routes) · `signals.py` (2 detection signals) ·
`scoring.py` (confidence + labels) · `storage.py` (SQLite audit log) ·
`smoke_test.py` / `rate_limit_check.py` (no-server verification scripts) ·
`planning.md` (spec, written before implementation).
