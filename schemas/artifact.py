"""
Artifact schema: the typed, versioned, serializable contract for a
"capability" — a reusable automation recorded once by an LLM-driven
discovery run, then replayed deterministically forever after.

Design intent (see /REPORT.md section 2 for the full rationale):
  - Steps are a flat, ordered list. No loops, no branching. Real
    back-office wizards are linear; adding control flow here would be
    complexity with no payoff at this scope.
  - Every locator carries an optional fallback. Primary = fast/specific
    (CSS), fallback = more resilient to markup churn (accessibility
    role+name or visible text). This is the concrete answer to "replay
    must use stable element/control targeting."
  - known_outcomes is a flat list evaluated after every step, not
    nested per-step. This is what lets the replay engine cleanly
    separate a business outcome ("no such member") from a recoverable
    condition (retry a slow load) from a hard failure — the three-way
    split the assignment explicitly asks for.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Locators
# ---------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    CSS = "css"
    ROLE = "role"          # Playwright get_by_role(name=...)
    TEXT = "text"          # Playwright get_by_text(...)
    TEST_ID = "test_id"    # data-testid, rare on our legacy surface


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str
    # For ROLE strategy, `value` is the role (e.g. "button") and
    # `name` is the accessible name (e.g. "Submit"). Optional because
    # CSS/TEXT/TEST_ID don't need it.
    name: Optional[str] = None
    fallback: Optional["Locator"] = None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    FILL = "fill"
    CLICK = "click"
    SELECT = "select"
    WAIT_FOR = "wait_for"


class Step(BaseModel):
    step_id: str
    action: ActionType
    description: str  # human-readable, for logs and REPORT.md, not used by the engine

    # navigate
    url: Optional[str] = None

    # fill / select / click / wait_for
    locator: Optional[Locator] = None

    # fill / select — supports {{param_name}} substitution, resolved
    # against the input params at replay time
    value: Optional[str] = None

    timeout_ms: int = 5000


# ---------------------------------------------------------------------------
# Params, outputs, outcomes
# ---------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str = ""


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str = ""
    extract_from: Locator  # where on the final page to read this value from


class OutcomeClassification(str, Enum):
    BUSINESS_OUTCOME = "business_outcome"   # legitimate result, not a crash
    RECOVERABLE = "recoverable"             # engine should attempt a defined recovery
    HARD_FAILURE = "hard_failure"           # stop, surface a debuggable error


class RecoveryAction(BaseModel):
    """Only meaningful when classification == RECOVERABLE."""
    action: Literal["retry_step", "dismiss_dialog", "wait_and_recheck"]
    max_attempts: int = 2
    dialog_locator: Optional[Locator] = None  # for dismiss_dialog


class KnownOutcome(BaseModel):
    name: str
    description: str
    # Condition that, if true after any step, means this outcome fired.
    condition: Locator
    classification: OutcomeClassification
    recovery: Optional[RecoveryAction] = None


class Checkpoint(BaseModel):
    """Confirms we actually reached the expected end state, rather than
    assuming the last click worked."""
    description: str
    locator: Locator


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    SAFE = "safe"        # read-only or easily reversible
    RISKY = "risky"       # irreversible or consequential — see guardrails.py


class SafetyPolicy(BaseModel):
    risk_level: RiskLevel
    requires_confirmation: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Target + metadata
# ---------------------------------------------------------------------------

class Target(BaseModel):
    base_url: str
    app_name: str = "mock_bank"


class Metadata(BaseModel):
    created_at: datetime = Field(default_factory=datetime.utcnow)
    discovered_by_model: str
    last_replayed_at: Optional[datetime] = None
    replay_count: int = 0


# ---------------------------------------------------------------------------
# Top-level artifact
# ---------------------------------------------------------------------------

class Artifact(BaseModel):
    capability: str
    version: int = 1
    description: str

    target: Target
    input_params: list[InputParam]
    steps: list[Step]
    checkpoint: Checkpoint
    outputs: list[OutputField]
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    safety: SafetyPolicy
    metadata: Metadata

    def param_names(self) -> set[str]:
        return {p.name for p in self.input_params}