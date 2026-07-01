"""
scoring.py — combine the two signals into one confidence score, classify
the result into an attribution bucket, and generate the plain-language
transparency label.

Thresholds and formula are defined in planning.md §3-4. Kept in one place
so the numbers in code and the numbers in the docs can never silently
diverge.
"""

LLM_WEIGHT = 0.65
STYLO_WEIGHT = 0.35

HIGH_AI_CONFIDENCE_THRESHOLD = 0.75
HIGH_AI_AGREEMENT_THRESHOLD = 0.55  # both signals must clear this to call "AI"
HIGH_HUMAN_CONFIDENCE_THRESHOLD = 0.30

ATTRIBUTION_AI = "likely_ai_generated"
ATTRIBUTION_HUMAN = "likely_human_written"
ATTRIBUTION_UNCERTAIN = "uncertain"


def combine_scores(llm_score, stylo_score):
    """Weighted average of the two signal scores, rounded to 2 decimals."""
    return round(LLM_WEIGHT * llm_score + STYLO_WEIGHT * stylo_score, 4)


def classify(confidence, llm_score, stylo_score):
    """
    Maps (confidence, llm_score, stylo_score) -> attribution bucket.

    Asymmetric by design: calling something "AI-generated" requires BOTH
    signals to agree (reduces false positives, which are more harmful to
    creators than false negatives on a writing platform). Calling something
    "human-written" only requires a low combined score.
    """
    if confidence >= HIGH_AI_CONFIDENCE_THRESHOLD and min(llm_score, stylo_score) >= HIGH_AI_AGREEMENT_THRESHOLD:
        return ATTRIBUTION_AI
    if confidence <= HIGH_HUMAN_CONFIDENCE_THRESHOLD:
        return ATTRIBUTION_HUMAN
    return ATTRIBUTION_UNCERTAIN


_LABEL_TEMPLATES = {
    ATTRIBUTION_AI: (
        "This piece shows strong signs of being AI-generated. Our system is "
        "fairly confident in this result ({pct}% confidence), based on both "
        "how the writing reads and its structural patterns. If you wrote "
        "this yourself, you can appeal this decision below."
    ),
    ATTRIBUTION_HUMAN: (
        "This piece reads as human-written. Our system found strong signs "
        "of natural, individual writing style ({pct}% confidence) and did "
        "not detect the patterns typically seen in AI-generated text."
    ),
    ATTRIBUTION_UNCERTAIN: (
        "We can't confidently tell whether this was written by a person or "
        "by AI ({pct}% confidence). The signals we use gave mixed results, "
        "so please treat this classification as inconclusive, not a "
        "verdict."
    ),
}


def generate_label(attribution, confidence):
    pct = round(confidence * 100)
    return _LABEL_TEMPLATES[attribution].format(pct=pct)


def score_and_label(llm_score, stylo_score):
    """Convenience wrapper used by the /submit route."""
    confidence = combine_scores(llm_score, stylo_score)
    attribution = classify(confidence, llm_score, stylo_score)
    label = generate_label(attribution, confidence)
    return confidence, attribution, label
