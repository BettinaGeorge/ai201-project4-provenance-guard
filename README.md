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

**How I validated the scores are meaningful:** I ran the live server
(`python app.py`, real `GROQ_API_KEY`) and posted contrasting real text to
`POST /submit`. Two actual responses with noticeably different confidence:

| Case | Text | llm_score | stylo_score | confidence | attribution |
|---|---|---|---|---|---|
| High-confidence AI | "Artificial intelligence represents a transformative paradigm shift..." (generic, hedge-heavy AI-style paragraph) | 0.9 | 0.5998 | **79%** | `likely_ai_generated` |
| Lower-confidence / boundary case | "ok so i finally tried that new ramen place downtown and honestly?..." (casual venting, clearly human) | 0.2 | 0.4998 | **30%** | `uncertain` |

The second row is a genuinely interesting real result, not a cherry-picked
one. The LLM signal correctly read the casual text as human (`llm_score=0.2`),
but the stylometric signal came back nearly neutral (`0.4998`) because the
text is short enough (55 words) that its type-token ratio is high regardless
of true authorship — the exact blind spot documented below. That neutral
stylometric score pulled the combined confidence to 30.49%, missing the
`<=0.30` cutoff for `likely_human_written` by 0.0049 and landing in
`uncertain` instead. Submitting a near-identical version of the same text a
second time (`llm_score=0.1` that run) did clear the human threshold — 24%
confidence, `likely_human_written` — showing the system is genuinely
sensitive right at its own boundary, exactly where you'd want to interrogate
a scoring function rather than trust it blindly.

I also unit-tested `scoring.py` directly against a deliberately adversarial
pair — `llm_score=0.90, stylo_score=0.30` — to confirm the *disagreement*
safety check works: combined confidence is 0.69 (high), but because
`stylo_score` is below the 0.55 agreement bar, the result is `uncertain`,
not `likely_ai_generated`. That confirms the threshold logic in §3 of
`planning.md` does what it claims, not just something "reasonable-looking."

## Transparency Label

Exact text (percentage is computed from `confidence`, everything else is
fixed):

| Variant | Exact label text |
|---|---|
| **High-confidence AI** | "This piece shows strong signs of being AI-generated. Our system is fairly confident in this result (79% confidence), based on both how the writing reads and its structural patterns. If you wrote this yourself, you can appeal this decision below." |
| **High-confidence Human** | "This piece reads as human-written. Our system found strong signs of natural, individual writing style (24% confidence) and did not detect the patterns typically seen in AI-generated text." |
| **Uncertain** | "We can't confidently tell whether this was written by a person or by AI (30% confidence). The signals we use gave mixed results, so please treat this classification as inconclusive, not a verdict." |

(Percentages above are real numbers from a live run of the server, not
placeholders — see Confidence Scoring for the exact text/score pairs they
came from.)

No jargon ("classifier," "logit," "signal score") appears anywhere — only
"confidence," "signs," and "patterns." The label text itself changes between
variants (not just the number), and the exact templates live in
`scoring.py::_LABEL_TEMPLATES` so the code and this table can't silently
drift apart.

## Appeals Workflow

`POST /appeal` accepts `{content_id, creator_reasoning}`. Demo, from a real
run against the live server (`content_id` from the "ramen review" submission
in the Confidence Scoring section, which landed as `uncertain` at 30%):

```bash
curl -s -X POST http://localhost:5001/appeal -H "Content-Type: application/json" -d '{
  "content_id": "a37e750e-bb17-4c96-a6c9-58effa1e377b",
  "creator_reasoning": "This is just a casual venting post about ramen, obviously written by me."
}' | python -m json.tool
```

```json
{
    "content_id": "a37e750e-bb17-4c96-a6c9-58effa1e377b",
    "message": "Appeal received and logged. A human reviewer will examine this classification.",
    "status": "under_review"
}
```

The corresponding audit-log row (from `GET /log`):

```json
{
    "id": 5,
    "content_id": "a37e750e-bb17-4c96-a6c9-58effa1e377b",
    "creator_id": "demo-2",
    "event_type": "appeal",
    "timestamp": "2026-07-01T05:39:41.316872Z",
    "attribution": "uncertain",
    "confidence": 0.3049,
    "llm_score": 0.2,
    "stylo_score": 0.4998,
    "label": "We can't confidently tell whether this was written by a person or by AI (30% confidence). The signals we use gave mixed results, so please treat this classification as inconclusive, not a verdict.",
    "status": "under_review",
    "appeal_reasoning": "This is just a casual venting post about ramen, obviously written by me."
}
```

