"""LLM output is untrusted input.

The model is a remote service returning text. It can be wrong, it can be
malformed, it can be a different model than yesterday, and - the case worth
designing for - it can be well-formed and quietly out of scope. These tests
cover all three, with no network and no database.

The rule throughout: a failed validation is a Stage 7 failure, never a repair.
Filling in a missing field means inventing content the model did not produce,
which is the exact fabrication this stage exists to prevent.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from analytics.llm.models import RootCauseHypothesis
from analytics.llm.provider import ProviderError, StaticProvider
from tests.llm_fixtures import valid_response


def parse(payload: dict) -> RootCauseHypothesis:
    return RootCauseHypothesis.model_validate(payload)


# =============================================================================
# The happy path
# =============================================================================


def test_a_well_formed_response_validates():
    hypothesis = parse(valid_response())

    assert hypothesis.confidence == "medium"
    assert hypothesis.primary_hypothesis
    assert len(hypothesis.supporting_evidence) == 2
    assert hypothesis.supporting_evidence[0].metric == "refund_rate"


def test_optional_lists_default_to_empty_rather_than_missing():
    payload = valid_response()
    del payload["alternative_hypotheses"]
    del payload["missing_evidence"]
    del payload["recommended_checks"]

    hypothesis = parse(payload)

    assert hypothesis.alternative_hypotheses == []
    assert hypothesis.missing_evidence == []


# =============================================================================
# Malformed and incomplete
# =============================================================================


def test_invalid_json_is_rejected_not_repaired():
    provider = StaticProvider(payload="{not json at all")

    with pytest.raises(ProviderError, match="invalid JSON"):
        provider.complete("system", "user", {})


def test_an_empty_response_is_rejected():
    provider = StaticProvider(payload=None)

    with pytest.raises(ProviderError, match="empty response"):
        provider.complete("system", "user", {})


@pytest.mark.parametrize(
    "field", ["summary", "confidence", "primary_hypothesis", "supporting_evidence"]
)
def test_missing_required_fields_are_rejected(field: str):
    payload = valid_response()
    del payload[field]

    with pytest.raises(ValidationError):
        parse(payload)


def test_an_empty_primary_hypothesis_is_rejected():
    """Well-formed JSON containing nothing is the failure a schema alone misses."""
    payload = valid_response()
    payload["primary_hypothesis"] = "   "

    with pytest.raises(ValidationError, match="must not be blank"):
        parse(payload)


def test_an_empty_summary_is_rejected():
    payload = valid_response()
    payload["summary"] = ""

    with pytest.raises(ValidationError):
        parse(payload)


def test_a_hypothesis_with_no_supporting_evidence_is_rejected():
    """Speculation wearing a schema."""
    payload = valid_response()
    payload["supporting_evidence"] = []

    with pytest.raises(ValidationError):
        parse(payload)


@pytest.mark.parametrize("value", ["very high", "HIGH", "0.8", "80%", "certain", ""])
def test_unsupported_confidence_values_are_rejected(value: str):
    payload = valid_response()
    payload["confidence"] = value

    with pytest.raises(ValidationError):
        parse(payload)


# =============================================================================
# The Stage 6 boundary
#
# The important case is not a malformed response - it is a perfectly valid one
# that quietly expands the model's remit.
# =============================================================================


@pytest.mark.parametrize(
    "field, value",
    [
        ("severity", "minor"),
        ("routing", "no_action"),
        ("decision", "no_action"),
        ("notification_allowed", True),
        ("human_review_required", False),
        ("is_anomaly", False),
        ("business_impact_tier", "trivial"),
    ],
)
def test_any_attempt_to_return_a_stage6_field_is_rejected(field: str, value):
    """`extra="forbid"` is what makes this true, and it is worth a test each.

    A model returning `severity: "minor"` beside a correct analysis is the most
    dangerous output this system can receive: it is valid JSON, it reads as
    helpful, and a downstream consumer that trusted it would have silently let
    the model overrule the deterministic layer.
    """
    payload = valid_response()
    payload[field] = value

    with pytest.raises(ValidationError, match="[Ee]xtra"):
        parse(payload)


def test_the_response_schema_has_no_stage6_field_at_all():
    """Not merely rejected on the way in - absent from the contract."""
    properties = set(RootCauseHypothesis.model_json_schema()["properties"])

    for forbidden in (
        "severity", "routing", "decision", "notification_allowed",
        "human_review_required", "is_anomaly", "anomaly_score",
    ):
        assert forbidden not in properties


def test_the_provider_schema_forbids_additional_properties_throughout():
    """So a provider honouring the schema refuses a stray field before we see it."""
    schema = RootCauseHypothesis.json_schema_for_provider()

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)


def test_unknown_fields_inside_nested_objects_are_also_rejected():
    payload = valid_response()
    payload["supporting_evidence"][0]["severity_override"] = "critical"

    with pytest.raises(ValidationError):
        parse(payload)


# =============================================================================
# Evidence grounding
# =============================================================================


def test_citing_a_metric_that_was_never_provided_is_rejected():
    """The one mechanical defence against a fabricated fact.

    A model that writes "payment_gateway_error_rate = 14%" in supporting evidence
    has produced something indistinguishable in form from a real observation.
    Requiring the metric name to come from the package makes that specific
    fabrication impossible.
    """
    payload = valid_response(metric="payment_gateway_error_rate")
    hypothesis = parse(payload)

    with pytest.raises(ValueError, match="never provided"):
        hypothesis.assert_cites_known_metrics(frozenset({"refund_rate", "orders_count"}))


def test_citing_only_provided_metrics_passes():
    hypothesis = parse(valid_response())

    hypothesis.assert_cites_known_metrics(frozenset({"refund_rate", "orders_count"}))


def test_the_error_names_every_unknown_metric():
    payload = valid_response(metric="inventory_stockouts")
    payload["supporting_evidence"].append(
        {"metric": "campaign_spend", "observation": "x", "relevance": "y"}
    )
    hypothesis = parse(payload)

    with pytest.raises(ValueError) as excinfo:
        hypothesis.assert_cites_known_metrics(frozenset({"orders_count"}))

    assert "campaign_spend" in str(excinfo.value)
    assert "inventory_stockouts" in str(excinfo.value)


# =============================================================================
# Provider failures all arrive the same way
# =============================================================================


def test_a_provider_exception_surfaces_as_provider_error():
    provider = StaticProvider(error=ProviderError("timed out after 60s"))

    with pytest.raises(ProviderError, match="timed out"):
        provider.complete("system", "user", {})


def test_a_schema_violating_payload_fails_at_the_provider_boundary():
    """Validation happens before the caller ever sees a hypothesis object."""
    provider = StaticProvider(payload={"summary": "only this"})

    with pytest.raises(ProviderError, match="failed validation"):
        provider.complete("system", "user", {})


def test_the_provider_records_what_it_was_asked():
    """Lets the service tests assert on the text the model would have received."""
    provider = StaticProvider(payload=valid_response())
    provider.complete("SYSTEM TEXT", "USER TEXT", {})

    assert provider.calls == [("SYSTEM TEXT", "USER TEXT")]


def test_provider_metadata_carries_provenance_and_no_credentials():
    provider = StaticProvider(payload=valid_response())
    _, metadata = provider.complete("system", "user", {})

    assert metadata.provider == "mock"
    assert metadata.model == "mock-model-v1"
    assert metadata.latency_ms is not None

    serialised = json.dumps(metadata.__dict__)
    for secret_ish in ("api_key", "authorization", "bearer", "sk-"):
        assert secret_ish not in serialised.lower()
