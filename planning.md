# Provenance Guard — planning.md

## 1. Architecture Narrative

A single piece of submitted text travels through six components before a user
ever sees a result:

1. **Flask route `POST /submit`** receives `{text, creator_id}`, generates a
   `content_id` (UUID), and hands the raw text to both detection signals.
2. **Signal 1 — LLM classifier (Groq, `llama-3.3-70b-versatile`)** reads the
   raw text holistically and returns a `probability_ai` score (0–1) plus a
   one-line rationale. This captures semantic/stylistic coherence — does the
   text *read* like something a model would produce.
3. **Signal 2 — Stylometric heuristics (pure Python)** computes structural
   statistics on the same raw text (sentence-length variance, type-token
   ratio, punctuation density) and returns a `stylo_score` (0–1). This
   captures measurable uniformity, independent of meaning.
4. **Confidence Scorer** combines `llm_score` and `stylo_score` into one
   `confidence` value using a weighted formula, then applies asymmetric
   thresholds (see §3) to decide `attribution` (`likely_ai_generated`,
   `likely_human_written`, or `uncertain`).
5. **Label Generator** maps `(attribution, confidence)` to one of three fixed
   label templates (see §3), filling in the confidence percentage.
6. **Audit Logger** writes a structured row (SQLite) containing the
   `content_id`, both raw signal scores, the combined confidence, the
   attribution, the label, and a timestamp — *before* the response is
   returned to the client.

The Flask route then returns `{content_id, attribution, confidence, label,
signals: {llm_score, stylo_score}}` to the caller.

**Appeal flow:** a creator who disagrees calls `POST /appeal` with
`{content_id, creator_reasoning}`. The system looks up the original
submission, flips its `status` to `under_review`, and writes a *second*
audit-log row referencing the same `content_id` so the appeal sits next to
the original decision in the log. No re-classification happens automatically.

## 2. Detection Signals

| | Signal 1: LLM Classification | Signal 2: Stylometric Heuristics |
|---|---|---|
| **Tool** | Groq `llama-3.3-70b-versatile` | Pure Python (no libraries) |
| **What it measures** | Whether the text *reads* as something an LLM would generate — generic phrasing, hedge-heavy transitions ("it is important to note," "furthermore"), lack of concrete specific detail, overly balanced/neutral tone | Structural uniformity: sentence-length variance, type-token ratio (vocabulary diversity), punctuation density |
| **Output format** | `probability_ai` float 0–1 + short text rationale | Three sub-metrics normalized and averaged into a single float 0–1 |
| **Why it differs between human/AI text** | AI models default to hedged, evenly-paced, generically "helpful" phrasing even when prompted for creativity; humans digress, use idiosyncratic word choices, and take stances | AI text tends toward consistent sentence length and a narrower, more "average" vocabulary; human writing varies more — short punchy sentences next to long rambling ones, occasional invented or unusual word choices |
| **Blind spots** | Can be fooled by lightly-edited AI text (a human rewriting 20% of an AI draft breaks the "generic tone" signal); can misfire on genuinely formal human writing (academic prose, non-native English speakers) that happens to sound "smooth" | Needs enough text to be statistically meaningful — very short submissions (1–2 sentences) produce noisy variance/TTR; can't detect anything about meaning or factual grounding, so a deliberately repetitive human poem can score as "uniform" like AI text |

**Combining signals into one confidence score:**

```
confidence = round(0.65 * llm_score + 0.35 * stylo_score, 2)
```

The LLM signal is weighted higher (0.65) because it evaluates meaning and
holistic style, which is generally a stronger standalone predictor than
structural statistics alone. The stylometric signal (0.35) acts as a
corroborating/dissenting check, which matters most at the extremes (see §3).

## 3. Uncertainty Representation

`confidence` is always the single combined float from the formula above —
it is shown to the user directly (as a percentage) regardless of which label
bucket it falls in. What *changes* between buckets is the `attribution` label
and the label text, not the number itself.

