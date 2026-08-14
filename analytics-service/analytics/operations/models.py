"""What a maintenance run does, and what it reports.

Every maintenance step is independent. One that fails must not stop the others,
because the operations they perform are unrelated: a retention sweep failing has
nothing to do with whether a stale run gets closed, and a maintenance run that
abandoned the rest of its work because of one bad step would be a reliability
feature that reduces reliability.

So each step records its own outcome, and the run status is derived from the
collection - never asserted by having reached the end.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StepOutcome(StrEnum):
    """What one maintenance step achieved.

    `skipped` is distinct from `succeeded` on purpose: "there was nothing to do"
    and "I did it" are different facts, and collapsing them would make a run
    that quietly stopped working indistinguishable from a quiet week.
    """

    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class MaintenanceStep:
    name: str
    outcome: StepOutcome
    #: What the step actually did, in numbers. Shaped for the run ledger.
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "outcome": str(self.outcome),
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class MaintenanceSummary:
    """One maintenance run. Status derived, never asserted."""

    steps: list[MaintenanceStep] = field(default_factory=list)

    def record(self, step: MaintenanceStep) -> MaintenanceStep:
        self.steps.append(step)
        return step

    @property
    def succeeded(self) -> int:
        return sum(1 for s in self.steps if s.outcome is StepOutcome.SUCCEEDED)

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.steps if s.outcome is StepOutcome.SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.outcome is StepOutcome.FAILED)

    @property
    def status(self) -> str:
        """`failed` only when nothing worked.

        A maintenance run that closed three stale records and failed to purge
        staging did real work, and reporting it as an outright failure would
        train people to ignore the status. It is `partial`, which is the honest
        word and the one that makes the difference actionable.
        """
        if self.failed and self.succeeded == 0 and self.skipped == 0:
            return "failed"
        if self.failed:
            return "partial"
        return "success"

    @property
    def changes_made(self) -> int:
        """How much this run actually changed.

        The number that makes a rerun's idempotency checkable: a second run over
        an unchanged system must report zero.
        """
        return sum(
            int(value)
            for step in self.steps
            for key, value in step.detail.items()
            if key.startswith(("recovered", "deleted", "staged", "retried", "closed"))
            and isinstance(value, (int, float))
        )

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "steps_succeeded": self.succeeded,
            "steps_skipped": self.skipped,
            "steps_failed": self.failed,
            "changes_made": self.changes_made,
            "steps": [step.as_dict() for step in self.steps],
        }
