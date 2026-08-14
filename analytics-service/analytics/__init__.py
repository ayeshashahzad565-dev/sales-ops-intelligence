"""Stage 5 statistical anomaly detection.

Layered so each concern is testable on its own:

    statistics.py   robust estimators - median, MAD, robust z    (pure)
    baseline.py     which prior days a day is compared against   (pure)
    detector.py     signal scoring and combination               (pure)
    models.py       the value types passed between them
    repository.py   the only module that touches PostgreSQL
    runner.py       sequencing and run counts
    api.py          HTTP surface for n8n
    cli.py          command-line entrypoint

Stage 4 = deterministic KPI generation.
Stage 5 = statistical anomaly detection.
Stage 7 = LLM root-cause reasoning.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
