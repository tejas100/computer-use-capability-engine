"""
Risky-action confirmation gate: enforces an artifact's declared
safety.requires_confirmation flag before replay is allowed to
actually execute any steps.

Per section 3.4: "Distinguish 'safe/reversible' actions from
risky/irreversible ones, and handle the risky class conservatively
(block, require confirmation, or flag -- your call, justify it)."

Design choice: require an explicit, separate confirmation argument at
the call site (--confirm-risky-action on the CLI, or an explicit
kwarg via the Python API) rather than an interactive y/n prompt.
Rationale: this is meant to be the path an AI agent invokes in
production (per the brief's framing), and an agent calling a function
can supply an explicit flag -- it can't answer an interactive
terminal prompt. The gate itself is what matters; how the caller
obtains authorization (a human approving in some other UI, a
policy engine, whatever) is out of scope here, but the artifact
cannot proceed past this gate without it either way.
"""

from __future__ import annotations

from schemas.artifact import Artifact, RiskLevel


class ConfirmationRequired(Exception):
    """Raised when a risky artifact is invoked without explicit
    confirmation. Always a hard stop -- replay never proceeds past
    this, and it is checked before a single step executes, before any
    browser session is even opened."""
    pass


def check_confirmation(artifact: Artifact, confirmed: bool) -> None:
    """
    Raise ConfirmationRequired if the artifact is risky and
    `confirmed` was not explicitly passed as True. Safe artifacts
    (risk_level == SAFE) are unaffected regardless of `confirmed`.
    """
    if artifact.safety.risk_level == RiskLevel.RISKY and artifact.safety.requires_confirmation:
        if not confirmed:
            raise ConfirmationRequired(
                f"Capability '{artifact.capability}' is marked risky and requires explicit "
                f"confirmation ({artifact.safety.notes!r}). Re-run with --confirm-risky-action "
                f"(CLI) or confirmed=True (Python API) to proceed."
            )