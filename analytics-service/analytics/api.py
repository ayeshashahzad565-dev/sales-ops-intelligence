"""HTTP surface.

n8n orchestrates this stage the same way it orchestrates the others - over the
Docker network, by service name. The n8n container has no Python runtime, so a
small HTTP service is how the detector becomes callable from a workflow without
either side knowing anything about the other's internals.

Four stages share this surface, in descending order of authority:

    /detect               Stage 5  statistical evidence
    /anomalies/analyze    Stage 7  LLM hypotheses
    /notifications/*      Stage 8  delivery
    /reviews/*            Stage 8  the human-review queue
    /remediation/*        Stage 9  human-approved remediation

Stage 6 has no endpoint at all: it is deterministic SQL and runs in the database.
That asymmetry is the architecture, not an oversight - the layer with the final
say is the one with the least machinery in front of it.

Stage 9 is the other end of the same idea. It has the most endpoints of any
stage and the least discretion: every one of them either records a human
decision or carries one out.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field

from analytics.config import LLMSettings, NotificationSettings, Settings
from analytics.detector import (
    ANOMALY_SCORE_THRESHOLD,
    DETECTOR_VERSION,
    ROBUST_Z_CAP,
)
from analytics.llm import prompts
from analytics.llm import service as llm_service
from analytics.llm.provider import OpenAICompatibleProvider
from analytics.notifications import service as notification_service
from analytics.notifications.provider import WebhookProvider
from analytics.operations import service as operations_service
from analytics.remediation import service as remediation_service
from analytics.remediation.models import ActionType as RemediationActionType
from analytics.remediation.provider import RecordingRemediationProvider
from analytics.runner import RunMode, run_detection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SERVICE_NAME = "analytics-service"
SERVICE_VERSION = "1.0.0"


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    detector_version: str


class DetectRequest(BaseModel):
    mode: RunMode = Field(
        default=RunMode.FULL,
        description=(
            "full recomputes and upserts every KPI date (default, recommended - "
            "baselines look backwards, so new data changes earlier verdicts). "
            "incremental writes only dates with no result for this detector version."
        ),
    )
    detector_version: str | None = Field(
        default=None,
        description="Override the detector version label. Normally left unset.",
    )


class DetectResponse(BaseModel):
    detector_version: str
    mode: str
    dates_evaluated: int
    dates_written: int
    dates_scored: int
    anomalies_detected: int
    dates_insufficient_history: int
    dates_incomplete_kpi: int
    earliest_date: str | None
    latest_date: str | None


class AnalyzeRequest(BaseModel):
    decision_version: str = Field(
        default=llm_service.DEFAULT_DECISION_VERSION,
        description="Which Stage 6 ruleset's decisions to explain.",
    )
    regenerate: bool = Field(
        default=False,
        description=(
            "Replace existing analyses for this prompt version and model. Off by "
            "default and never set by the schedule: silently rewriting reasoning a "
            "human may already have read would destroy the audit trail."
        ),
    )
    dates: list[date] | None = Field(
        default=None,
        description="Restrict to specific anomaly dates. Normally left unset.",
    )
    limit: int | None = Field(
        default=None, ge=1,
        description="Cap how many anomalies are analysed. A cost guard; what it drops is logged.",
    )


class AnalyzeFailure(BaseModel):
    calendar_date: str
    anomaly_id: int
    reason: str


class AnalyzeResponse(BaseModel):
    status: str
    decision_version: str
    prompt_version: str
    model_provider: str
    model_name: str
    eligible: int
    processed: int
    succeeded: int
    failed: int
    skipped_existing: int
    regenerated: int
    analysed_dates: list[str]
    failures: list[AnalyzeFailure]


app = FastAPI(
    title="Sales Ops Analytics Service",
    version=SERVICE_VERSION,
    summary="Anomaly detection, hypotheses, delivery, human review and remediation.",
    description=(
        "Four stages, deliberately unequal in authority.\n\n"
        "**/detect** (Stage 5) computes robust, calendar-aware statistical evidence "
        "into `salesops.anomaly_daily`. No LLM, no natural language, no severity.\n\n"
        "**/anomalies/analyze** (Stage 7) asks a language model to explain anomalies "
        "that Stage 6 has already judged actionable. It writes only to "
        "`salesops.anomaly_hypotheses` and cannot alter a Stage 6 decision - the "
        "response schema has no field for severity, routing or decision, and the "
        "database re-checks the snapshot before accepting a row.\n\n"
        "**/notifications/process** and **/reviews/** (Stage 8) deliver what Stage 6 "
        "decided: `auto_notify` becomes a notification, `human_review` becomes a queue "
        "item. Stage 8 reads Stage 6's routing rather than re-deriving it, and executes "
        "no business action - it ends at 'delivered' or 'queued for a human'.\n\n"
        "**/remediation/** (Stage 9) turns an approved review into an auditable, "
        "executed-once request for human work. Approving a review, authorising an "
        "action and executing it are three separate operations, and the database "
        "enforces that an action cannot exist without an approved review, cannot be "
        "of a type ineligible at its severity, and cannot execute twice. The "
        "development provider records the request and contacts nothing - there is no "
        "external business system in this project and none is pretended.\n\n"
        "**Stage 6 decides. Stage 7 explains. Stage 8 delivers. Stage 9 executes what "
        "a human approved.**\n\n"
        "Internal service, Docker network only, no authentication. `actor` is whatever "
        "the caller says it is. Not for public exposure."
    ),
)


@app.get("/health", response_model=HealthResponse, tags=["ops"], summary="Liveness probe")
def health() -> HealthResponse:
    """Return `ok` if the service can serve traffic.

    Backs the container health check, so it stays dependency-free: no database
    round trip. A warehouse problem is not a reason to restart this container.
    """
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
        detector_version=DETECTOR_VERSION,
    )


@app.get("/detector", tags=["ops"], summary="Detector parameters")
def detector_parameters() -> dict:
    """The constants that define this detector version.

    Exposed so a run can be reproduced, and so a reviewer can see the thresholds
    without reading the source.
    """
    from analytics import baseline, statistics

    return {
        "detector_version": DETECTOR_VERSION,
        "anomaly_score_threshold": ANOMALY_SCORE_THRESHOLD,
        "robust_z_significant": statistics.ROBUST_Z_SIGNIFICANT,
        "robust_z_cap": ROBUST_Z_CAP,
        "mad_to_sigma": statistics.MAD_TO_SIGMA,
        "signal_weights": {"revenue": 1.0, "aov": 0.5, "refund": 0.5, "orders": 0.5},
        "baseline": {
            "min_day_of_week_observations": baseline.MIN_DAY_OF_WEEK_OBSERVATIONS,
            "max_day_of_week_observations": baseline.MAX_DAY_OF_WEEK_OBSERVATIONS,
            "min_day_type_observations": baseline.MIN_DAY_TYPE_OBSERVATIONS,
            "max_day_type_observations": baseline.MAX_DAY_TYPE_OBSERVATIONS,
        },
    }


@app.post(
    "/detect",
    response_model=DetectResponse,
    tags=["detection"],
    summary="Run anomaly detection",
    responses={503: {"description": "The warehouse could not be reached."}},
)
def detect(request: DetectRequest) -> DetectResponse:
    """Score the KPI series and persist the results.

    Idempotent: results are keyed on `(calendar_date, detector_version)` and
    upserted, so running this twice over unchanged KPI data leaves the same rows
    with the same values.
    """
    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        # Misconfiguration, not a transient fault - say so rather than retrying.
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        summary = run_detection(
            settings=settings,
            mode=request.mode,
            detector_version=request.detector_version or DETECTOR_VERSION,
        )
    except Exception as exc:
        # Surfaced as 503 so the n8n HTTP node retries, and so a failed run is
        # never mistaken for a run that found nothing.
        logger.exception("Detection run failed")
        raise HTTPException(
            status_code=503,
            detail=f"Detection failed against {settings.describe()}: {exc}",
        ) from exc

    return DetectResponse(**summary.as_dict())


@app.get("/llm", tags=["ops"], summary="LLM configuration (no secrets)")
def llm_configuration() -> dict:
    """What Stage 7 is configured to call, and what it will send.

    Deliberately returns no key, not even a masked prefix - a prefix is still a
    fact about a credential, and this endpoint exists to make configuration
    debuggable, not to confirm what was typed. `configured` is enough to tell a
    missing key from a wrong one.
    """
    try:
        settings = LLMSettings.from_env()
    except RuntimeError as exc:
        return {
            "configured": False,
            "reason": str(exc),
            "prompt_version": prompts.PROMPT_VERSION,
        }

    return {
        "configured": True,
        "provider": settings.provider,
        "model": settings.model,
        "base_url": settings.base_url,
        "temperature": settings.temperature,
        "timeout_seconds": settings.timeout_seconds,
        "json_mode": settings.json_mode,
        "prompt_version": prompts.PROMPT_VERSION,
        "unavailable_sources": list(prompts.UNAVAILABLE_SOURCES),
    }


@app.post(
    "/anomalies/analyze",
    response_model=AnalyzeResponse,
    tags=["analysis"],
    summary="Generate root-cause hypotheses for actionable anomalies",
    responses={
        500: {"description": "Misconfiguration - no API key, no model, no database."},
        503: {"description": "The warehouse could not be reached."},
    },
)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """Explain the anomalies Stage 6 judged actionable.

    Only `decision = 'action_required'` records are considered; normal days and
    unscorable days are never sent to a model. Idempotent by
    `(anomaly, decision version, prompt version, model)` - a second run analyses
    nothing new and reports what it skipped.

    A per-anomaly failure is isolated: it is counted, named, and the run
    continues. In every failure path the Stage 6 decision is left exactly as it
    was; this endpoint has no code that writes to `anomaly_decisions`.
    """
    try:
        settings = Settings.from_env()
        llm_settings = LLMSettings.from_env()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    provider = OpenAICompatibleProvider(
        api_key=llm_settings.api_key,
        model=llm_settings.model,
        base_url=llm_settings.base_url,
        temperature=llm_settings.temperature,
        timeout_seconds=llm_settings.timeout_seconds,
        max_output_tokens=llm_settings.max_output_tokens,
        json_mode=llm_settings.json_mode,
        provider_name=llm_settings.provider,
    )

    logger.info("Stage 7 analysis requested against %s", llm_settings.describe())

    try:
        summary = llm_service.run_analysis(
            settings=settings,
            provider=provider,
            decision_version=request.decision_version,
            regenerate=request.regenerate,
            only_dates=request.dates,
            limit=request.limit,
        )
    except Exception as exc:
        # Reaching here means the warehouse itself was unreachable - individual
        # provider failures are handled per anomaly and never propagate. Surfaced
        # as 503 so a failed run is never mistaken for a run that found nothing.
        logger.exception("Stage 7 run failed")
        raise HTTPException(
            status_code=503,
            detail=f"Analysis failed against {settings.describe()}: {exc}",
        ) from exc

    return AnalyzeResponse(**summary.as_dict())


# =============================================================================
# Stage 8: delivery and human review
#
# Stage 6 decides. Stage 7 explains. Stage 8 delivers and queues review.
# Stage 8 cannot change Stage 6. Stage 8 executes no business action.
#
# AUTHENTICATION: there is none, on this endpoint or any other in this service.
# It listens on the Docker network only and is not published beyond the host
# port used for development. That is a real limitation, and it is stated here
# rather than papered over with a shared secret in an environment variable -
# which would look like authentication without being any.
# =============================================================================


class ProcessRequest(BaseModel):
    decision_version: str = Field(
        default=notification_service.DEFAULT_DECISION_VERSION,
        description="Which Stage 6 ruleset's decisions to route.",
    )
    dates: list[date] | None = Field(
        default=None, description="Restrict to specific anomaly dates."
    )
    resend: bool = Field(
        default=False,
        description=(
            "Re-attempt deliveries that already succeeded. Off by default and never "
            "set by the schedule - a rerun must not tell somebody the same thing twice."
        ),
    )


class ProcessResponse(BaseModel):
    status: str
    eligible: int
    processed: int
    notifications_sent: int
    notifications_failed: int
    reviews_created: int
    skipped: int
    notified_dates: list[str]
    queued_dates: list[str]
    failures: list[dict]


class ClaimRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200, description="Who is taking this on.")


class ResolveRequest(BaseModel):
    resolution: Literal[
        "confirmed", "false_positive", "expected_business_variation", "requires_follow_up"
    ] = Field(description="What the reviewer concluded. Not a new severity.")
    actor: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(
        default=None, max_length=4000,
        description="Free text. Stored as untrusted content and never sent anywhere.",
    )


def _notification_provider() -> tuple[WebhookProvider, NotificationSettings]:
    settings = NotificationSettings.from_env()
    provider = WebhookProvider(
        webhook_url=settings.webhook_url,
        timeout_seconds=settings.timeout_seconds,
        name=settings.provider,
    )
    return provider, settings


@app.get("/notifications", tags=["ops"], summary="Delivery configuration (no secrets)")
def notification_configuration() -> dict:
    """What Stage 8 is configured to deliver through.

    Returns the webhook HOST but never the path or query: webhook secrets live in
    the path, and an ops endpoint is a poor place to hand one out.
    """
    try:
        settings = NotificationSettings.from_env()
    except RuntimeError as exc:
        return {"configured": False, "reason": str(exc)}

    return {
        "configured": True,
        "provider": settings.provider,
        "webhook_host": settings.webhook_host,
        "timeout_seconds": settings.timeout_seconds,
        "sender": settings.sender,
        "recipient_count": len(settings.recipients),
        "max_delivery_attempts": notification_service.MAX_DELIVERY_ATTEMPTS,
    }


@app.post(
    "/notifications/process",
    response_model=ProcessResponse,
    tags=["delivery"],
    summary="Route actionable anomalies to notification or human review",
    responses={
        500: {"description": "Misconfiguration - no webhook URL, no recipients, no database."},
        503: {"description": "The warehouse could not be reached."},
    },
)
def process_notifications(request: ProcessRequest) -> ProcessResponse:
    """Deliver notifications and queue reviews, according to Stage 6's routing.

    Nothing here decides who should be told. `auto_notify` becomes a
    notification, `human_review` becomes a queue item, and both predicates are
    read from the Stage 6 decision row rather than derived from severity.

    Idempotent by `(anomaly, decision version, channel, recipient)` for
    notifications and by `(anomaly, decision version)` for reviews. A delivery
    that already succeeded is not repeated; one that failed retryably is retried
    on a later run, up to a bounded number of attempts.

    A delivery failure changes nothing about the anomaly. This endpoint has no
    code path that writes to `anomaly_decisions`.
    """
    try:
        settings = Settings.from_env()
        provider, notification_settings = _notification_provider()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("Stage 8 routing requested via %s", notification_settings.describe())

    try:
        summary = notification_service.run_routing(
            settings=settings,
            provider=provider,
            recipients=list(notification_settings.recipients),
            decision_version=request.decision_version,
            only_dates=request.dates,
            resend=request.resend,
        )
    except Exception as exc:
        logger.exception("Stage 8 routing failed")
        raise HTTPException(
            status_code=503,
            detail=f"Routing failed against {settings.describe()}: {exc}",
        ) from exc

    return ProcessResponse(**summary.as_dict())


@app.get("/reviews", tags=["review"], summary="The human-review queue")
def list_reviews(
    status: str | None = None,
    severity: str | None = None,
    limit: int = 100,
) -> dict:
    """Open review items, most severe first.

    Everything a reviewer needs to understand the escalation without leaving this
    response: what happened, what Stage 6 decided, what Stage 7 hypothesised,
    what supports it, and what evidence is missing.
    """
    settings = _require_database()
    items = notification_service.fetch_reviews(settings, status, severity, limit)
    return {"count": len(items), "reviews": _jsonable(items)}


@app.get("/reviews/{review_id}", tags=["review"], summary="One review item, with its history")
def get_review(review_id: int) -> dict:
    settings = _require_database()
    review = notification_service.fetch_review(settings, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"No review item {review_id}")
    return _jsonable(review)


@app.post("/reviews/{review_id}/claim", tags=["review"], summary="pending -> in_review")
def claim_review(review_id: int, request: ClaimRequest) -> dict:
    return _apply_transition(
        lambda settings: notification_service.claim_review(settings, review_id, request.actor)
    )


@app.post("/reviews/{review_id}/release", tags=["review"], summary="in_review -> pending")
def release_review(review_id: int) -> dict:
    """Return a claimed item to the queue.

    Without this, an item claimed by someone who then becomes unavailable is
    stuck in `in_review` forever, and the only remedy is a manual UPDATE that
    bypasses the state machine entirely.
    """
    return _apply_transition(
        lambda settings: notification_service.release_review(settings, review_id)
    )


@app.post("/reviews/{review_id}/resolve", tags=["review"], summary="in_review -> resolved")
def resolve_review(review_id: int, request: ResolveRequest) -> dict:
    """Record what a human concluded.

    The resolution is not a new severity and triggers nothing. Stage 8 ends at
    the recorded outcome; acting on it belongs to a later stage.
    """
    return _apply_transition(
        lambda settings: notification_service.resolve_review(
            settings, review_id, request.resolution, request.actor, request.notes
        )
    )


@app.post("/reviews/{review_id}/dismiss", tags=["review"], summary="-> dismissed")
def dismiss_review(review_id: int, request: ResolveRequest) -> dict:
    return _apply_transition(
        lambda settings: notification_service.dismiss_review(
            settings, review_id, request.resolution, request.actor, request.notes
        )
    )


# =============================================================================
# Stage 9: human-approved remediation
#
#     The LLM proposes. Deterministic rules decide. A human approves.
#     Only then may remediation execute.
#
# Three separate operations, deliberately. Approving a review says the anomaly
# is real and warrants a response; authorising an action says this is the
# response; executing it runs it. No endpoint here collapses two of those into
# one, because a caller who can approve and execute in a single request has
# human approval in name only.
#
# The action vocabulary is closed. `action_type` is a Literal of three values
# and the database holds a foreign key into the eligibility policy, so neither
# an HTTP caller nor a language model can name an action that does not exist or
# is not permitted at this severity.
#
# AUTHENTICATION: still none. `actor` is whatever the caller says it is, which
# is stated plainly here and in the README rather than dressed up.
# =============================================================================


class ApproveRequest(BaseModel):
    actor: str = Field(
        min_length=1, max_length=200,
        description="Who is approving. Recorded as the authorising human on every "
                    "action created from this review.",
    )
    action_type: Literal[
        "create_investigation", "request_refund_review", "request_operations_review"
    ] = Field(
        description="The remediation to propose. A closed vocabulary - and the "
                    "database refuses one that is not permitted at this severity.",
    )
    resolution: Literal["confirmed", "requires_follow_up"] = Field(
        default="confirmed",
        description="Approval requires a confirming resolution. A false positive or "
                    "an expected variation needs no action; use /resolve for those.",
    )
    notes: str | None = Field(
        default=None, max_length=4000,
        description="Free text. Stored as untrusted content, never executed, never "
                    "sent to a provider or a model.",
    )


class ActorRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)


class CloseRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    reason: str = Field(
        min_length=1, max_length=2000,
        description="Why this action will not be taken. Required - a closure with no "
                    "stated reason is indistinguishable from one that was forgotten.",
    )


class ExecuteApprovedRequest(BaseModel):
    limit: int = Field(
        default=100, ge=1, le=500,
        description="Cap how many authorised actions one run executes.",
    )
    actor: str = Field(
        default="stage9-workflow", max_length=200,
        description="Recorded as the executing actor. Never the approving one.",
    )


class ExecuteApprovedResponse(BaseModel):
    status: str
    eligible: int
    processed: int
    executed: int
    failed: int
    skipped: int
    executed_ids: list[int]
    failures: list[dict]


#: One provider instance per process, so `RecordingRemediationProvider` keeps
#: its record across requests and "the provider was called exactly once" is
#: observable through the API rather than only inside a test.
_REMEDIATION_PROVIDER = RecordingRemediationProvider()


@app.get("/remediation/actions", tags=["remediation"], summary="Action vocabulary and eligibility")
def remediation_vocabulary() -> dict:
    """What may be requested, and at which Stage 6 severity.

    Read from the database rather than from a constant here, so this endpoint
    cannot describe a policy the database does not enforce.
    """
    settings = _require_database()
    return _jsonable(remediation_service.fetch_vocabulary(settings))


@app.get("/remediation", tags=["remediation"], summary="Remediation actions")
def list_remediation(
    status: str | None = None,
    severity: str | None = None,
    action_type: str | None = None,
    limit: int = 100,
) -> dict:
    settings = _require_database()
    items = remediation_service.fetch_actions(settings, status, severity, action_type, limit)
    return {"count": len(items), "remediation": _jsonable(items)}


@app.get("/remediation/{remediation_id}", tags=["remediation"],
         summary="One action, with attempts and history")
def get_remediation(remediation_id: int) -> dict:
    settings = _require_database()
    action = remediation_service.fetch_action(settings, remediation_id)
    if action is None:
        raise HTTPException(status_code=404, detail=f"No remediation action {remediation_id}")
    return _jsonable(action)


@app.get("/remediation/{remediation_id}/events", tags=["remediation"], summary="Full audit history")
def get_remediation_events(remediation_id: int) -> dict:
    settings = _require_database()
    events = remediation_service.fetch_events(settings, remediation_id)
    if events is None:
        raise HTTPException(status_code=404, detail=f"No remediation action {remediation_id}")
    return {"remediation_id": remediation_id, "events": _jsonable(events)}


@app.post(
    "/reviews/{review_id}/approve",
    tags=["remediation"],
    summary="in_review -> approved, and propose one remediation action",
    responses={
        404: {"description": "No such review."},
        409: {"description": "The review is not in a state that permits approval, or "
                             "the action is not eligible at this severity."},
    },
)
def approve_review(review_id: int, request: ApproveRequest) -> dict:
    """Authorise remediation for a reviewed anomaly.

    This is the only door into Stage 9. It requires a review a human has claimed
    and is working, and it refuses `resolved` explicitly: that state means
    reviewed and closed WITHOUT remediation, and reading it as consent would be
    inventing an authorisation nobody gave.

    The action it creates is `proposed`. Nothing has executed, and nothing will
    until it is authorised and then executed - two further, separate calls.
    """
    return _apply_remediation(
        lambda settings: remediation_service.approve_review_for_remediation(
            settings, review_id, request.actor,
            RemediationActionType(request.action_type),
            request.resolution, request.notes,
        )
    )


@app.post(
    "/remediation/{remediation_id}/approve",
    tags=["remediation"],
    summary="proposed -> approved (authorise this action for execution)",
)
def authorize_remediation(remediation_id: int, request: ActorRequest) -> dict:
    """Authorise the action itself.

    Approving the review answered "is this anomaly real and does it warrant a
    response?". This answers "is this the response?". Separating them is what
    lets the record show a reviewer who confirmed an anomaly and then rejected
    the action proposed for it.
    """
    return _apply_remediation(
        lambda settings: remediation_service.authorize_action(
            settings, remediation_id, request.actor
        )
    )


@app.post("/remediation/{remediation_id}/reject", tags=["remediation"],
          summary="proposed -> rejected")
def reject_remediation(remediation_id: int, request: CloseRequest) -> dict:
    """Refuse this action. The anomaly stays confirmed; this response does not."""
    return _apply_remediation(
        lambda settings: remediation_service.reject_action(
            settings, remediation_id, request.actor, request.reason
        )
    )


@app.post("/remediation/{remediation_id}/cancel", tags=["remediation"], summary="-> cancelled")
def cancel_remediation(remediation_id: int, request: CloseRequest) -> dict:
    """Cancel an action that has not run.

    Not permitted once execution has started. An action already handed to a
    provider cannot be un-handed by changing a row, and recording otherwise
    would put a lie in the audit trail.
    """
    return _apply_remediation(
        lambda settings: remediation_service.cancel_action(
            settings, remediation_id, request.actor, request.reason
        )
    )


@app.post(
    "/remediation/{remediation_id}/execute",
    tags=["remediation"],
    summary="approved -> executing -> executed",
    responses={
        403: {"description": "The action has not been authorised by a human."},
        409: {"description": "The action is rejected, cancelled, or otherwise not runnable."},
    },
)
def execute_remediation(remediation_id: int, request: ActorRequest) -> dict:
    """Execute one authorised action.

    Refuses anything still `proposed` with a 403: approving the review confirmed
    the anomaly, and authorising the action is a separate step that execution
    requires.

    Idempotent. The claim into `executing` is a conditional UPDATE, so the
    provider is called at most once per logical action however many callers race
    for it, and an action that has already executed is reported as such rather
    than run again.
    """
    return _apply_remediation(
        lambda settings: remediation_service.execute_action(
            settings, _REMEDIATION_PROVIDER, remediation_id, request.actor
        )
    )


@app.post(
    "/remediation/execute-approved",
    response_model=ExecuteApprovedResponse,
    tags=["remediation"],
    summary="Execute every authorised action waiting to run",
    responses={503: {"description": "The warehouse could not be reached."}},
)
def execute_approved(request: ExecuteApprovedRequest) -> ExecuteApprovedResponse:
    """The batch operation the Stage 9 workflow calls.

    Its work set is `salesops.remediation_pending_execution`, which contains
    only actions a human authorised and already applies the retry budget. This
    endpoint therefore selects nothing, grades nothing, and authorises nothing -
    it runs what a person already said to run.

    Safe to repeat: an executed action is terminal and is not in the work set.
    """
    settings = _require_database()
    try:
        summary = remediation_service.execute_approved(
            settings=settings,
            provider=_REMEDIATION_PROVIDER,
            actor=request.actor,
            limit=request.limit,
        )
    except Exception as exc:
        logger.exception("Stage 9 execution run failed")
        raise HTTPException(
            status_code=503,
            detail=f"Remediation run failed against {settings.describe()}: {exc}",
        ) from exc

    return ExecuteApprovedResponse(**summary.as_dict())


# =============================================================================
# Stage 10: operational reliability
#
# Recovery, replay, retention and health. Two rules hold across every endpoint:
#
#   RECOVERY never repeats work.  Closing a stale run does not re-run it;
#   moving a crashed remediation to execution_unknown does not call a provider.
#
#   REPLAY is the only operation that repeats work.  It is explicit, bounded,
#   idempotent against fact_orders, and it accepts a batch id and NOTHING else -
#   the payloads come from the database, so there is no path by which a caller
#   can put an order into the warehouse through this surface.
# =============================================================================


class ReplayRequest(BaseModel):
    """Deliberately minimal.

    A batch id and an actor. No payload, no override, no correction field: a
    replay endpoint that accepted order data would be an unauthenticated write
    path into `fact_orders` wearing a recovery label.
    """

    model_config = {"extra": "forbid"}

    batch_id: UUID = Field(description="The failed staging batch to replay.")
    actor: str = Field(default=operations_service.RECOVERY_ACTOR, max_length=200)


class PurgeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    dry_run: bool = Field(
        default=True,
        description="Report what would be deleted without deleting it. On by default - "
                    "a cleanup whose safe mode must be asked for will one day be called "
                    "without arguments.",
    )
    actor: str = Field(default=operations_service.RECOVERY_ACTOR, max_length=200)


class ReconcileRequest(BaseModel):
    model_config = {"extra": "forbid"}

    outcome: Literal["confirmed_executed", "confirmed_not_executed"] = Field(
        description="What actually happened to an action stranded in execution_unknown.",
    )
    actor: str = Field(min_length=1, max_length=200)
    evidence: str = Field(
        min_length=1, max_length=2000,
        description="How this was established. Required - unattributed or unexplained, "
                    "a reconciliation is a guess with a timestamp.",
    )


class MaintenanceRequest(BaseModel):
    model_config = {"extra": "forbid"}

    actor: str = Field(default=operations_service.RECOVERY_ACTOR, max_length=200)
    purge: bool = Field(
        default=False,
        description="Actually delete retention-eligible staging rows. Off by default; "
                    "the reporting step runs either way.",
    )
    replay: bool = Field(
        default=False,
        description="Replay eligible failed batches. Off by default - replay is the one "
                    "maintenance operation that repeats work.",
    )
    retry_notifications: bool = Field(default=True)


@app.get("/operations/health", tags=["operations"], summary="Pipeline health")
def operational_health() -> dict:
    """Deterministic health, with the numbers behind it.

    Every component reports `reason_code`, `observed_value`, `threshold_value`
    and `measure`, so a caller can act on the numbers and recompute the verdict
    by hand. **No LLM output is read anywhere in this** - a health signal a
    language model could influence would be one nobody could trust during the
    incident that mattered.
    """
    settings = _require_database()
    return _jsonable(operations_service.health(settings))


@app.get("/operations/config", tags=["operations"], summary="Operational thresholds")
def operational_configuration() -> dict:
    """Every threshold Stage 10 compares against. No secrets: these are timeouts
    and counts, and the table holds nothing else."""
    settings = _require_database()
    return {"config": _jsonable(operations_service.configuration(settings))}


@app.get("/operations/retry-queue", tags=["operations"], summary="Everything that failed")
def operational_retry_queue(
    entity_type: str | None = None,
    eligible_only: bool = False,
    limit: int = 200,
) -> dict:
    """Failed operational records in one shape, whatever produced them.

    `disposition` is the machine-readable answer to "so what do I do about it?":
    SELF_HEALING_NEXT_RUN, RETRY_VIA_STAGE8_ROUTING, AWAITING_RECONCILIATION,
    REPLAYABLE, RETRY_BUDGET_SPENT, ABANDONED.
    """
    settings = _require_database()
    items = operations_service.retry_queue(settings, entity_type, eligible_only, limit)
    return {"count": len(items), "items": _jsonable(items)}


@app.get("/operations/events", tags=["operations"], summary="Recovery audit log")
def operational_events(
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> dict:
    """Everything Stage 10 did to the pipeline. Append-only in the database."""
    settings = _require_database()
    items = operations_service.events(settings, entity_type, entity_id, limit)
    return {"count": len(items), "events": _jsonable(items)}


@app.get("/operations/reviews/ageing", tags=["operations"], summary="How long reviews have waited")
def review_ageing(bucket: str | None = None) -> dict:
    """Operational ageing: fresh | warning | overdue | critical_overdue.

    **Not an anomaly severity and not comparable with one.** A critical anomaly
    reviewed within the hour is a healthy pipeline; a minor one unclaimed for a
    week is not. Read-only - nothing changes a review's state on account of its
    age, because "nobody has looked at this" is not a decision.
    """
    settings = _require_database()
    items = operations_service.review_ageing(settings, bucket)
    return {"count": len(items), "reviews": _jsonable(items)}


@app.get("/operations/replay-candidates", tags=["operations"], summary="Replayable failed batches")
def replay_candidates() -> dict:
    settings = _require_database()
    items = operations_service.replay_candidates(settings)
    return {"count": len(items), "candidates": _jsonable(items)}


@app.get("/operations/staging-retention", tags=["operations"],
         summary="What retention would delete")
def staging_retention() -> dict:
    """Readable before anything is deleted. `pending` and `failed` rows are
    never eligible - unfinished work and the dead-letter trail respectively."""
    settings = _require_database()
    return {"report": _jsonable(operations_service.retention_report(settings))}


@app.post("/operations/recover/runs", tags=["operations"], summary="Close stale runs")
def recover_runs(request: MaintenanceRequest | None = None) -> dict:
    """Close ingestion runs abandoned at `running`.

    The work is **not** repeated. Recovery stops the ledger lying about what is
    in flight; whether the work should be redone is a separate question with a
    different answer per pipeline.
    """
    settings = _require_database()
    actor = (request or MaintenanceRequest()).actor
    recovered = operations_service.recover_stale_runs(settings, actor)
    return {"recovered": len(recovered), "runs": _jsonable(recovered)}


@app.post("/operations/recover/remediation", tags=["operations"],
          summary="Recover crashed executions")
def recover_remediation(request: MaintenanceRequest | None = None) -> dict:
    """Move remediation actions stranded at `executing` into `execution_unknown`.

    **Calls no provider.** The process died somewhere around a provider call and
    nothing here can know whether it landed, so the uncertainty is recorded
    rather than resolved. Reconciliation is a separate, human operation.
    """
    settings = _require_database()
    actor = (request or MaintenanceRequest()).actor
    recovered = operations_service.recover_stale_remediation(settings, actor)
    return {"recovered": len(recovered), "actions": _jsonable(recovered)}


@app.post(
    "/remediation/{remediation_id}/reconcile",
    tags=["operations"],
    summary="execution_unknown -> executed | failed",
    responses={409: {"description": "The action is not awaiting reconciliation."}},
)
def reconcile_remediation(remediation_id: int, request: ReconcileRequest) -> dict:
    """A human states what actually happened.

    This is the only way out of `execution_unknown`, and it deliberately does
    not lead back to `executing`: confirming an execution did not happen returns
    the action to `failed`, where the ordinary bounded retry rules apply. So
    recovery can never re-execute, and a retry is always something a person
    chose.
    """
    settings = _require_database()
    try:
        return _jsonable(operations_service.reconcile_remediation(
            settings, remediation_id, request.outcome, request.actor, request.evidence,
        ))
    except operations_service.OperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/operations/replay",
    tags=["operations"],
    summary="Replay a failed staging batch",
    responses={409: {"description": "No such batch, or nothing eligible to replay."}},
)
def replay_batch(request: ReplayRequest) -> dict:
    """Re-run a failed batch's payloads through validation and loading.

    The original staging rows are **never modified**. Their payloads are copied
    into a new batch under a new run, so "the first attempt failed" and "a replay
    succeeded" stay separately true in separate places.

    Idempotent against `fact_orders` (`ON CONFLICT DO NOTHING`) and bounded by
    `max_replay_attempts`: a row that has failed validation three times is
    failing for a reason replay cannot fix.
    """
    settings = _require_database()
    try:
        return _jsonable(operations_service.replay_batch(
            settings, str(request.batch_id), request.actor))
    except operations_service.OperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/operations/staging/purge", tags=["operations"], summary="Retention sweep")
def purge_staging(request: PurgeRequest) -> dict:
    """Delete settled staging rows past the retention period. Dry run by default."""
    settings = _require_database()
    return _jsonable(operations_service.purge_staging(
        settings, request.dry_run, request.actor))


@app.post(
    "/operations/maintenance",
    tags=["operations"],
    summary="Run every maintenance operation",
    responses={503: {"description": "The warehouse could not be reached."}},
)
def run_maintenance(request: MaintenanceRequest) -> dict:
    """Every maintenance operation, each isolated from the others.

    A step that fails is recorded and the run continues: these operations are
    unrelated, and one being impossible says nothing about whether the others
    are. The run status is derived from the collection - `success`, `partial`
    when some steps failed and others did real work, `failed` only when nothing
    worked at all.
    """
    settings = _require_database()

    provider = None
    recipients: list[str] = []
    if request.retry_notifications:
        try:
            provider, notification_settings = _notification_provider()
            recipients = list(notification_settings.recipients)
        except RuntimeError:
            # No delivery channel configured. Every other maintenance operation
            # is unaffected, so this is a skipped step rather than a failed run.
            logger.info("No delivery channel configured; skipping notification retry")

    try:
        summary = operations_service.run_maintenance(
            settings=settings,
            provider=provider,
            recipients=recipients,
            actor=request.actor,
            purge=request.purge,
            replay=request.replay,
        )
    except Exception as exc:
        logger.exception("Stage 10 maintenance failed")
        raise HTTPException(
            status_code=503,
            detail=f"Maintenance failed against {settings.describe()}: {exc}",
        ) from exc

    return _jsonable(summary.as_dict())


@app.get("/dev/remediation-record", tags=["dev"], summary="What the recording provider received")
def remediation_record(limit: int = 20) -> dict:
    """Everything the development provider was asked to do.

    It records requests and contacts nothing, so this is the whole of its
    effect. `external_side_effect` is false on every attempt it ever returns.
    """
    received = _REMEDIATION_PROVIDER.received[-limit:]
    return {
        "provider": _REMEDIATION_PROVIDER.name,
        "calls": _REMEDIATION_PROVIDER.calls,
        "external_side_effect": False,
        "note": (
            "Development provider. It records the request and returns; no external "
            "business system exists in this project and none was contacted."
        ),
        "requests_per_action": _REMEDIATION_PROVIDER.requests_for,
        "received": _jsonable([
            {
                "remediation_id": request.remediation_id,
                "review_id": request.review_id,
                "calendar_date": request.calendar_date,
                "severity": request.severity,
                "action_type": str(request.action_type),
                "approved_by": request.approved_by,
                "authorized_by": request.authorized_by,
            }
            for request in received
        ]),
    }


# -----------------------------------------------------------------------------
# Local delivery destination
#
# Somewhere for notifications to go during development and live validation, so
# the webhook path can be exercised end to end without a Slack workspace, a mail
# server, or anyone's inbox being used as a test fixture.
#
# In memory, capped at 50, never persisted. It exists because the alternative -
# validating delivery against a real messaging channel - means either an
# integration nobody trusts or a genuine one everybody mutes.
#
# NOT for a real deployment. There is no authentication here either.
# -----------------------------------------------------------------------------
_SINK: deque[dict[str, Any]] = deque(maxlen=50)


@app.post("/dev/notification-sink", tags=["dev"], summary="Local webhook destination")
def notification_sink(body: dict = Body(...)) -> dict:
    _SINK.append({"received_at": datetime.now(UTC).isoformat(), "body": body})
    return {"accepted": True, "id": f"sink-{len(_SINK)}"}


@app.get("/dev/notification-sink", tags=["dev"], summary="What the local destination received")
def notification_sink_contents(limit: int = 10) -> dict:
    return {"count": len(_SINK), "received": list(_SINK)[-limit:]}


@app.delete("/dev/notification-sink", tags=["dev"], summary="Clear the local destination")
def clear_notification_sink() -> dict:
    _SINK.clear()
    return {"cleared": True}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _require_database() -> Settings:
    try:
        return Settings.from_env()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _apply_transition(operation) -> dict:
    """Run a review transition, mapping its refusals onto HTTP.

    409 rather than 400: the request is well-formed, the item is simply not in a
    state that permits it. A reviewer racing another reviewer gets a conflict,
    which is what actually happened.
    """
    settings = _require_database()
    try:
        return _jsonable(operation(settings))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except notification_service.ReviewTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _apply_remediation(operation) -> dict:
    """Run a Stage 9 operation, mapping its refusals onto HTTP.

    403 is reserved for exactly one thing: execution requested for something no
    human authorised. It is not a 409, because the request is not merely badly
    timed - it is an attempt to act without authorisation, and that deserves its
    own status code in a log somebody may one day read.
    """
    settings = _require_database()
    try:
        return _jsonable(operation(settings))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except remediation_service.NotAuthorized as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except remediation_service.RemediationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _jsonable(value):
    """Render dates, timestamps and Decimals for JSON without a serialiser class."""
    from decimal import Decimal

    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