**Design intent:** a false positive (calling a human's writing "AI-generated")
is more damaging to a creator than a false negative, so the thresholds are
**asymmetric**. Reaching high-confidence-AI requires *both* signals to agree;
reaching high-confidence-human only requires the combined score to be low.

```
if confidence >= 0.75 and min(llm_score, stylo_score) >= 0.55:
    attribution = "likely_ai_generated"        # HIGH CONFIDENCE AI
elif confidence <= 0.30:
    attribution = "likely_human_written"       # HIGH CONFIDENCE HUMAN
else:
    attribution = "uncertain"                  # includes disagreement cases,
                                                # e.g. confidence 0.80 but
                                                # stylo_score only 0.40 —
                                                # signals disagree, so we
                                                # don't commit to "AI"
```

Concretely: `0.51` and `0.95` cannot land in the same bucket — `0.51` is
always `uncertain`, `0.95` is `likely_ai_generated` only if the stylometric
signal also agrees (>= 0.55); if it doesn't, `0.95` still drops to
`uncertain` rather than asserting AI authorship on a single signal.

## 4. Transparency Label Design

Exact text, filled in with the computed confidence percentage:

| Variant | Exact label text |
|---|---|
| **High-confidence AI** | `"This piece shows strong signs of being AI-generated. Our system is fairly confident in this result ({confidence}% confidence), based on both how the writing reads and its structural patterns. If you wrote this yourself, you can appeal this decision below."` |
| **High-confidence Human** | `"This piece reads as human-written. Our system found strong signs of natural, individual writing style ({confidence}% confidence) and did not detect the patterns typically seen in AI-generated text."` |
| **Uncertain** | `"We can't confidently tell whether this was written by a person or by AI ({confidence}% confidence). The signals we use gave mixed results, so please treat this classification as inconclusive, not a verdict."` |

No jargon ("classifier," "logit," "signal score") appears in any variant —
only "confidence," "signs," and "patterns," which a non-technical reader can
parse.

## 5. Appeals Workflow

- **Who:** the original creator (identified by `creator_id`), submitting
  against a specific `content_id` they received from `/submit`.
- **What they provide:** `content_id` + free-text `creator_reasoning`
  (e.g., "I wrote this myself; English is my second language").
- **What the system does:**
  1. Look up the submission by `content_id`; 404 if not found.
  2. Set that submission's `status` to `"under_review"`.
  3. Write a new audit-log row: `event_type: "appeal"`, same `content_id`,
     the `creator_reasoning`, and the new status — logged *next to* (not
     overwriting) the original classification row.
  4. Return `{content_id, status: "under_review", message: "Appeal received"}`.
- **What a human reviewer would see:** querying `GET /log` (or the
  `submissions` table) for a `content_id` shows the full history — original
  attribution/confidence/signals, then the appeal with the creator's stated
  reasoning and timestamp. Automated re-classification is explicitly out of
  scope; this is a queue for a human to review.

## 6. Anticipated Edge Cases

1. **Repetition-heavy poetry (e.g., villanelles, refrains, children's verse).**
   Deliberate repetition and simple vocabulary depress the type-token ratio
   and sentence-length variance — exactly the structural signature our
   stylometric heuristic associates with AI uniformity. A human poet writing
   in a tightly patterned form is at real risk of a false "AI-generated"
   read from Signal 2 alone (mitigated, but not eliminated, by requiring
   Signal 1 agreement for a high-confidence-AI label).
2. **Formal/academic writing and non-native-English prose.** Grammatically
   uniform, hedge-heavy, low-contraction writing (common in academic
   abstracts or from non-native speakers who learned formal written English)
   looks structurally similar to typical LLM output on both signals. This is
   the clearest fairness risk in the system and is called out explicitly in
   the README's Known Limitations section.
3. **Very short submissions (under ~3 sentences).** Sentence-length variance
   and type-token ratio are unstable on tiny samples — a two-sentence caption
   can swing to either extreme based on noise rather than authorship.

## Architecture

```
                         SUBMISSION FLOW
 ┌────────┐   text, creator_id    ┌──────────────────┐
 │ Client │ ─────────────────────▶│  POST /submit      │
 └────────┘                        │  (Flask route,     │
      ▲                            │   rate-limited)     │
      │                            └─────────┬───────────┘
      │                                      │ raw text
      │                     ┌────────────────┼─────────────────┐
      │                     ▼                                  ▼
      │           ┌───────────────────┐              ┌───────────────────┐
      │           │ Signal 1: Groq LLM │              │ Signal 2: Stylo-  │
      │           │ classifier         │              │ metric heuristics │
      │           │ -> llm_score (0-1) │              │ -> stylo_score    │
      │           └─────────┬─────────┘              └─────────┬─────────┘
      │                     │      llm_score, stylo_score       │
      │                     └────────────────┬───────────────────┘
      │                                      ▼
      │                          ┌────────────────────────┐
      │                          │  Confidence Scorer       │
      │                          │  0.65*llm + 0.35*stylo   │
      │                          │  -> combined confidence, │
      │                          │     attribution bucket   │
      │                          └───────────┬─────────────┘
      │                                      ▼
      │                          ┌────────────────────────┐
      │                          │  Label Generator          │
      │                          │  -> label text (3 variants)│
      │                          └───────────┬─────────────┘
      │                                      ▼
      │                          ┌────────────────────────┐
      │                          │  Audit Logger (SQLite)    │
      │                          │  writes: content_id,       │
      │                          │  timestamp, both scores,    │
      │                          │  attribution, label         │
      │                          └───────────┬─────────────┘
      │        content_id, attribution,      │
      │        confidence, label, signals     │
      └──────────────────────────────────────┘

                          APPEAL FLOW
 ┌────────┐  content_id, creator_reasoning   ┌───────────────────┐
 │ Client │ ────────────────────────────────▶│  POST /appeal       │
 └────────┘                                   │  (Flask route)      │
      ▲                                       └─────────┬───────────┘
      │                                                 │ lookup by content_id
      │                                                 ▼
      │                                       ┌───────────────────┐
      │                                       │ Update submission   │
      │                                       │ status ->            │
      │                                       │ "under_review"       │
      │                                       └─────────┬───────────┘
      │                                                 ▼
      │                                       ┌───────────────────┐
      │                                       │ Audit Logger:        │
      │                                       │ write appeal row      │
      │                                       │ (same content_id,      │
      │                                       │  creator_reasoning)     │
      │                                       └─────────┬───────────┘
      │        confirmation, status: under_review        │
      └────────────────────────────────────────────────┘

                GET /log  ->  reads audit_log table, returns
                              recent entries as JSON (grading /
                              transparency visibility)
```

**Narrative:** In the submission flow, raw text fans out to two independent
signal functions, whose scores converge at the Confidence Scorer, which
hands a single `(attribution, confidence)` pair to the Label Generator before
everything is durably recorded by the Audit Logger and returned to the
client. In the appeal flow, a `content_id` from a prior submission is looked
up, its status is flipped, and a new audit-log row is appended alongside the
original — the two flows share the same audit log table so a reviewer sees
the full decision history in one place.

## AI Tool Plan

**M3 — Submission endpoint + first signal.** Spec sections provided to the
AI tool: §2 (Detection Signals, Signal 1 row) + the Architecture diagram
above. Ask: generate the Flask app skeleton with a `POST /submit` stub, plus
a standalone `get_llm_signal(text) -> (score, rationale)` function calling
Groq. Verification: call `get_llm_signal` directly on 2–3 hand-picked strings
before wiring it into the route; confirm the function's return shape matches
what §2 says Signal 1 outputs (a float 0–1, not a raw API response object).

**M4 — Second signal + confidence scoring.** Spec sections provided: §2 (full
table) + §3 (Uncertainty Representation) + diagram. Ask: generate
`get_stylometric_signal(text) -> (score, metrics_dict)` and a
`score_confidence(llm_score, stylo_score) -> (confidence, attribution)`
function implementing the exact formula and asymmetric thresholds in §3.
Verification: run the four calibration inputs from Milestone 4 of the
assignment (clear AI, clear human, two borderline) and confirm the generated
thresholds match §3's numbers exactly — not just "reasonable-looking"
thresholds. Corrected one instance where the generated code used a symmetric
0.5 cutoff instead of the specified asymmetric 0.75/0.30 split (see README AI
Usage section).

**M5 — Production layer.** Spec sections provided: §4 (Transparency Label
Design) + §5 (Appeals Workflow) + diagram. Ask: generate a
`generate_label(attribution, confidence) -> str` function reproducing the
three template strings verbatim, and the `POST /appeal` route. Verification:
call `generate_label` with confidence values that should hit all three
buckets (e.g., 0.20, 0.55, 0.90) and diff the output against §4's exact
strings; POST a test appeal and confirm `GET /log` shows the status change
and the appeal row next to the original.