The appeal row (`id: 5`) sits right next to the original `submission` row for
the same `content_id` (`id: 4` in the same log — see Audit Log below), so a
reviewer sees the full history — original decision, then the creator's
stated reasoning — in one place. Automated re-classification is
intentionally not implemented; this is a queue for a human reviewer.

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

**Evidence it works:** ran against the live server (`python app.py`, real
`flask-limiter`), firing 12 rapid `POST /submit` calls in a loop:

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "request $i -> %{http_code}\n" -X POST http://localhost:5001/submit \
    -H "Content-Type: application/json" \
    -d '{"text": "This is a rate limit test submission.", "creator_id": "ratelimit-test"}'
done
```

```
request 1 -> 201
request 2 -> 201
request 3 -> 201
request 4 -> 201
request 5 -> 201
request 6 -> 201
request 7 -> 201
request 8 -> 201
request 9 -> 201
request 10 -> 201
request 11 -> 429
request 12 -> 429
```

Exactly as designed: the first 10 requests within the one-minute window
succeed (`201`), and requests 11–12 are rejected with real HTTP `429`
responses from `flask-limiter`. (`rate_limit_check.py` also exists in the
repo as a standalone, no-dependencies re-implementation of the same
fixed-window counting rule, used earlier in development before Flask was
installed — the numbers above are from the real library.)

## Audit Log

Structured SQLite table (`audit_log` in `storage.py`), one row per event.
This is the real output of `GET /log` from a live run against the running
server, real Groq calls included (`llm_score` values below are actual model
outputs, not fallback values):

```json
{"id": 5, "content_id": "a37e750e-bb17-4c96-a6c9-58effa1e377b", "creator_id": "demo-2", "event_type": "appeal", "timestamp": "2026-07-01T05:39:41.316872Z", "attribution": "uncertain", "confidence": 0.3049, "llm_score": 0.2, "stylo_score": 0.4998, "label": "We can't confidently tell whether this was written by a person or by AI (30% confidence)...", "status": "under_review", "appeal_reasoning": "This is just a casual venting post about ramen, obviously written by me."}
{"id": 4, "content_id": "a37e750e-bb17-4c96-a6c9-58effa1e377b", "creator_id": "demo-2", "event_type": "submission", "timestamp": "2026-07-01T05:37:59.246017Z", "attribution": "uncertain", "confidence": 0.3049, "llm_score": 0.2, "stylo_score": 0.4998, "label": "We can't confidently tell whether this was written by a person or by AI (30% confidence)...", "status": "classified", "appeal_reasoning": null}
{"id": 3, "content_id": "666e6c95-4905-4076-9f02-153f61b7cf91", "creator_id": "demo-1", "event_type": "submission", "timestamp": "2026-07-01T05:37:58.967160Z", "attribution": "likely_ai_generated", "confidence": 0.7949, "llm_score": 0.9, "stylo_score": 0.5998, "label": "This piece shows strong signs of being AI-generated... (79% confidence)...", "status": "classified", "appeal_reasoning": null}
{"id": 2, "content_id": "e677bf21-990f-4a52-bf32-84c3419e3c87", "creator_id": "demo-2", "event_type": "submission", "timestamp": "2026-07-01T05:37:21.920724Z", "attribution": "likely_human_written", "confidence": 0.2399, "llm_score": 0.1, "stylo_score": 0.4998, "label": "This piece reads as human-written... (24% confidence)...", "status": "classified", "appeal_reasoning": null}
{"id": 1, "content_id": "65d03677-2ef1-4ff0-8150-9211e31da345", "creator_id": "demo-1", "event_type": "submission", "timestamp": "2026-07-01T05:37:21.401608Z", "attribution": "likely_ai_generated", "confidence": 0.7949, "llm_score": 0.9, "stylo_score": 0.5998, "label": "This piece shows strong signs of being AI-generated... (79% confidence)...", "status": "classified", "appeal_reasoning": null}
```

Every row includes the timestamp, attribution, combined confidence, and both
individual signal scores; `event_type` distinguishes submissions from
appeals. This one log capture happens to show all three label variants live
(`likely_ai_generated` at 79%, `likely_human_written` at 24%, `uncertain` at
30%), plus rows `id: 4` and `id: 5` showing the appeal sitting directly
alongside its original classification for the same `content_id`.

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
