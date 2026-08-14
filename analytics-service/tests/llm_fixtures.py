"""Builders for the Stage 7 tests.

Everything here is a plain dictionary shaped like the row `repository.py`
returns, so the prompt builder and the validator can be exercised with no
database and no network. The two live dates the specification names are modelled
faithfully - 2026-08-05 with its refund spike and AOV collapse, 2026-08-09 with
the highest score in the series and a lower severity - because those are the two
cases where a regression would be least obvious and most damaging.
"""

from __future__ import annotations

from datetime import date, timedelta

# The injected event: revenue and order value collapse, refunds explode, order
# volume barely moves. Stage 6 rated it critical.
CRITICAL_ROW = {
    "decision_id": 501,
    "anomaly_id": 301,
    "calendar_date": date(2026, 8, 5),
    "day_name": "Wednesday",
    "decision_version": "stage6-v1",
    "severity": "critical",
    "routing": "human_review",
    "decision": "action_required",
    "decision_reason_code": "CRITICAL_COMBINED_IMPACT",
    "anomaly_score": 8.9254,
    "signal_count": 3,
    "baseline_status": "scored",
    "business_impact_tier": "material",
    "expected_net_revenue_usd": 13641.63,
    "actual_net_revenue_usd": 4748.95,
    "revenue_delta_usd": -8892.68,
    "revenue_delta_pct": -65.1881,
    "revenue_robust_z": -3.7784,
    "aov_robust_z": -4.2900,
    "refund_robust_z": 39.2302,
    "orders_robust_z": -0.2200,
    "revenue_deviation_pct": -65.1881,
    "aov_deviation_pct": -64.4174,
    "refund_rate_deviation": 0.335483,
    "orders_deviation_pct": -2.8037,
    "orders_count": 51,
    "customers_count": 48,
    "units_sold": 96,
    "gross_revenue_usd": 7388.12,
    "refund_amount_usd": 2639.17,
    "net_revenue_usd": 4748.95,
    "average_order_value_usd": 93.12,
    "refund_rate": 0.357222,
    "rolling_7d_net_revenue_usd": 9120.44,
    "rolling_28d_net_revenue_usd": 10233.10,
    "already_analysed": False,
}

# The highest anomaly score in the live series - and only major, because it is a
# single measure moving upward with no operational damage behind it. The one
# case that proves severity is not a re-labelling of the score.
MAJOR_ROW = {
    "decision_id": 502,
    "anomaly_id": 302,
    "calendar_date": date(2026, 8, 9),
    "day_name": "Sunday",
    "decision_version": "stage6-v1",
    "severity": "major",
    "routing": "human_review",
    "decision": "action_required",
    "decision_reason_code": "HIGH_REVENUE_IMPACT",
    "anomaly_score": 12.9368,
    "signal_count": 2,
    "baseline_status": "scored",
    "business_impact_tier": "severe",
    "expected_net_revenue_usd": 3916.00,
    "actual_net_revenue_usd": 13106.36,
    "revenue_delta_usd": 9190.36,
    "revenue_delta_pct": 234.6554,
    "revenue_robust_z": 21.3797,
    "aov_robust_z": 2.1000,
    "refund_robust_z": -0.0604,
    "orders_robust_z": 4.8000,
    "revenue_deviation_pct": 234.6554,
    "aov_deviation_pct": 46.3816,
    "refund_rate_deviation": -0.001961,
    "orders_deviation_pct": 125.6410,
    "orders_count": 88,
    "customers_count": 80,
    "units_sold": 190,
    "gross_revenue_usd": 13390.00,
    "refund_amount_usd": 283.64,
    "net_revenue_usd": 13106.36,
    "average_order_value_usd": 148.94,
    "refund_rate": 0.021182,
    "rolling_7d_net_revenue_usd": 10400.00,
    "rolling_28d_net_revenue_usd": 10100.00,
    "already_analysed": False,
}


def history_rows(anomaly_date: date, day_name: str, count: int = 6) -> list[dict]:
    """Prior same-weekday observations, all strictly earlier than the anomaly."""
    return [
        {
            "calendar_date": anomaly_date - timedelta(days=7 * (index + 1)),
            "day_name": day_name,
            "net_revenue_usd": 13000.0 + index * 150,
            "orders_count": 52 + index,
            "average_order_value_usd": 250.0 + index,
            "refund_rate": 0.022 + index * 0.001,
        }
        for index in range(count)
    ]


def preceding_rows(anomaly_date: date, count: int = 5) -> list[dict]:
    """The days immediately before the anomaly, most recent first."""
    names = ["Tuesday", "Monday", "Sunday", "Saturday", "Friday"]
    return [
        {
            "calendar_date": anomaly_date - timedelta(days=index + 1),
            "day_name": names[index % len(names)],
            "net_revenue_usd": 12500.0 - index * 200,
            "orders_count": 50 - index,
            "average_order_value_usd": 248.0,
            "refund_rate": 0.023,
        }
        for index in range(count)
    ]


def valid_response(metric: str = "refund_rate") -> dict:
    """A well-formed hypothesis citing a metric that exists in the package."""
    return {
        "summary": (
            "Net revenue on 2026-08-05 came in far below what prior Wednesdays "
            "earned, while order volume held roughly steady."
        ),
        "confidence": "medium",
        "primary_hypothesis": (
            "The pattern is consistent with a problem affecting order value and "
            "refunds rather than demand, since customers still ordered at a normal "
            "rate. The specific cause cannot be determined from this dataset."
        ),
        "supporting_evidence": [
            {
                "metric": metric,
                "observation": "The refund rate reached 35.72% against a baseline near 2%.",
                "relevance": "A refund surge of this size directly depresses net revenue.",
            },
            {
                "metric": "orders_count",
                "observation": "51 orders, close to the same-weekday norm.",
                "relevance": "Demand was intact, which argues against a traffic or outage cause.",
            },
        ],
        "alternative_hypotheses": [
            {
                "hypothesis": "A pricing or discount error reduced realised order value.",
                "why_plausible": "Average order value fell 64% while volume held.",
                "what_would_confirm": "Pricing and discount records, which this warehouse lacks.",
            }
        ],
        "missing_evidence": [
            "Refund reason codes are not stored, so the driver of the refund surge is unknown.",
            "No pricing or promotion history exists to test the discount hypothesis.",
        ],
        "recommended_checks": [
            "Review individual orders on 2026-08-05 for a common product or channel.",
            "Compare refund timestamps against any deployment on that date.",
        ],
    }
