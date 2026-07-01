"""
smoke_test.py — exercises the detection + scoring + storage pipeline
directly (no Flask server needed). Useful for quickly sanity-checking the
core pipeline logic, and for generating audit-log evidence.

Run: python3 smoke_test.py
"""
import uuid

import scoring
import storage
from signals import get_llm_signal, get_stylometric_signal

CALIBRATION_TEXTS = {
    "clearly_ai": (
        "Artificial intelligence represents a transformative paradigm shift in modern "
        "society. It is important to note that while the benefits of AI are numerous, "
        "it is equally essential to consider the ethical implications. Furthermore, "
        "stakeholders across various sectors must collaborate to ensure responsible "
        "deployment."
    ),
    "clearly_human": (
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium in it and "
        "i was thirsty for like three hours after. my friend got the spicy version and "
        "said it was better. probably won't go back unless someone drags me there"
    ),
    "borderline_formal_human": (
        "The relationship between monetary policy and asset price inflation has been "
        "extensively studied in the literature. Central banks face a fundamental "
        "tension between their mandate for price stability and the unintended "
        "consequences of prolonged low interest rates on equity and real estate "
        "valuations."
    ),
    "borderline_edited_ai": (
        "I've been thinking a lot about remote work lately. There are genuine "
        "tradeoffs — flexibility and no commute on one side, isolation and blurred "
        "work-life boundaries on the other. Studies show productivity varies widely "
        "by individual and role type."
    ),
}


def run_calibration():
    print("=" * 70)
    print("STEP 1 — Stylometric signal (real, pure-Python) on 4 calibration texts")
    print("(LLM signal shown as-is: sandbox has no outbound network access to")
    print(" Groq, so it reports its documented graceful fallback. With a real")
    print(" GROQ_API_KEY and network access this returns a live probability_ai.)")
    print("=" * 70)
    results = {}
    for name, text in CALIBRATION_TEXTS.items():
        llm_score, llm_reason, llm_live = get_llm_signal(text)
        stylo_score, stylo_metrics = get_stylometric_signal(text)
        results[name] = (llm_score, stylo_score, stylo_metrics)
        print(f"\n[{name}]")
        print(f"  llm_score={llm_score} (live_call_available={llm_live}) reason={llm_reason}")
        print(f"  stylo_score={stylo_score}")
        print(f"  stylo_metrics={stylo_metrics}")
    return results


def run_scoring_unit_tests():
    print("\n" + "=" * 70)
    print("STEP 2 — scoring.py unit test: direct (llm_score, stylo_score) pairs")
    print("This isolates the confidence-scoring formula/thresholds from signal")
    print("acquisition, so all 3 label buckets can be demonstrated deterministically.")
    print("=" * 70)
    pairs = [
        ("high-confidence AI (both signals agree text is AI-like)", 0.93, 0.81),
        ("high-confidence human (both signals agree text is human-like)", 0.12, 0.22),
        ("uncertain: mid-range combined score", 0.55, 0.48),
        ("uncertain: high combined score but signals DISAGREE (safety check)", 0.90, 0.30),
    ]
    for description, llm_score, stylo_score in pairs:
        confidence, attribution, label = scoring.score_and_label(llm_score, stylo_score)
        print(f"\n[{description}]")
        print(f"  llm_score={llm_score}  stylo_score={stylo_score}")
        print(f"  -> confidence={confidence}  attribution={attribution}")
        print(f"  -> label: {label}")


def run_submission_and_appeal_demo():
    print("\n" + "=" * 70)
    print("STEP 3 — full submission -> audit log -> appeal flow (via storage.py,")
    print("the same functions app.py's /submit and /appeal routes call)")
    print("=" * 70)
    storage.init_db()

    demo_submissions = []
    for name, text in CALIBRATION_TEXTS.items():
        llm_score, llm_rationale, _ = get_llm_signal(text)
        stylo_score, stylo_metrics = get_stylometric_signal(text)
        confidence, attribution, label = scoring.score_and_label(llm_score, stylo_score)
        content_id = str(uuid.uuid4())
        storage.create_submission(
            content_id=content_id,
            creator_id=f"demo-creator-{name}",
            text=text,
            llm_score=llm_score,
            llm_rationale=llm_rationale,
            stylo_score=stylo_score,
            stylo_metrics=stylo_metrics,
            confidence=confidence,
            attribution=attribution,
            label=label,
        )
        demo_submissions.append((name, content_id, attribution, confidence, label))
        print(f"\nSubmitted [{name}] -> content_id={content_id}")
        print(f"  attribution={attribution} confidence={confidence}")
        print(f"  label={label}")

    appeal_target = demo_submissions[2]  # the borderline_formal_human one
    _, appeal_content_id, orig_attribution, orig_confidence, orig_label = appeal_target
    print(f"\nFiling appeal for content_id={appeal_content_id} (originally '{orig_attribution}')")
    submission = storage.get_submission(appeal_content_id)
    storage.update_submission_status(appeal_content_id, "under_review")
    storage.log_event(
        content_id=appeal_content_id,
        creator_id=submission["creator_id"],
        event_type="appeal",
        attribution=submission["attribution"],
        confidence=submission["confidence"],
        llm_score=submission["llm_score"],
        stylo_score=submission["stylo_score"],
        label=submission["label"],
        status="under_review",
        appeal_reasoning=(
            "This is a formal academic-style excerpt I wrote myself for a "
            "policy class; my writing has always been described as dry/formal."
        ),
    )
    updated = storage.get_submission(appeal_content_id)
    print(f"  new status={updated['status']}")

    print("\n" + "=" * 70)
    print("STEP 4 — GET /log equivalent: recent structured audit-log entries")
    print("=" * 70)
    for entry in storage.get_recent_log(limit=10):
        print(entry)


if __name__ == "__main__":
    run_calibration()
    run_scoring_unit_tests()
    run_submission_and_appeal_demo()
