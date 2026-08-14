"""What a notification says, and what it must never let a reader conclude.

The rule these tests protect: a reader who scans the message and stops early
must not come away believing a cause has been established. Stage 5 and Stage 6
produce facts; Stage 7 produces a guess; both arrive in the same message. The
labelling is the only thing keeping them apart, so it is asserted rather than
assumed.
"""

from __future__ import annotations

import json
from datetime import date

from analytics.notifications.models import (
    HYPOTHESIS,
    NOT_CONFIRMED,
    OBSERVED,
    build_payload,
    build_subject,
    render_text,
)

MINOR_ROW = {
    "calendar_date": date(2026, 6, 1),
    "day_name": "Monday",
    "severity": "minor",
    "routing": "auto_notify",
    "decision": "action_required",
    "decision_version": "stage6-v1",
    "business_impact_tier": "limited",
    "actual_net_revenue_usd": 12240.97,
    "expected_net_revenue_usd": 11113.00,
    "revenue_delta_usd": 1127.97,
    "revenue_delta_pct": 10.15,
    "average_order_value_usd": 208.00,
    "refund_rate": 0.161957,
    "orders_count": 59,
    "anomaly_score": 3.6263,
    "signal_count": 1,
}

CRITICAL_ROW = {
    **MINOR_ROW,
    "calendar_date": date(2026, 8, 5),
    "day_name": "Wednesday",
    "severity": "critical",
    "routing": "human_review",
    "business_impact_tier": "material",
    "actual_net_revenue_usd": 4748.95,
    "expected_net_revenue_usd": 13641.63,
    "revenue_delta_usd": -8892.68,
    "revenue_delta_pct": -65.19,
    "refund_rate": 0.357222,
    "anomaly_score": 8.9254,
    "signal_count": 3,
}

HYPOTHESIS_FIXTURE = {
    "summary": "Net revenue came in far below what prior Wednesdays earned.",
    "primary_hypothesis": "Consistent with a refund-related operational issue.",
    "confidence": "medium",
    "supporting_evidence": [{"metric": "refund_rate"}, {"metric": "orders_count"}],
    "missing_evidence": ["Refund reason codes are not stored."],
    "recommended_checks": ["Review individual orders for a common product."],
    "model_name": "llama-3.3-70b-versatile",
}

REASONS = ("SEVERE_REFUND_SPIKE", "STATISTICAL_ANOMALY")


# =============================================================================
# The three blocks
# =============================================================================


def test_the_payload_separates_observed_from_hypothesis():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    assert OBSERVED in payload
    assert HYPOTHESIS in payload
    assert NOT_CONFIRMED in payload


def test_measured_values_live_only_in_the_observed_block():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    observed = payload[OBSERVED]
    assert observed["actual_net_revenue_usd"] == 4748.95
    assert observed["expected_net_revenue_usd"] == 13641.63
    assert observed["revenue_delta_usd"] == -8892.68
    assert observed["severity"] == "critical"

    # The hypothesis block carries the model's words, not the warehouse's numbers.
    assert "actual_net_revenue_usd" not in payload[HYPOTHESIS]
    assert "revenue_delta_usd" not in payload[HYPOTHESIS]


def test_the_hypothesis_is_labelled_as_unconfirmed():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    caveat = payload[HYPOTHESIS]["caveat"].lower()
    assert "not a confirmed cause" in caveat
    assert "not been verified" in caveat


def test_the_not_confirmed_block_carries_the_models_own_evidence_gaps():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    gaps = payload[NOT_CONFIRMED]["missing_evidence"]
    assert "Refund reason codes are not stored." in gaps


def test_a_reader_who_stops_after_the_observed_block_has_read_only_facts():
    """The failure this guards against is a reader skimming and stopping.

    Everything in OBSERVED comes from kpi_daily or anomaly_decisions. If a
    speculative field ever migrated into it, a skim-reader would take a guess for
    a measurement.
    """
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)
    observed_text = json.dumps(payload[OBSERVED]).lower()

    for speculative in ("hypothesis", "likely", "plausible", "suggests", "probably",
                        "consistent with", "confidence"):
        assert speculative not in observed_text


# =============================================================================
# Required content
# =============================================================================


def test_the_payload_carries_every_field_an_operator_needs():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)
    observed = payload[OBSERVED]

    for field in (
        "calendar_date", "severity", "routing", "decision", "reason_codes",
        "actual_net_revenue_usd", "expected_net_revenue_usd", "revenue_delta_usd",
        "revenue_delta_pct", "average_order_value_usd", "refund_rate",
        "orders_count", "anomaly_score",
    ):
        assert field in observed, f"notification omits {field}"


