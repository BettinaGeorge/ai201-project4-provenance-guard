"""
app.py — Provenance Guard Flask backend.

Endpoints:
  POST /submit   -> classify submitted text, return attribution + label
  POST /appeal   -> contest a classification, flips status to under_review
  GET  /log      -> recent structured audit-log entries (grading visibility)
  GET  /health   -> liveness check
"""
import os
import uuid

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import scoring
import storage
from signals import get_llm_signal, get_stylometric_signal

load_dotenv()

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

storage.init_db()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    creator_id = (data.get("creator_id") or "").strip()

    if not text:
        return jsonify({"error": "text field is required and cannot be empty"}), 400
    if not creator_id:
        return jsonify({"error": "creator_id field is required"}), 400

    llm_score, llm_rationale, llm_available = get_llm_signal(text)
    stylo_score, stylo_metrics = get_stylometric_signal(text)

    confidence, attribution, label = scoring.score_and_label(llm_score, stylo_score)

    content_id = str(uuid.uuid4())
    created_at = storage.create_submission(
        content_id=content_id,
        creator_id=creator_id,
        text=text,
        llm_score=llm_score,
        llm_rationale=llm_rationale,
        stylo_score=stylo_score,
        stylo_metrics=stylo_metrics,
        confidence=confidence,
        attribution=attribution,
        label=label,
    )

    return jsonify(
        {
            "content_id": content_id,
            "creator_id": creator_id,
            "attribution": attribution,
            "confidence": confidence,
            "label": label,
            "status": "classified",
            "timestamp": created_at,
            "signals": {
                "llm": {
                    "score": llm_score,
                    "rationale": llm_rationale,
                    "live_call_available": llm_available,
                },
                "stylometric": {
                    "score": stylo_score,
                    "metrics": stylo_metrics,
                },
            },
        }
    ), 201


@app.route("/appeal", methods=["POST"])
@limiter.limit("20 per minute")
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = (data.get("content_id") or "").strip()
    creator_reasoning = (data.get("creator_reasoning") or "").strip()

    if not content_id:
        return jsonify({"error": "content_id field is required"}), 400
    if not creator_reasoning:
        return jsonify({"error": "creator_reasoning field is required"}), 400

    submission = storage.get_submission(content_id)
    if not submission:
        return jsonify({"error": f"no submission found for content_id {content_id}"}), 404

    storage.update_submission_status(content_id, "under_review")
    storage.log_event(
        content_id=content_id,
        creator_id=submission["creator_id"],
        event_type="appeal",
        attribution=submission["attribution"],
        confidence=submission["confidence"],
        llm_score=submission["llm_score"],
        stylo_score=submission["stylo_score"],
        label=submission["label"],
        status="under_review",
        appeal_reasoning=creator_reasoning,
    )

    return jsonify(
        {
            "content_id": content_id,
            "status": "under_review",
            "message": "Appeal received and logged. A human reviewer will examine this classification.",
        }
    ), 200


@app.route("/log", methods=["GET"])
def get_log():
    limit = request.args.get("limit", default=20, type=int)
    entries = storage.get_recent_log(limit=limit)
    return jsonify({"entries": entries})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
