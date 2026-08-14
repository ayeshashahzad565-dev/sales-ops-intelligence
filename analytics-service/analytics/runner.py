"""Orchestration: read KPIs, score them, persist the results.

Thin by design. The statistics live in `statistics.py`, the comparison rules in
`baseline.py`, the scoring in `detector.py`, and the SQL in `repository.py`.
This module only sequences them and counts what happened.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum

from analytics import repository
from analytics.config import Settings
from analytics.detector import DETECTOR_VERSION, detect_series
from analytics.models import BaselineStatus

logger = logging.getLogger(__name__)


class RunMode(StrEnum):
    """Which dates to write results for."""

    # Recompute and upsert every date. The default, and the recommended mode.
    FULL = "full"
    # Only dates with no result yet for this detector version.
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class DetectionRunSummary:
    """What one detection run did. Shaped for the ingestion_runs ledger."""

    detector_version: str
    mode: str

    #: KPI dates considered.
    dates_evaluated: int
    #: Results written (scored or explicitly unscored).
    dates_written: int
    #: Dates compared against a real baseline.
    dates_scored: int
    #: Flagged anomalous.
    anomalies_detected: int
    #: Too little history to judge. Expected near the start of a series.
    dates_insufficient_history: int
    #: KPI rows lacking FX coverage. A genuine data gap, not an expected state.
    dates_incomplete_kpi: int

    earliest_date: date | None
    latest_date: date | None

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["earliest_date"] = self.earliest_date.isoformat() if self.earliest_date else None
        payload["latest_date"] = self.latest_date.isoformat() if self.latest_date else None
        return payload


def run_detection(
    settings: Settings,
    mode: RunMode = RunMode.FULL,
    detector_version: str = DETECTOR_VERSION,
) -> DetectionRunSummary:
    """Score the KPI series and persist the results.

    Full mode is the default, and for this warehouse it is also the right one.
    Baselines look backwards, so a late-arriving order changes not only its own
    date's KPI row but the baseline of every later date. An incremental run that
    only touched new dates would leave earlier verdicts describing inputs that
    have since changed. At one row per trading day a full recomputation costs
    milliseconds, so correctness is free.

    Incremental mode exists for the case where that trade-off flips - a long
    history where only the newest date is genuinely new. It still reads the whole
    series to build baselines; it only narrows what gets written.
    """
    with repository.connect(settings.dsn) as connection:
        observations = repository.load_kpi_observations(connection)

        if not observations:
            logger.warning("kpi_daily is empty - nothing to detect")
            return _empty_summary(detector_version, mode)

        targets = observations
        if mode is RunMode.INCREMENTAL:
            already_done = repository.load_detected_dates(connection, detector_version)
            targets = [o for o in observations if o.calendar_date not in already_done]
            logger.info(
                "Incremental mode: %d of %d dates have no result for %s",
                len(targets), len(observations), detector_version,
            )

        # Baselines are always built from the FULL series, never from `targets`.
        # Narrowing the write scope must not narrow the statistical context.
        results = detect_series(observations, targets, detector_version)

        written = repository.save_results(connection, results)
        connection.commit()

    summary = DetectionRunSummary(
        detector_version=detector_version,
        mode=str(mode),
        dates_evaluated=len(targets),
        dates_written=written,
        dates_scored=sum(1 for r in results if r.baseline_status is BaselineStatus.SCORED),
        anomalies_detected=sum(1 for r in results if r.is_anomaly),
        dates_insufficient_history=sum(
            1 for r in results if r.baseline_status is BaselineStatus.INSUFFICIENT_HISTORY
        ),
        dates_incomplete_kpi=sum(
            1 for r in results if r.baseline_status is BaselineStatus.INCOMPLETE_KPI
        ),
        earliest_date=min((r.calendar_date for r in results), default=None),
        latest_date=max((r.calendar_date for r in results), default=None),
    )

    logger.info(
        "Detection complete: %d evaluated, %d scored, %d anomalous, "
        "%d insufficient history, %d incomplete KPI",
        summary.dates_evaluated, summary.dates_scored, summary.anomalies_detected,
        summary.dates_insufficient_history, summary.dates_incomplete_kpi,
    )
    return summary


def _empty_summary(detector_version: str, mode: RunMode) -> DetectionRunSummary:
    return DetectionRunSummary(
        detector_version=detector_version,
        mode=str(mode),
        dates_evaluated=0,
        dates_written=0,
        dates_scored=0,
        anomalies_detected=0,
        dates_insufficient_history=0,
        dates_incomplete_kpi=0,
        earliest_date=None,
        latest_date=None,
    )