def test_the_hypothesis_block_carries_summary_confidence_and_checks():
    analysis = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)[HYPOTHESIS]

    assert analysis["summary"] == HYPOTHESIS_FIXTURE["summary"]
    assert analysis["primary_hypothesis"] == HYPOTHESIS_FIXTURE["primary_hypothesis"]
    assert analysis["confidence"] == "medium"
    assert analysis["recommended_checks"]
    assert analysis["model"] == "llama-3.3-70b-versatile"


def test_reason_codes_are_included():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)
    assert payload[OBSERVED]["reason_codes"] == list(REASONS)


def test_the_subject_names_the_severity_and_the_movement():
    assert build_subject(CRITICAL_ROW).startswith("[CRITICAL]")
    assert "2026-08-05" in build_subject(CRITICAL_ROW)
    assert "down $8,892.68" in build_subject(CRITICAL_ROW)
    assert build_subject(MINOR_ROW).startswith("[Minor]")
    assert "up $1,127.97" in build_subject(MINOR_ROW)


def test_the_notification_is_not_a_database_dump():
    """A notification carrying every column is one nobody reads."""
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    assert len(payload[OBSERVED]) <= 20
    for internal in ("decision_id", "anomaly_id", "hypothesis_id", "evidence_digest",
                     "prompt_tokens", "request_id", "baseline_size"):
        assert internal not in payload[OBSERVED]


# =============================================================================
# Missing Stage 7 output
# =============================================================================


def test_a_missing_hypothesis_says_so_rather_than_inventing_one():
    payload = build_payload(CRITICAL_ROW, None, REASONS)

    assert payload[HYPOTHESIS]["status"] == "unavailable"
    assert "AI analysis unavailable" in payload[HYPOTHESIS]["note"]


def test_a_missing_hypothesis_leaves_the_observed_evidence_intact():
    """Section 18: the deterministic evidence is what justified the escalation."""
    payload = build_payload(CRITICAL_ROW, None, REASONS)

    assert payload[OBSERVED]["revenue_delta_usd"] == -8892.68
    assert payload[OBSERVED]["severity"] == "critical"
    assert payload[OBSERVED]["reason_codes"] == list(REASONS)


def test_a_missing_hypothesis_contains_no_speculative_language():
    payload = build_payload(CRITICAL_ROW, None, REASONS)
    text = json.dumps(payload[HYPOTHESIS]).lower()

    for invented in ("likely", "plausible", "consistent with", "probably", "suggests"):
        assert invented not in text


def test_a_missing_hypothesis_states_that_no_cause_is_established():
    payload = build_payload(CRITICAL_ROW, None, REASONS)
    assert "No cause has been established" in payload[NOT_CONFIRMED]["statement"]


# =============================================================================
# The rendered text
# =============================================================================


def test_the_text_has_all_three_labelled_sections_in_order():
    text = render_text(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    assert text.index(f"{OBSERVED}:") < text.index(f"{HYPOTHESIS}:") < text.index(f"{NOT_CONFIRMED}:")


def test_the_text_humanises_money_and_rates():
    text = render_text(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    assert "$4,748.95" in text
    assert "35.72%" in text
    assert "0.357222" not in text


def test_the_text_says_no_action_has_been_taken():
    """Stage 8 delivers; it does not act. The message has to say so."""
    minor_text = render_text(MINOR_ROW, HYPOTHESIS_FIXTURE, REASONS)
    critical_text = render_text(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)

    assert "No action has been taken" in minor_text
    assert "requires human review before any action is taken" in critical_text


def test_the_text_marks_a_missing_analysis_plainly():
    text = render_text(CRITICAL_ROW, None, REASONS)

    assert "AI analysis unavailable" in text
    assert "No cause has been established" in text


# =============================================================================
# Nothing sensitive
# =============================================================================


def test_no_credential_shaped_content_reaches_the_payload():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)
    serialised = json.dumps(payload).lower()

    for secret_ish in ("api_key", "apikey", "authorization", "bearer", "password",
                       "webhook_url", "gsk_", "sk-", "secret", "token"):
        assert secret_ish not in serialised, f"payload contains {secret_ish!r}"


def test_the_payload_never_carries_a_database_connection_string():
    payload = build_payload(CRITICAL_ROW, HYPOTHESIS_FIXTURE, REASONS)
    serialised = json.dumps(payload).lower()

    for leak in ("postgres", "dbname=", "host=", "5432"):
        assert leak not in serialised
