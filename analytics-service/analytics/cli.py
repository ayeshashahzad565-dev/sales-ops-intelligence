"""Command-line entrypoint.

The same detection the HTTP endpoint runs, without needing the service up.
Useful for backfills, for validating an algorithm change against live data, and
for reading the results without opening psql.

    python -m analytics.cli detect --mode full
    python -m analytics.cli show --limit 15
    python -m analytics.cli show --anomalies-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from analytics import repository
from analytics.config import Settings
from analytics.detector import DETECTOR_VERSION
from analytics.runner import RunMode, run_detection

logger = logging.getLogger("analytics.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analytics",
        description="Stage 5 statistical anomaly detection over salesops.kpi_daily.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    detect = subparsers.add_parser("detect", help="Run detection and persist results")
    detect.add_argument(
        "--mode",
        choices=[str(RunMode.FULL), str(RunMode.INCREMENTAL)],
        default=str(RunMode.FULL),
        help="full (default) recomputes every date; incremental writes only new ones",
    )
    detect.add_argument(
        "--detector-version",
        default=DETECTOR_VERSION,
        help=f"Version label to record (default: {DETECTOR_VERSION})",
    )

    show = subparsers.add_parser("show", help="Print stored detection results")
    show.add_argument("--limit", type=int, default=20, help="Rows to show (default 20)")
    show.add_argument(
        "--anomalies-only", action="store_true", help="Only rows flagged as anomalous"
    )
    show.add_argument("--detector-version", default=DETECTOR_VERSION)

    return parser


def _command_detect(args: argparse.Namespace) -> int:
    summary = run_detection(
        settings=Settings.from_env(),
        mode=RunMode(args.mode),
        detector_version=args.detector_version,
    )
    print(json.dumps(summary.as_dict(), indent=2))
    return 0


def _command_show(args: argparse.Namespace) -> int:
    settings = Settings.from_env()

    query = """
        SELECT calendar_date, anomaly_score, is_anomaly, baseline_status,
               baseline_kind, baseline_size, dominant_signal, signal_count,
               revenue_deviation_pct, revenue_robust_z,
               aov_deviation_pct, aov_robust_z,
               refund_rate_deviation, refund_robust_z,
               orders_deviation_pct, orders_robust_z
        FROM salesops.anomaly_daily
        WHERE detector_version = %(detector_version)s
          AND (%(anomalies_only)s IS FALSE OR is_anomaly)
        ORDER BY calendar_date DESC
        LIMIT %(limit)s
    """

    with repository.connect(settings.dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            query,
            {
                "detector_version": args.detector_version,
                "anomalies_only": args.anomalies_only,
                "limit": args.limit,
            },
        )
        rows = cursor.fetchall()

    if not rows:
        print("No detection results found. Run `detect` first.")
        return 0

    header = f"{'date':12} {'score':>7} {'flag':>5} {'dominant':>9} {'sig':>4}  {'status':<20}"
    print(header)
    print("-" * len(header))
    for row in rows:
        score = row["anomaly_score"]
        print(
            f"{row['calendar_date']!s:12} "
            f"{('-' if score is None else f'{float(score):7.2f}'):>7} "
            f"{('YES' if row['is_anomaly'] else '.'):>5} "
            f"{(row['dominant_signal'] or '-'):>9} "
            f"{row['signal_count']:>4}  "
            f"{row['baseline_status']:<20}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    args = _build_parser().parse_args(argv)

    try:
        if args.command == "detect":
            return _command_detect(args)
        if args.command == "show":
            return _command_show(args)
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"Failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
