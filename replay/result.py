"""
The replay result contract: what a replay run returns to its caller.

This directly encodes the three-way split the assignment calls out
as the central design decision (section 3.3, and the glossary entry
"Business outcome vs. failure"):

  - SUCCESS: the goal was achieved; declared outputs are populated.
  - BUSINESS_OUTCOME: a known, legitimate non-success result occurred
    (e.g. "no such member," "requires supervisor approval"). This is
    NOT an error -- the caller asked a valid question and got a real
    answer, just not the happy-path one. Distinguishing this from a
    crash is, per the brief, "the most common design mistake" to avoid.
  - HARD_FAILURE: something genuinely went wrong -- a locator chain
    was exhausted, the checkpoint was never reached, a timeout expired
    with no known outcome to explain it. Carries enough detail (which
    step, what was expected, what was observed) to debug without
    re-running.

RECOVERABLE conditions (a dismissed dialog, a retried transient load)
do not appear as a distinct top-level result: they're handled inline
during replay (see engine.py) and, if recovery succeeds, the run
proceeds to SUCCESS or a business outcome as normal. If recovery
itself fails, that becomes a HARD_FAILURE with the recovery attempt
noted in step_log.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"


class StepLogEntry(BaseModel):
    step_id: str
    action: str
    description: str
    status: str  # "ok" | "recovered" | "failed"
    detail: str = ""


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None

    # SUCCESS: populated outputs, per the artifact's declared output fields.
    outputs: dict[str, Any] = Field(default_factory=dict)

    # BUSINESS_OUTCOME: which known_outcome fired, and its description.
    outcome_name: Optional[str] = None
    outcome_description: Optional[str] = None

    # HARD_FAILURE: enough to debug without re-running.
    failed_step_id: Optional[str] = None
    failed_step_description: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    error_detail: Optional[str] = None

    # Always populated: a step-by-step log, regardless of outcome --
    # this is the observability requirement (section 3.5) applied to
    # the replay path specifically.
    step_log: list[StepLogEntry] = Field(default_factory=list)

    # Path to a screenshot taken on failure, if any -- the "richer
    # signal on failure" the brief asks for alongside the structured log.
    failure_screenshot: Optional[str] = None

    def mark_finished(self) -> None:
        self.finished_at = datetime.now(timezone.utc)